"""T24 · 院校库检索 / 详情 / 对比接口。

- ``GET /api/schools``          —— 列表检索（关键词 + 层次 + 地域 + 分页）
- ``GET /api/schools/meta``     —— 筛选项枚举（院校所在省份）
- ``GET /api/schools/{code}``   —— 院校详情（按专业组聚合历年投档行）
- ``GET /api/schools/compare``  —— ≤3 所院校对比

红线：数据全部来自库内；无数据维度显式标注；筛选条件由前端进 URL（后端无状态）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.schools import CompareResponse, UniversityDetail, UniversityPage
from app.services.schools import get_school_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/schools", tags=["schools"])


@router.get("", response_model=UniversityPage, summary="院校列表检索（筛选 + 分页）")
async def list_schools(
    keyword: str = Query("", description="关键词（匹配名称 / 代码 / 基础校名）"),
    level: str | None = Query(None, description="层次：本科 / 专科"),
    tag: str | None = Query(None, description="层次标签：985 / 211 / 双一流"),
    uni_province: str | None = Query(None, description="院校所在省份"),
    province: str = Query("广东", description="生源省份（取该省投档数据）"),
    subject_type: str | None = Query(None, description="科类：物理 / 历史"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
) -> UniversityPage:
    service = get_school_service()
    data = service.search(
        province_query=province, subject_type=subject_type, level=level, uni_province=uni_province,
        keyword=keyword or None, tag=tag, page=page, page_size=page_size,
    )
    return UniversityPage(**data)


@router.get("/meta", summary="院校库筛选项枚举")
async def schools_meta() -> dict:
    service = get_school_service()
    return {
        "uni_provinces": service.provinces(),
        "levels": ["本科", "专科"],
        "tags": ["985", "211", "双一流"],
        "current_year": settings.current_year,
        "ref_years": settings.ref_year_list,
    }


@router.get("/compare", response_model=CompareResponse, summary="院校对比（≤3 所）")
async def compare_schools(
    codes: str = Query(..., description="院校代码，逗号分隔（≤3 个）"),
    province: str = Query("广东", description="生源省份"),
    subject_type: str | None = Query(None, description="科类（可选）"),
) -> CompareResponse:
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    service = get_school_service()
    data = service.compare(code_list, province=province, subject_type=subject_type or "")
    return CompareResponse(**data)


@router.get("/{code}", response_model=UniversityDetail, summary="院校详情（专业组历年投档）")
async def school_detail(
    code: str,
    province: str = Query("广东", description="生源省份"),
    subject_type: str = Query("", description="科类（可选）"),
) -> UniversityDetail:
    service = get_school_service()
    data = service.detail(code, province=province, subject_type=subject_type)
    return UniversityDetail(**data)
