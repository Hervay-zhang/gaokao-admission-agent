"""T24 · 院校库检索与对比 Schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UniversityItem(BaseModel):
    """列表项（院校粒度，不含专业组）。"""

    code: str
    name: str
    base_name: str = ""
    level: str = "本科"
    province: str | None = None
    school_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    meta_status: str = "待补"
    admission_url: str = ""
    admission_url_kind: str = "chsi"
    admission_url_status: str = "待核"
    admission_url_updated_at: str = ""
    latest_min_rank: int | None = Field(default=None, description="最近一年该院校最低投档位次（跨组取最优）")
    latest_year: int | None = None
    group_count: int = Field(default=0, description="该院校在筛选条件下的专业组数")


class UniversityPage(BaseModel):
    """分页列表响应。"""

    total: int
    page: int
    page_size: int
    items: list[UniversityItem]
    caliber_text: str = ""


class GroupLine(BaseModel):
    """专业组某年投档行。"""

    year: int
    group_code: int
    plan_count: int | None = None
    admitted_count: int | None = None
    min_score: int | None = None
    min_rank: int | None = None
    subject_requirement: dict = Field(default_factory=dict)


class GroupDetail(BaseModel):
    """专业组（含历年投档行 + 选科要求）。"""

    group_code: int
    subject_type: str
    requirement_text: str = ""
    require_status: str = "待补"
    lines: list[GroupLine] = Field(default_factory=list)


class UniversityDetail(BaseModel):
    """院校详情：基本信息 + 按专业组聚合的历年数据。"""

    code: str
    name: str
    base_name: str = ""
    level: str = "本科"
    province: str | None = None
    school_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    meta_status: str = "待补"
    admission_url: str = ""
    admission_url_kind: str = "chsi"
    admission_url_status: str = "待核"
    admission_url_updated_at: str = ""
    province_query: str = ""
    subject_type: str = ""
    years: list[int] = Field(default_factory=list)
    groups: list[GroupDetail] = Field(default_factory=list)
    trend_note: str = ""
    caliber_text: str = ""


class CompareRow(BaseModel):
    """对比表中的一行（某院校某指标）。"""

    code: str
    name: str
    tags: list[str] = Field(default_factory=list)
    province: str | None = None
    school_type: str | None = None
    meta_status: str = "待补"
    latest_min_rank: int | None = None
    min_score: int | None = None
    group_count: int = 0
    years: list[int] = Field(default_factory=list)
    rank_trend: list[int | None] = Field(default_factory=list, description="按 years 顺序的历年最低位次")


class CompareResponse(BaseModel):
    """院校对比（≤3 所）。"""

    years: list[int] = Field(default_factory=list)
    rows: list[CompareRow] = Field(default_factory=list)
    caliber_text: str = ""
