"""T23 · 画像测评与档案 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ProfileCreateRequest(BaseModel):
    """POST /api/profile 请求体（画像测评表单提交）。"""

    province: str = Field(default="广东", max_length=16, description="省份（决定高考模式，不允许手选模式）")
    current_year: int = Field(default=2026, ge=2000, le=2100, description="报考年份")
    subject_type: str = Field(..., max_length=8, description="科类：物理 / 历史（传统文理由前端映射）")
    elective_subjects: list[str] = Field(default_factory=list, description="再选科目列表")
    score: int | None = Field(default=None, ge=0, le=750, description="高考分数（明文仅内存态，落库脱敏）")
    rank: int | None = Field(default=None, ge=1, description="考生位次（可直填，优先于换算）")
    preferred_cities: list[str] = Field(default_factory=list, description="偏好城市")
    preferred_majors: list[str] = Field(default_factory=list, description="偏好专业方向")
    preferred_types: list[str] = Field(default_factory=list, description="偏好院校层次标签")
    anon_id: str | None = Field(default=None, max_length=64, description="匿名标识（不含身份信息）")

    @field_validator("elective_subjects", "preferred_cities", "preferred_majors", "preferred_types")
    @classmethod
    def _strip_list(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]


class ProfileOut(BaseModel):
    """建档回执（ProfileView 投影；**不含明文分数/位次**，只回区间）。"""

    id: int | None
    province: str
    current_year: int
    subject_type: str
    elective_subjects: list[str]
    score_bucket: str | None = None
    rank_bucket: str | None = None
    preferred_cities: list[str] = []
    preferred_majors: list[str] = []
    preferred_types: list[str] = []
    anon_id: str | None = None


class EquivScoreOut(BaseModel):
    """单年等位分（位次 → 某年分数）。"""

    year: int
    score: float | None = Field(default=None, description="等位分；None = 该年数据待补，不伪造")
    capped: bool = False
    note: str = ""
    low_score: int = 0
    high_score: int = 0


class RankResponse(BaseModel):
    """GET /api/rank 响应：位次 + 近三年等位分。"""

    year: int
    province: str
    subject_type: str
    score: int | None
    rank: int
    rank_source: str = Field(default="computed", description="computed（按分数换算）/ manual（用户直填）")
    capped: bool = False
    note: str = ""
    equiv_scores: list[EquivScoreOut] = []
    caliber_text: str = ""
