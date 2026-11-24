"""页面底部快捷链接 Pydantic 模型。

红线：URL 仅接受 http/https（管理端写入时校验，避免 javascript: 等注入）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

URL_MAX_LEN = 512
LABEL_MAX_LEN = 64
ALLOWED_GROUPS = ("gaokao", "official", "tool")


class QuickLinkCreateRequest(BaseModel):
    """新增快捷链接请求。"""

    label: str = Field(..., min_length=1, max_length=LABEL_MAX_LEN, description="显示名")
    url: str = Field(..., max_length=URL_MAX_LEN, description="链接（http/https）")
    icon: str = Field(default="", max_length=16, description="图标字（emoji/单字，可空）")
    group: str = Field(default="gaokao", max_length=32, description="分组")
    sort_order: int = Field(default=0, ge=0, le=9999, description="排序（小在前）")
    is_active: bool = Field(default=True, description="是否启用")

    @field_validator("url")
    @classmethod
    def _url_safe(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("链接必须以 http:// 或 https:// 开头")
        return v


class QuickLinkUpdateRequest(BaseModel):
    """更新快捷链接请求（整体替换字段）。"""

    label: str = Field(..., min_length=1, max_length=LABEL_MAX_LEN, description="显示名")
    url: str = Field(..., max_length=URL_MAX_LEN, description="链接（http/https）")
    icon: str = Field(default="", max_length=16, description="图标字（emoji/单字，可空）")
    group: str = Field(default="gaokao", max_length=32, description="分组")
    sort_order: int = Field(default=0, ge=0, le=9999, description="排序（小在前）")
    is_active: bool = Field(default=True, description="是否启用")

    @field_validator("url")
    @classmethod
    def _url_safe(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("链接必须以 http:// 或 https:// 开头")
        return v


class QuickLinkItem(BaseModel):
    """快捷链接项（公开返回）。"""

    id: int
    label: str
    url: str
    icon: str = ""
    group: str = "gaokao"
    sort_order: int = 0
    is_active: bool = True


class QuickLinkListResponse(BaseModel):
    """快捷链接列表响应。"""

    total: int
    items: list[QuickLinkItem]
