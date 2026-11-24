"""T22 · 反馈闭环 Pydantic 模型（请求 / 回执）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 允许的反馈动作
KIND_UP = "up"
KIND_DOWN = "down"
KIND_CORRECTION = "correction"
ALLOWED_KINDS = frozenset({KIND_UP, KIND_DOWN, KIND_CORRECTION})

# 反馈维度
ALLOWED_DIMENSIONS = frozenset({"answer", "recommend"})

# 点踩原因（前端下拉固定枚举，杜绝自由文本注入面）
DOWN_REASONS = ("推荐不准", "数据错误", "回答跑题", "其他")
ALLOWED_REASONS = frozenset(DOWN_REASONS)


class FeedbackRequest(BaseModel):
    """POST /api/feedback 请求体。"""

    message_id: int | None = Field(default=None, description="被反馈的消息 id（done 事件返回；无会话可空）")
    session_sid: str = Field(default="", max_length=64, description="匿名会话 ID（不含身份）")
    kind: str = Field(..., description="up / down / correction")
    dimension: str = Field(default="answer", description="answer（问答）/ recommend（推荐）")
    reason: str = Field(default="", max_length=64, description="点踩原因（down 时必填枚举）")
    correction: str = Field(default="", max_length=2000, description="纠错内容（correction 时必填）")


class FeedbackAck(BaseModel):
    """提交回执。"""

    ok: bool = True
    feedback_id: int | None = None
    deduplicated: bool = Field(default=False, description="该输出此前已反馈（本次未重复入库）")


class FeedbackView(BaseModel):
    """反馈落库后的可读视图（管理后台/测试核对用；不含 session_sid）。"""

    feedback_id: int | None
    message_id: int | None
    kind: str
    dimension: str | None = None
    rating: int | None = None
    reason: str | None = None
    correction: str | None = None
    prompt_snapshot: str | None = None
    response_snapshot: str | None = None
    model: str | None = None
