"""推荐降级兜底接口（T11）。

- ``GET /api/recommend/fallback`` → 「专家规则模式」兜底表（**轻量、无流式、无 LLM**）

该接口是 SSE 流式对话连续失败时的**降级出口**（PRD §5.3）：即使 LLM 完全不可用，
本接口也**只走本地数据 + 规则算法**，保证任何时候都能给用户一份可用的「冲稳保」精简表。

设计要点
--------
- **同步、短响应**：不走 SSE（降级场景本就因为流式失败才进来），一次性返回 JSON；
- **位次可传可算**：``rank`` 由上位（T07 位次引擎）换算；若只给 ``score``，
  本接口会用 T07 引擎现算位次（**口径唯一，不重复实现**）；
- **无解释字段**：返回结构见 ``schemas/fallback.py``，**不含 LLM 解释**，禁止伪造。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.exceptions import DataMissingError
from app.core.logging import get_logger
from app.schemas.fallback import FallbackResult
from app.services.fallback import get_fallback_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/recommend", tags=["recommend"])


@router.get(
    "/fallback",
    response_model=FallbackResult,
    summary="冲稳保降级兜底表（专家规则模式）",
)
async def recommend_fallback(
    score: int = Query(..., ge=0, le=750, description="高考分数（0–750）"),
    subject_type: str = Query(..., description="科类：物理 / 历史"),
    electives: str = Query("", description="再选科目，逗号分隔（如「化学,生物」）"),
    province: str = Query("广东", description="省份（当前仅演示广东）"),
    rank: int | None = Query(None, ge=1, description="考生位次（可选；不传则由 T07 引擎按分数现算）"),
    current_year: int = Query(settings.current_year, description="报考年份"),
    tier_limit: int = Query(6, ge=1, le=20, description="每档条数（兜底表求快，默认 6）"),
) -> FallbackResult:
    """返回「专家规则模式」兜底表（不依赖 LLM）。

    - 若只给 ``score`` 不给 ``rank``：用 T07 位次引擎现算（口径唯一）；
    - 数据来源与正常推荐**完全一致**（同一套 T08 引擎），仅**丢弃解释字段**。
    """
    effective_rank = rank
    if effective_rank is None:
        effective_rank = _rank_from_score(score=score, subject_type=subject_type,
                                          province=province, current_year=current_year)

    elective_list = [e.strip() for e in electives.split(",") if e.strip()]

    service = get_fallback_service()
    return service.fallback(
        score=score,
        subject_type=subject_type,
        electives=elective_list,
        province=province,
        rank=effective_rank,
        current_year=current_year,
        tier_limit=tier_limit,
    )


def _rank_from_score(*, score: int, subject_type: str, province: str, current_year: int) -> int:
    """用 T07 位次引擎把分数换算成位次（**口径唯一，不重复实现**）。"""
    from app.services.score import get_score_engine

    res = get_score_engine().query_rank(current_year, province, subject_type, score)
    if res.rank is None:  # pragma: no cover - 引擎保证非空
        raise DataMissingError(
            f"无法换算 {current_year} 年 {province} {subject_type} {score} 分的位次",
            detail={"code": "RANK_UNAVAILABLE"},
        )
    return res.rank
