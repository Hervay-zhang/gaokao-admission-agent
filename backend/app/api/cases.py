"""T27 · 案例中心 API（脱敏真实案例 + 筛选）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/cases` | 案例列表（可按 科类 / 分数段 / 路径类型 / 叙事分层 筛选） |
| GET | `/api/cases/{slug}` | 案例详情（元数据 + 章节正文） |

红线：案例来自真实数据且脱敏；接口不返回明文分数/位次与任何可识别个人信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.logging import get_logger
from app.services.cases import get_case_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("", summary="案例列表（可筛选）")
async def list_cases(
    subject_type: str | None = Query(None, description="科类：物理 / 历史"),
    score_band: str | None = Query(None, description="分数段，如 600-609"),
    path_type: str | None = Query(None, description="路径类型：进取型 / 稳妥型 / 保底型 / 滑档预警"),
    narrative_tier: str | None = Query(None, description="叙事分层：basic / deep"),
) -> dict:
    return get_case_service().list_cases(
        subject_type=subject_type,
        score_band=score_band,
        path_type=path_type,
        narrative_tier=narrative_tier,
    )


@router.get("/{slug}", summary="案例详情")
async def get_case(slug: str) -> dict:
    return get_case_service().get_case(slug)
