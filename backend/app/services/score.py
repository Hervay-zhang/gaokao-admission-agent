"""位次换算引擎（T07）。

职责
----
把「分数」与「位次」在**同一年内**双向换算，并支持**跨年等位分**映射，
为 T08 的「冲稳保」推荐提供统一口径的位次基准。

两个核心操作
------------
1. ``query_rank(year, province, subject_type, score)``  分数 → 位次（当年）
2. ``equiv_score(year, province, subject_type, rank)``  位次 → 等位分（跨年参考）

口径（严格遵循 ``docs/升学志愿助手-data_caliber.md`` §二、§六）
----------------------------------------------------------------
- 一分一段表的 ``rank`` = 官方「本科（含本科加分）累计人数」= 该分数及以上考生总数
  = 拿到该分数考生的**位次上限**；同一分数内部名次差异公开数据无法得知，
  故**同分并列统一取累计值**，本引擎不做任何二次拆分。
- **分数高于表中最高段**（表首行，如 2026 物理 699）：官方未细分，取该最高段累计值
  作为位次（= 公开数据上限），并标注 ``capped``，**不外推**。
- **分数 < 100**：官方表下限为 100 分，返回「无细分数据」错误，**不虚构位次**。
- **位次 → 等位分**：在该年一分一段表内找 ``rank`` 落在 ``[R_low, R_high]`` 的分数区间，
  区间内做**线性插值**（见 ``_interp_score`` 注释）。位次超出该年表范围时返回最近边界值
  并标注 ``capped``，**不外推**。

缓存
----
- 键：``rank:{province}:{year}:{subject}:{score}`` / ``equiv:{province}:{year}:{subject}:{rank}``
- **失效策略**：键中内嵌 ``dataset_version``（由最大年份 + 行数指纹构成），数据版本变化后
  旧键自然不再命中；同时设 TTL（默认 1 天）兜底。Redis 不可用时静默降级为直查，
  **不阻塞**业务（缓存是优化，不是依赖）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import DataMissingError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 官方一分一段表下限（< 该分数返回「无细分数据」，不虚构）
MIN_SCORE = 100
# 高考满分上限（用于入参粗校验）
MAX_SCORE = 750
# 缓存 TTL（秒）—— 1 天。真正的失效靠 dataset_version，TTL 仅兜底。
CACHE_TTL_SECONDS = 86400


# ============================================================
# 数据结构
# ============================================================


@dataclass(frozen=True)
class RankResult:
    """分数 → 位次 的结果。"""

    year: int
    province: str
    subject_type: str
    score: int
    rank: int
    # True 表示分数高于表中最高段，rank 取自最高段累计值（公开上限，非精确）
    capped: bool = False
    # 该组数据的最高分段（供提示文案用）
    top_score: int = 0

    @property
    def note(self) -> str:
        if self.capped:
            return f"分数高于 {self.year} 年最高公布分段（{self.top_score} 分），位次取该段累计值（公开数据上限）"
        return ""


@dataclass(frozen=True)
class EquivScoreResult:
    """位次 → 等位分 的结果。"""

    year: int
    province: str
    subject_type: str
    rank: int
    score: float
    # True 表示位次超出该年表范围，score 取最近边界值（不外推）
    capped: bool = False
    # 位次落在该区间对应的两个相邻分数点（用于可解释展示）
    low_score: int = 0
    high_score: int = 0

    @property
    def note(self) -> str:
        if self.capped:
            return f"位次超出 {self.year} 年可换算范围，分数取最近边界值（不对外推）"
        return ""


@dataclass
class DatasetVersion:
    """数据集版本指纹（用于缓存键与失效）。

    由「各 (year, subject) 组的最小/最大分数 + 行数」摘要而来：
    数据一旦重导或增删，指纹变化 → 旧缓存键自然不再命中。
    """

    fingerprint: str = "v0"
    years: list[int] = field(default_factory=list)


# ============================================================
# 数据源协议（可注入：生产走 MySQL，测试走内存）
# ============================================================


@runtime_checkable
class ScoreRepository(Protocol):
    """位次换算所需的最小数据访问接口。"""

    def rank_at(self, province: str, year: int, subject_type: str, score: int) -> int | None:
        """精确查询某分数对应的累计人数（位次上限）；无该分数行返回 None。"""
        ...

    def list_segments(self, province: str, year: int, subject_type: str) -> list[tuple[int, int]]:
        """返回该组的 ``[(score, rank), ...]``，**按分数降序**（表本身就降序）。"""
        ...

    def dataset_version(self, province: str) -> DatasetVersion:
        """数据集版本指纹。"""
        ...


# ============================================================
# 内存实现（测试 / 离线）
# ============================================================


class InMemoryScoreRepository:
    """内存数据源：直接吃 ``[(year, subject_type, score, rank)]``。

    用于单元测试与离线联调，**不依赖 MySQL**。
    """

    def __init__(self, rows: list[tuple[int, str, int, int]], province: str = "广东") -> None:
        self.province = province
        # 按 (year, subject) 分组，组内按分数降序（与官方表一致）
        self._groups: dict[tuple[int, str], list[tuple[int, int]]] = {}
        for year, subject, score, rank in rows:
            self._groups.setdefault((year, subject), []).append((score, rank))
        for key in self._groups:
            self._groups[key].sort(key=lambda t: t[0], reverse=True)

    def rank_at(self, province: str, year: int, subject_type: str, score: int) -> int | None:
        if province != self.province:
            return None  # 未载入该省数据 → 上层按「无数据」处理（与 Sql 行为一致）
        for s, r in self._groups.get((year, subject_type), []):
            if s == score:
                return r
        return None

    def list_segments(self, province: str, year: int, subject_type: str) -> list[tuple[int, int]]:
        if province != self.province:
            return []
        return self._groups.get((year, subject_type), [])

    def dataset_version(self, province: str) -> DatasetVersion:
        return _fingerprint(self._groups)

    @classmethod
    def from_csv(cls, path: str, province: str = "广东") -> InMemoryScoreRepository:
        """从 ``score_segments.csv`` 载入（utf-8-sig）。"""
        import csv

        rows: list[tuple[int, str, int, int]] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            for rec in csv.DictReader(f):
                if rec["province"] != province:
                    continue
                rows.append((int(rec["year"]), rec["subject_type"], int(rec["score"]), int(rec["rank"])))
        return cls(rows, province=province)


def _fingerprint(groups: dict[tuple[int, str], list[tuple[int, int]]]) -> DatasetVersion:
    """从分组数据算版本指纹（内容摘要，稳定且对增删敏感）。"""
    parts: list[str] = []
    years: set[int] = set()
    for (year, subject), segs in sorted(groups.items()):
        years.add(year)
        if segs:
            top_s, _ = segs[0]
            bot_s, bot_r = segs[-1]
            parts.append(f"{year}:{subject}:{len(segs)}:{top_s}:{bot_s}:{bot_r}")
        else:
            parts.append(f"{year}:{subject}:0")
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return DatasetVersion(fingerprint=digest, years=sorted(years))


# ============================================================
# 生产实现（MySQL）
# ============================================================


class SqlScoreRepository:
    """MySQL 数据源（每方法短连接会话，避免长事务）。"""

    def __init__(self) -> None:
        # 延迟导入，避免测试环境在无 MySQL 驱动配置时也硬依赖
        from app.db.session import SessionLocal

        self._SessionLocal = SessionLocal

    def rank_at(self, province: str, year: int, subject_type: str, score: int) -> int | None:
        from sqlalchemy import select

        from app.db.models import ScoreSegment

        with self._SessionLocal() as db:
            stmt = select(ScoreSegment.rank).where(
                ScoreSegment.province == province,
                ScoreSegment.year == year,
                ScoreSegment.subject_type == subject_type,
                ScoreSegment.score == score,
            )
            return db.execute(stmt).scalar_one_or_none()

    def list_segments(self, province: str, year: int, subject_type: str) -> list[tuple[int, int]]:
        from sqlalchemy import select

        from app.db.models import ScoreSegment

        with self._SessionLocal() as db:
            stmt = (
                select(ScoreSegment.score, ScoreSegment.rank)
                .where(
                    ScoreSegment.province == province,
                    ScoreSegment.year == year,
                    ScoreSegment.subject_type == subject_type,
                )
                .order_by(ScoreSegment.score.desc())
            )
            return [(int(s), int(r)) for s, r in db.execute(stmt).all()]

    def dataset_version(self, province: str) -> DatasetVersion:
        """用「分组行数 + 分数边界」摘要做指纹（避免全表拉取）。"""
        from sqlalchemy import func, select

        from app.db.models import ScoreSegment

        with self._SessionLocal() as db:
            stmt = (
                select(
                    ScoreSegment.year,
                    ScoreSegment.subject_type,
                    func.count().label("n"),
                    func.max(ScoreSegment.score).label("hi"),
                    func.min(ScoreSegment.score).label("lo"),
                    func.max(ScoreSegment.rank).label("max_rank"),
                )
                .where(ScoreSegment.province == province)
                .group_by(ScoreSegment.year, ScoreSegment.subject_type)
            )
            parts: list[str] = []
            years: set[int] = set()
            for year, subject, n, hi, lo, max_rank in db.execute(stmt).all():
                years.add(int(year))
                parts.append(f"{year}:{subject}:{n}:{hi}:{lo}:{max_rank}")
            digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:12]
            return DatasetVersion(fingerprint=digest, years=sorted(years))


# ============================================================
# 缓存（Redis，可选依赖）
# ============================================================


class _Cache:
    """极简缓存封装：Redis 不可用时全部降级为 miss，不抛异常。"""

    def __init__(self, version: str) -> None:
        self.version = version

    def _key(self, kind: str, province: str, year: int, subject: str, val: int) -> str:
        return f"{kind}:{province}:{self.version}:{year}:{subject}:{val}"

    def get(self, kind: str, province: str, year: int, subject: str, val: int) -> str | None:
        try:
            from app.core.redis import get_redis

            return _run_sync(lambda: get_redis().get(self._key(kind, province, year, subject, val)))
        except Exception:  # noqa: BLE001 —— 缓存故障不得影响主流程
            return None

    def set(self, kind: str, province: str, year: int, subject: str, val: int, payload: str) -> None:
        try:
            from app.core.redis import get_redis

            _run_sync(lambda: get_redis().set(self._key(kind, province, year, subject, val), payload, ex=CACHE_TTL_SECONDS))
        except Exception:  # noqa: BLE001
            pass


def _run_sync(make_coro):
    """委托共享桥接（专用临时客户端，防共享连接池被临时事件循环毒化；见 app.core.redis）。"""
    from app.core.redis import run_sync

    return run_sync(make_coro)


# ============================================================
# 引擎
# ============================================================


class ScoreEngine:
    """位次换算引擎（对外统一入口）。"""

    def __init__(self, repo: ScoreRepository, *, use_cache: bool = True) -> None:
        self._repo = repo
        self._use_cache = use_cache
        self._version: str | None = None

    # ---------- 版本指纹 ----------

    def _version_str(self) -> str:
        if self._version is None:
            self._version = self._repo.dataset_version(settings.province).fingerprint
        return self._version

    # ---------- 1. 分数 → 位次 ----------

    def query_rank(self, year: int, province: str, subject_type: str, score: int) -> RankResult:
        """分数 → 当年位次（位次上限）。

        - 分数 < 100：抛 ``DataMissingError``，不虚构；
        - 分数高于表中最高段：取最高段累计值，``capped=True``；
        - 其余：精确命中该分数行。
        """
        self._validate_common(province, subject_type)
        if not isinstance(score, int) or score < 0:
            raise ValidationError(f"分数必须为非负整数，收到 {score!r}")
        if score > MAX_SCORE:
            raise ValidationError(f"分数超出满分上限 {MAX_SCORE}，收到 {score}")
        if score < MIN_SCORE:
            raise DataMissingError(
                f"{year} 年 {province} {subject_type} 一分一段表下限为 {MIN_SCORE} 分，"
                f"{score} 分无细分数据（不虚构位次）",
                detail={"code": "BELOW_MIN_SCORE", "min_score": MIN_SCORE},
            )

        # 缓存
        cache = _Cache(self._version_str()) if self._use_cache else None
        if cache:
            hit = cache.get("rank", province, year, subject_type, score)
            if hit is not None:
                return _rank_from_payload(hit, year, province, subject_type, score)

        # 精确命中
        rank = self._repo.rank_at(province, year, subject_type, score)
        if rank is not None:
            res = RankResult(year, province, subject_type, score, rank)
            if cache:
                cache.set("rank", province, year, subject_type, score, _rank_to_payload(res))
            return res

        # 未精确命中 → 判断是否高于最高段
        segs = self._repo.list_segments(province, year, subject_type)
        if not segs:
            raise DataMissingError(
                f"库内无 {year} 年 {province} {subject_type} 一分一段数据",
                detail={"code": "NO_SEGMENT_DATA", "year": year, "subject_type": subject_type},
            )
        top_score, top_rank = segs[0]
        if score >= top_score:
            # 高于（或等于）最高公布分段 → 取上限，不外推
            res = RankResult(year, province, subject_type, score, top_rank, capped=True, top_score=top_score)
            if cache:
                cache.set("rank", province, year, subject_type, score, _rank_to_payload(res))
            return res

        # 落在 [100, top_score) 区间内却缺行 → 数据缺口，显式报错（绝不插值补造分数→位次）
        raise DataMissingError(
            f"{year} 年 {province} {subject_type} 表中缺 {score} 分行（数据缺口，不补造）",
            detail={"code": "SCORE_GAP", "year": year, "score": score},
        )

    # ---------- 2. 位次 → 等位分 ----------

    def equiv_score(self, year: int, province: str, subject_type: str, rank: int) -> EquivScoreResult:
        """位次 → 某年等位分（区间线性插值）。

        - ``rank`` 小于表首（最优）段累计 → 取最高分（``capped``）；
        - ``rank`` 大于表末累计 → 取最低分（``capped``）；
        - 其余：找到 ``rank`` 落在的相邻两点 ``(hi_score, hi_rank) / (lo_score, lo_rank)``，
          其中 ``hi_rank <= rank <= lo_rank``（分数越高累计越小），线性插值。
        """
        self._validate_common(province, subject_type)
        if not isinstance(rank, int) or rank < 1:
            raise ValidationError(f"位次必须为正整数，收到 {rank!r}")

        cache = _Cache(self._version_str()) if self._use_cache else None
        if cache:
            hit = cache.get("equiv", province, year, subject_type, rank)
            if hit is not None:
                return _equiv_from_payload(hit, year, province, subject_type, rank)

        segs = self._repo.list_segments(province, year, subject_type)  # 分数降序
        if not segs:
            raise DataMissingError(
                f"库内无 {year} 年 {province} {subject_type} 一分一段数据",
                detail={"code": "NO_SEGMENT_DATA", "year": year, "subject_type": subject_type},
            )

        res = _interp_score(segs, year, province, subject_type, rank)
        if cache:
            cache.set("equiv", province, year, subject_type, rank, _equiv_to_payload(res))
        return res

    # ---------- 3. 批量等位分（跨年）----------

    def equiv_scores_across_years(
        self, rank: int, years: list[int], province: str, subject_type: str
    ) -> list[EquivScoreResult]:
        """把同一 ``rank`` 映射到多个年份的等位分（供报告展示「2026 位次 → 各年等位分」）。"""
        return [self.equiv_score(y, province, subject_type, rank) for y in years]

    # ---------- 内部校验 ----------

    @staticmethod
    def _validate_common(province: str, subject_type: str) -> None:
        if not province:
            raise ValidationError("省份不能为空")
        if subject_type not in ("物理", "历史"):
            raise ValidationError(f"科类必须是「物理」或「历史」，收到 {subject_type!r}")


# ============================================================
# 插值（纯函数，便于单测）
# ============================================================


def _interp_score(
    segs: list[tuple[int, int]], year: int, province: str, subject_type: str, rank: int
) -> EquivScoreResult:
    """在一分一段表（分数降序）中按 rank 线性插值求分数。

    插值口径
    --------
    取相邻两点 ``A=(score_hi, rank_hi)`` 与 ``B=(score_lo, rank_lo)``，满足
    ``rank_hi <= rank <= rank_lo``（分数越高累计人数越小）。假定区间内
    「分数 ↔ 累计人数」近似线性，则：

        score = score_hi + (score_hi - score_lo) * (rank - rank_hi) / (rank_lo - rank_hi)

    边界：``rank_lo == rank_hi``（零人段，相邻同累计）时直接取 ``score_hi``（避免除零）。
    ``rank`` 小于表首累计 → 取最高分；大于表末累计 → 取最低分；两者均标 ``capped``。
    """
    hi_score, hi_rank = segs[0]
    lo_score, lo_rank = segs[-1]

    # 优于最高段 → 取最高分
    if rank <= hi_rank:
        return EquivScoreResult(year, province, subject_type, rank, float(hi_score), capped=True,
                                low_score=hi_score, high_score=hi_score)
    # 低于最低段 → 取最低分
    if rank >= lo_rank:
        return EquivScoreResult(year, province, subject_type, rank, float(lo_score), capped=True,
                                low_score=lo_score, high_score=lo_score)

    for i in range(len(segs) - 1):
        s_hi, r_hi = segs[i]
        s_lo, r_lo = segs[i + 1]
        if r_hi <= rank <= r_lo:
            if r_lo == r_hi:
                return EquivScoreResult(year, province, subject_type, rank, float(s_hi),
                                        low_score=s_lo, high_score=s_hi)
            ratio = (rank - r_hi) / (r_lo - r_hi)
            # 修正（T23 发现的真实缺陷）：rank 越大 → 分数越低（段内从 s_hi 递减到 s_lo）。
            # 原公式 ``s_hi + (s_hi - s_lo) * ratio`` 方向反了（rank 增大反而加分，
            # 被 1 分粒度与宽松断言掩盖，段内最多偏 2 分）。
            score = s_hi - (s_hi - s_lo) * ratio
            return EquivScoreResult(year, province, subject_type, rank, round(score, 1),
                                    low_score=s_lo, high_score=s_hi)

    # 理论上不可达（rank 必落在 [hi_rank, lo_rank] 内）
    raise DataMissingError(  # pragma: no cover
        f"{year} 年 {province} {subject_type} 表内无法定位位次 {rank}",
        detail={"code": "RANK_NOT_LOCATED", "rank": rank},
    )


# ============================================================
# 缓存载荷编解码（用竖线分隔的极简格式，避免 JSON 体积/精度问题）
# ============================================================


def _rank_to_payload(r: RankResult) -> str:
    return f"{r.rank}|{int(r.capped)}|{r.top_score}"


def _rank_from_payload(s: str, year: int, province: str, subject_type: str, score: int) -> RankResult:
    """还原缓存命中结果（键里已含 year/province/subject/score，此处回填）。"""
    rank_s, capped_s, top_s = s.split("|")
    return RankResult(
        year=year,
        province=province,
        subject_type=subject_type,
        score=score,
        rank=int(rank_s),
        capped=capped_s == "1",
        top_score=int(top_s),
    )


def _equiv_to_payload(r: EquivScoreResult) -> str:
    return f"{r.score}|{int(r.capped)}|{r.low_score}|{r.high_score}"


def _equiv_from_payload(s: str, year: int, province: str, subject_type: str, rank: int) -> EquivScoreResult:
    score_s, capped_s, lo_s, hi_s = s.split("|")
    return EquivScoreResult(
        year=year,
        province=province,
        subject_type=subject_type,
        rank=rank,
        score=float(score_s),
        capped=capped_s == "1",
        low_score=int(lo_s),
        high_score=int(hi_s),
    )


# ============================================================
# 单例（生产）
# ============================================================

_engine: ScoreEngine | None = None


def get_score_engine() -> ScoreEngine:
    """生产用引擎单例（MySQL 数据源 + Redis 缓存）。"""
    global _engine
    if _engine is None:
        _engine = ScoreEngine(SqlScoreRepository(), use_cache=True)
    return _engine


def reset_score_engine() -> None:
    """重置单例（测试 / 数据版本切换后调用）。"""
    global _engine
    _engine = None
