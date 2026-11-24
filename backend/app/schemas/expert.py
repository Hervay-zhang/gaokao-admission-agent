"""专家服务预约模型。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class AppointmentCreateRequest(BaseModel):
    plan_tier: str = Field(..., min_length=1, max_length=32)
    price: Decimal = Field(..., ge=0, le=100000)
    contact_name: str = Field(..., min_length=1, max_length=64)
    contact_info: str = Field(..., min_length=5, max_length=128)
    note: str = Field(default="", max_length=2000)


class AppointmentItem(BaseModel):
    id: int
    username: str
    plan_tier: str
    price: Decimal
    contact_name: str
    contact_info: str
    note: str
    status: str
    created_at: str


class AppointmentListResponse(BaseModel):
    total: int
    items: list[AppointmentItem]


class AppointmentStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(pending|contacted|completed|cancelled)$")
