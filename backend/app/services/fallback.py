"""推荐降级快照 / 兜底表（T11）。

用途
----
当 LLM 全部不可用（连续失败 / 停服 / 欠费 → 见 PRD §5.3）时，前端切「**专家规则模式**」，
需要一份**不依赖 LLM** 就能给出的「冲稳保」结构化精简表。本模块产出的正是这份表。

与正常推荐（T08）的关系
----------------------
**不是另一套算法**，而是**同一套 T08 引擎**（``RecommendEngine``）的**精简投影**：

- 复用 T08 的候选过滤 / 三年加权 / 冲稳保分档 / 档内去重排序 —— 保证**降级结果与正常结果口径一致**，
  不会出现「降级后另一套结论」这种最坏情况；
- 但**只保留结构化字段**（院校 / 层次 / 往年位次 / 档位），**丢弃全部自然语言解释**
  （explanation / risk_explanation / parent_summary）—— 降级时**没有 LLM，绝不伪造解释**。

这样设计的理由：把「算法」与「表达」解耦。算法是确定性的、离线可算；表达才需要 LLM。
LLM 挂了只影响表达层，不影响决策层 —— 这正是「优雅降级」的关键。

红线
----
- 兜底表数据**同样必须来自库内**（经 T08 引擎从 MySQL / 可注入数据源取数）；
- 必须标注「**专家规则模式**」（``mode="expert_rule"``）；
- **不得因为降级而放宽「无数据标注」红线**：缺年 ``ref_years_note``、选科待补 ``require_note``
  必须原样带出；
- 强制免责声明随结果返回。

缓存
----
- 键内嵌**数据集指纹**（T07 ``ScoreRepository.dataset_version`` 同源的 ``dv`` 口径）；
  本模块用「请求关键参数 + 数据源指纹」构成键，数据变更即自然失效；
- TTL 兜底（默认 1 天）；
- Redis 故障 → **静默降级直算**，不阻塞（缓存是优化不是依赖）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import DataMissingError, ValidationError
from app.core.logging import get_logger
from app.schemas.fallback import (
    MODE_EXPERT_RULE,
    FallbackResult,
    FallbackRow,
    FallbackTier,
)
from app.schemas.recommend import RecommendResult
from app.services.recommend import RecommendEngine, RecommendRepository

logger = get_logger(__name__)

# 缓存 TTL（秒）—— 1 天。真正失效靠数据源指纹，TTL 仅兜底。
FALLBACK_CACHE_TTL_SECONDS = 86400

# 兜底表每档默认条数（比正常报告的 tier_limit 更精简，便于降级页快速加载）
DEFAULT_FALLBACK_LIMIT = 6


@runtime_checkable
class FallbackRepository(Protocol):
    """兜底服务所需的数据访问接口。

    **刻意与 T08 的 ``RecommendRepository`` 保持同形**：兜底服务把请求转交给 T08 引擎，
    因此直接复用后者的数据源。此处定义别名协议便于类型标注与测试注入。
    """

    def iter_admission_rows(self, province: str, subject_type: str, years: list[int]) -> list[dict]: ...

    def university_meta(self, codes: list[str]) -> dict[str, dict]: ...


def recommend_result_to_fallback(
    result: RecommendResult,
    *,
    tier_limit: int = DEFAULT_FALLBACK_LIMIT,
    generated_at: str = "",
) -> FallbackResult:
    """把 T08 完整推荐结果**投影**为降级兜底表（纯函数，便于单测）。

    - 只取结构化字段，**丢弃解释**；
    - 每档截断到 ``tier_limit``（兜底表求「快」，不求「全」）；
    - 缺年 ``ref_years_note`` / 选科 ``require_note`` **原样带出**（红线）。
    """
    tiers: list[FallbackTier] = []
    for g in result.tiers:
        rows = [
            FallbackRow(
                university_code=it.university_code,
                university_name=it.university_name,
                group_code=it.group_code,
                subject_type=it.subject_type,
                tier=it.tier or g.tier,
                tier_label=it.tier_label or g.label,
                ref_rank=it.ref_rank,
                latest_year=it.latest_year,
                latest_rank=it.latest_rank,
                latest_score=it.ref_score,
                level_tags=it.level_tags,
                school_province=it.school_province,
                ref_years_note=it.ref_years_note,
                require_note=it.require_note,
            )
            for it in g.items[:tier_limit]
        ]
        tiers.append(FallbackTier(tier=g.tier, label=g.label, count=len(rows), rows=rows))

    return FallbackResult(
        mode=MODE_EXPERT_RULE,
        province=result.province,
        current_year=result.current_year,
        subject_type=result.subject_type,
        score=result.score,
        rank=result.rank,
        ref_years_used=list(result.ref_years_used),
        tiers=tiers,
        caliber_text=result.caliber_text,
        generated_at=generated_at,
        cache_hit=False,
    )


class FallbackService:
    """降级兜底服务（「专家规则模式」）。

    对外同步 API：``fallback(...)`` → :class:`FallbackResult`。
    """

    def __init__(
        self,
        repo: RecommendRepository,
        *,
        engine: RecommendEngine | None = None,
        use_cache: bool = True,
        dataset_fingerprint: str | None = None,
    ) -> None:
        """
        :param repo: 数据源（与 T08 同形，生产 MySQL / 测试内存）
        :param engine: 可注入引擎（便于测试）；默认用 ``repo`` 新建
        :param use_cache: 是否启用 Redis 缓存（测试可关）
        :param dataset_fingerprint: 数据集指纹，用于缓存键；缺省则退化为版本号占位
        """
        self._repo = repo
        self._engine = engine or RecommendEngine(repo)
        self._use_cache = use_cache
        self._fingerprint = dataset_fingerprint or "v0"

    # ---------- 主入口 ----------

    def fallback(
        self,
        *,
        score: int,
        subject_type: str,
        electives: list[str] | None = None,
        province: str | None = None,
        rank: int | None = None,
        current_year: int | None = None,
        ref_years: list[int] | None = None,
        tier_limit: int = DEFAULT_FALLBACK_LIMIT,
    ) -> FallbackResult:
        """生成降级兜底表。

        入参语义与 T08 ``RecommendEngine.recommend`` **完全一致**（复用同一套校验），
        唯一区别：每档条数默认更少（``DEFAULT_FALLBACK_LIMIT``）。
        """
        province = province or "广东"  # 与 T08 引擎默认值保持一致（唯一演示省份）
        current_year = current_year or settings.current_year
        ref_years = ref_years or settings.ref_year_list
        electives = electives or []

        # 入参校验（直接复用 T08 引擎，避免校验口径双实现）
        RecommendEngine._validate(score, subject_type, rank, ref_years, current_year)
        assert rank is not None  # _validate 已保证

        if tier_limit < 1:
            raise ValidationError(f"兜底表每档条数必须 ≥ 1，收到 {tier_limit!r}")

        cache_key = self._cache_key(
            province=province, subject_type=subject_type, score=score, rank=rank,
            electives=electives, current_year=current_year, ref_years=ref_years,
            tier_limit=tier_limit,
        )

        # ---- 缓存读 ----
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached.cache_hit = True
            return cached

        # ---- 实算：复用 T08 引擎 ----
        result = self._engine.recommend(
            score=score,
            subject_type=subject_type,
            electives=electives,
            province=province,
            rank=rank,
            current_year=current_year,
            ref_years=ref_years,
            tier_limit=tier_limit,
        )

        fb = recommend_result_to_fallback(
            result, tier_limit=tier_limit, generated_at=_now_iso()
        )

        # ---- 缓存写 ----
        self._cache_set(cache_key, fb)
        return fb

    # ---------- 缓存键 ----------

    def _cache_key(
        self,
        *,
        province: str,
        subject_type: str,
        score: int,
        rank: int,
        electives: list[str],
        current_year: int,
        ref_years: list[int],
        tier_limit: int,
    ) -> str:
        """缓存键 = 请求关键参数 + 数据源指纹。

        指纹变化（数据重导 / 增删）→ 键变 → 旧键自然不再命中，无需显式清理。
        """
        raw = json.dumps(
            {
                "province": province,
                "subject_type": subject_type,
                "score": score,
                "rank": rank,
                "electives": sorted(electives),
                "current_year": current_year,
                "ref_years": list(ref_years),
                "tier_limit": tier_limit,
                "ratios": [settings.chong_ratio, settings.wen_ratio, settings.bao_ratio],
                "tier_min_rank_basis": settings.tier_min_rank_basis,
                "fp": self._fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"fallback:{digest}"

    # ---------- Redis 读写（静默降级）----------

    def _cache_get(self, key: str) -> FallbackResult | None:
        if not self._use_cache:
            return None
        try:
            from app.core.redis import get_redis

            payload = _run_sync(lambda: get_redis().get(key))
            if not payload:
                return None
            data = json.loads(payload)
            return FallbackResult.model_validate(data)
        except Exception:  # noqa: BLE001 —— 缓存故障不得影响主流程
            logger.debug("兜底表缓存读取失败，降级直算：%s", key)
            return None

    def _cache_set(self, key: str, result: FallbackResult) -> None:
        if not self._use_cache:
            return
        try:
            from app.core.redis import get_redis

            payload = result.model_dump_json()
            _run_sync(lambda: get_redis().set(key, payload, ex=FALLBACK_CACHE_TTL_SECONDS))
        except Exception:  # noqa: BLE001
            logger.debug("兜底表缓存写入失败（不影响结果）：%s", key)


# ============================================================
# 纯函数辅助
# ============================================================


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_sync(make_coro):
    """委托共享桥接（专用临时客户端，防共享连接池被临时事件循环毒化；见 app.core.redis）。"""
    from app.core.redis import run_sync

    return run_sync(make_coro)


# ============================================================
# 单例（生产）
# ============================================================

_service: FallbackService | None = None


def get_fallback_service() -> FallbackService:
    """生产单例：MySQL 数据源 + T07 数据集指纹（用于缓存失效）。"""
    global _service
    if _service is None:
        from app.services.recommend import SqlRecommendRepository

        fingerprint = _safe_dataset_fingerprint()
        _service = FallbackService(
            SqlRecommendRepository(),
            dataset_fingerprint=fingerprint,
        )
    return _service


def _safe_dataset_fingerprint() -> str:
    """取 T07 位次数据集指纹作为缓存版本；失败则退化占位（不影响功能）。"""
    try:
        from app.services.score import SqlScoreRepository

        return SqlScoreRepository().dataset_version("广东").fingerprint
    except Exception:  # noqa: BLE001
        logger.debug("无法获取数据集指纹，兜底缓存键退化占位")
        return "v0"


def reset_fallback_service() -> None:
    global _service
    _service = None


__all__ = [
    "DEFAULT_FALLBACK_LIMIT",
    "FALLBACK_CACHE_TTL_SECONDS",
    "FallbackRepository",
    "FallbackService",
    "get_fallback_service",
    "recommend_result_to_fallback",
    "reset_fallback_service",
    # 导出异常/数据源别名，便于 api 层与测试统一引用
    "DataMissingError",
]
