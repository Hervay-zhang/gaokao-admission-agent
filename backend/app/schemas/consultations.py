"""首页滚动咨询消息模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConsultationItem(BaseModel):
    id: int
    content: str
    url: str = ""
    sort_order: int = 0


class ConsultationInput(BaseModel):
    content: str = Field(..., min_length=1, max_length=160)
    url: str = Field(default="", max_length=512)


class ConsultationListResponse(BaseModel):
    items: list[ConsultationItem]
    updated_at: str = ""


class ConsultationUpdateRequest(BaseModel):
    items: list[str | ConsultationInput] = Field(default_factory=list, max_length=10)
