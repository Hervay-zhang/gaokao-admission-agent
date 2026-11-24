"""T25 · AI 报告接口（三档 + 风险解释 + 家长版）。

- ``GET /api/report`` —— 由 T08 推荐引擎产出的完整报告（含解释 / 风险 / 家长版 / 免责）

设计要点
--------
- **解释全部来自数据**（T08 规则模板拼接，不调 LLM）：`explanation` / `risk_explanation` /
  `parent_summary` / 每条 `evidence` 均为真实投档数据派生；
- 报告 `tier`（basic/deep）字段预留展示位（P0 不做付费）；
- 免责声明随结果返回（`disclaimer`，红线）；
- 位次可由 T07 引擎现算（口径唯一），或由前端传入（测评已算过）；
- **Redis 结果缓存（T30）**：键内嵌**数据集指纹 + 年份权重**（数据/口径变更 → 键变自然失效，
  TTL 1 天兜底）；Redis 故障**静默降级直算**（缓存绝不影响主流程）。单次冷算 ≈ 0.6s，
  热查询 ≈ 几十 ms——高并发下避免单 worker 事件循环被重复冷算排队拖垮（T30 压测定稿）。

红线：解释禁止无依据话术；无数据院校显式标注（`ref_years_note` / `require_note`）；
风险解释禁概率数字（由 T08 引擎保证 + 本层断言兜底）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_account
from app.core.config import settings
from app.core.exceptions import DataMissingError
from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.recommend import RecommendResult
from app.services.profile import rank_bucket, score_bucket
from app.services.recommend import get_recommend_engine
from app.services.report_history import (
    get_report_history,
    list_report_history,
    save_report_history,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/report", tags=["report"])

# 报告档位（P0 仅 basic；deep 预留付费位，不做）
REPORT_TIERS = ("basic", "deep")

_PROBABILITY_WORDS = ("录取概率", "概率", "%", "百分之")


class ReportHistoryCreateRequest(BaseModel):
    profile_id: int | None = Field(default=None, ge=1)
    title: str = Field(default="冲稳保路径报告", max_length=128)
    province: str = Field(default="", max_length=32)
    subject_type: str = Field(default="", max_length=16)
    score: int | None = Field(default=None, ge=0, le=750)
    rank: int | None = Field(default=None, ge=1)
    current_year: int = Field(default=settings.current_year, ge=2000, le=2100)
    tier_counts: dict[str, int] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1000)


@router.post("/history", summary="保存当前账号的报告历史")
async def create_report_history(
    request: Request,
    payload: ReportHistoryCreateRequest = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    item = save_report_history(
        db,
        owner_username=account.username,
        profile_id=payload.profile_id,
        title=payload.title,
        province=payload.province,
        subject_type=payload.subject_type,
        score_bucket=score_bucket(payload.score) if payload.score is not None else None,
        rank_bucket=rank_bucket(payload.rank) if payload.rank is not None else None,
        current_year=payload.current_year,
        tier_counts=payload.tier_counts,
        summary=payload.summary,
    )
    return {"ok": True, "item": item}


@router.get("/history", summary="当前账号报告历史")
async def report_history_list(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    items = list_report_history(db, account.username)
    return {"total": len(items), "items": items}


@router.get("/history/{snapshot_id}", summary="报告历史详情")
async def report_history_detail(
    snapshot_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_report_history(db, account.username, snapshot_id)


@router.get("", response_model=RecommendResult, summary="AI 报告（冲稳保三档 + 解释 + 家长版）")
async def build_report(
    score: int = Query(..., ge=0, le=750, description="高考分数（0–750）"),
    subject_type: str = Query(..., description="科类：物理 / 历史"),
    electives: str = Query("", description="再选科目，逗号分隔"),
    province: str = Query("广东", description="省份"),
    rank: int | None = Query(None, ge=1, description="考生位次（不传则由 T07 引擎按分数换算）"),
    current_year: int = Query(settings.current_year, description="报考年份"),
    tier_limit: int = Query(settings.tier_limit, ge=1, le=20, description="每档条数"),
    report_tier: str = Query("basic", description="报告档位：basic / deep（P0 仅 basic）"),
) -> RecommendResult:
    """生成报告。

    - ``rank`` 缺省时用 T07 引擎换算（**口径唯一**）；
    - 引擎产出的解释/风险/家长版**均为数据驱动**；
    - 风险解释做概率数字兜底断言（防御性，引擎本已保证）。
    """
    if report_tier not in REPORT_TIERS:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"report_tier 需为 {REPORT_TIERS} 之一", detail={"code": "TIER_INVALID"})

    effective_rank = rank
    if effective_rank is None:
        effective_rank = _rank_from_score(
            score=score, subject_type=subject_type, province=province, current_year=current_year
        )

    elective_list = [e.strip() for e in electives.split(",") if e.strip()]

    # ---- Redis 结果缓存（T30）：键 = 参数 + 数据集指纹 + 年份权重 ----
    cache_key = await _cache_key(
        score=score, subject_type=subject_type, electives=elective_list, province=province,
        rank=effective_rank, current_year=current_year, tier_limit=tier_limit,
    )
    cached = await _cache_get(cache_key)
    if cached is not None:
        logger.info("报告缓存命中 key=%s", cache_key)
        return cached

    result = get_recommend_engine().recommend(
        score=score,
        subject_type=subject_type,
        electives=elective_list,
        province=province,
        rank=effective_rank,
        current_year=current_year,
        tier_limit=tier_limit,
    )

    # 红线兜底：风险解释不得出现概率数字（防御性断言，异常即暴露而非静默）
    for word in _PROBABILITY_WORDS:
        if word in result.risk_explanation:
            logger.error("风险解释出现概率数字（引擎缺陷）：%s", result.risk_explanation)
            from app.core.exceptions import AppError

            raise AppError(
                "风险解释生成异常（含概率数字），已中止返回",
                detail={"code": "RISK_TEXT_INVALID"},
            )

    logger.info(
        "报告生成 省份=%s 科类=%s 分数区间=%s 位次档=%s 档位=%s 总条数=%s",
        province, subject_type, f"{score // 10 * 10}-{score // 10 * 10 + 9}",
        f"{effective_rank // 1000}k", report_tier, result.total_count,
    )
    await _cache_set(cache_key, result)
    return result


# ============================================================
#  结果缓存（Redis，静默降级；数据集指纹 + 年份权重入键）
# ============================================================

REPORT_CACHE_TTL_SECONDS = 24 * 3600


async def _dataset_fingerprint(province: str) -> str:
    """T07 数据集指纹（数据变更 → 指纹变 → 旧键自然失效）。

    指纹是一次 GROUP BY 聚合（score_segments → 6 组摘要）。同步 SQL 会阻塞事件循环，
    放线程执行（T30 压测：20 并发下这是热路径上最大的阻塞源）。
    """
    def _load() -> str:
        try:
            from app.services.score import SqlScoreRepository

            return SqlScoreRepository().dataset_version(province).fingerprint
        except Exception:  # noqa: BLE001 —— 指纹取不到退化占位，不影响功能
            return "v0"

    fp = await asyncio.to_thread(_load)
    if fp == "v0":
        logger.debug("报告缓存指纹退化占位（province=%s）", province)
    return fp


async def _cache_key(*, score: int, subject_type: str, electives: list[str], province: str,
                     rank: int, current_year: int, tier_limit: int) -> str:
    """缓存键：请求参数 + 数据集指纹 + 年份权重（口径变更同样使旧键失效）。"""
    raw = json.dumps({
        "fp": await _dataset_fingerprint(province),
        "w": settings.year_weight_list,
        "ratios": [settings.chong_ratio, settings.wen_ratio, settings.bao_ratio],
        "tier_min_rank_basis": settings.tier_min_rank_basis,
        "score": score, "subject": subject_type, "ele": sorted(electives),
        "province": province, "rank": rank, "year": current_year, "limit": tier_limit,
    }, sort_keys=True, ensure_ascii=False)
    return "report:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


async def _cache_get(key: str) -> RecommendResult | None:
    try:
        from app.core.redis import get_redis

        payload = await get_redis().get(key)
        if not payload:
            return None
        # 大载荷反序列化 + pydantic 校验放线程，避免阻塞事件循环（T30 压测定稿）
        return await asyncio.to_thread(lambda: RecommendResult.model_validate(json.loads(payload)))
    except Exception:  # noqa: BLE001 —— 缓存故障不得影响主流程
        logger.debug("报告缓存读取失败（降级直算）：%s", key)
        return None


async def _cache_set(key: str, result: RecommendResult) -> None:
    try:
        from app.core.redis import get_redis

        payload = await asyncio.to_thread(
            lambda: json.dumps(result.model_dump(), ensure_ascii=False, default=str)
        )
        await get_redis().set(key, payload, ex=REPORT_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("报告缓存写入失败（不影响结果）：%s", key)


def _rank_from_score(*, score: int, subject_type: str, province: str, current_year: int) -> int:
    """用 T07 位次引擎换算（唯一实现，不重复造轮子）。"""
    from app.services.score import get_score_engine

    res = get_score_engine().query_rank(current_year, province, subject_type, score)
    if res.rank is None:  # pragma: no cover
        raise DataMissingError(
            f"无法换算 {current_year} 年 {province} {subject_type} {score} 分的位次",
            detail={"code": "RANK_UNAVAILABLE"},
        )
    return res.rank
