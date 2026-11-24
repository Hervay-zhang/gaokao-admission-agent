"""T26 · 志愿表数据契约（Pydantic v2）。

- ``PlanCreateRequest`` / ``PlanView`` / ``PlanListResponse``
- ``ItemAddRequest`` / ``ItemUpdateRequest`` / ``ItemView``
- ``ReorderRequest``

约定
----
- ``system_tier``：系统推荐档位，**写入后只读**；用户调整只改 ``tier`` 并置 ``user_adjusted=True``；
- ``version``：志愿表乐观锁（每次变更 +1），客户端带旧版本 → 409 ``PLAN_VERSION_CONFLICT``；
- 容量按省配置（``settings.plan_capacity_for``），超额 → 422 ``PLAN_CAPACITY_EXCEEDED``。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---- 档位枚举（与推荐引擎 tier 一致）----
ALLOWED_TIERS = ("chong", "wen", "bao")


class PlanCreateRequest(BaseModel):
    """创建志愿表。"""

    profile_id: int = Field(..., ge=1, description="所属档案 id")
    name: str = Field("我的志愿表", max_length=128)
    year: int = Field(..., ge=2000, le=2100)


class ItemAddRequest(BaseModel):
    """向志愿表加入一个院校专业组。"""

    university_code: str = Field(..., min_length=1, max_length=16)
    university_name: str = Field(..., min_length=1, max_length=128)
    group_code: int | None = Field(None, ge=0, le=9999)
    tier: str = Field("", description="系统档位（chong/wen/bao），写入 system_tier（只读）")
    notes: str = Field("", max_length=255)


class ItemUpdateRequest(BaseModel):
    """更新志愿表条目（仅可改 tier / notes；system_tier 不可覆盖）。"""

    version: int = Field(..., ge=0, description="乐观锁：客户端持有的志愿表版本")
    tier: str | None = Field(None, description="用户调整后的档位（不传表示不改）")
    notes: str | None = Field(None, max_length=255)


class ReorderRequest(BaseModel):
    """重排志愿顺序（按给定 item id 顺序落库）。"""

    version: int = Field(..., ge=0, description="乐观锁：客户端持有的志愿表版本")
    item_ids: list[int] = Field(..., min_length=1, description="按新顺序排列的条目 id")


class ItemView(BaseModel):
    """条目视图。"""

    id: int
    seq: int
    university_code: str
    university_name: str
    group_code: int | None = None
    tier: str = ""
    system_tier: str = ""
    user_adjusted: bool = False
    notes: str = ""


class PlanView(BaseModel):
    """志愿表视图（含条目与容量元信息）。"""

    id: int
    profile_id: int
    name: str
    year: int
    version: int
    province: str = ""
    capacity: int = Field(0, description="该省志愿容量上限")
    capacity_verified: bool = Field(True, description="容量是否已核实（未核实用默认值并标注）")
    count: int = 0
    share_token: str = ""
    share_expires_at: str = ""
    items: list[ItemView] = Field(default_factory=list)


class PlanListResponse(BaseModel):
    """多方案列表（不含条目明细）。"""

    total: int
    plans: list[PlanView] = Field(default_factory=list)
