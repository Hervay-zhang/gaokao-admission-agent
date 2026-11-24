"""T22 · 反馈闭环接口。

- ``POST /api/feedback`` —— 点赞/点踩/纠错落库（一条输出仅一次，409 兜底）

红线：反馈必须**流式结束后**才出现（由前端 FeedbackBar 控制，后端不校验时序但
``message_id`` 来自 ``done`` 事件，天然保证）；快照去身份（服务层脱敏）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.feedback import FeedbackAck, FeedbackRequest
from app.services.feedback import get_feedback_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackAck, summary="提交反馈（点赞/点踩/纠错 → DB 资产）")
async def submit_feedback(
    req: FeedbackRequest,
    db: Session = Depends(get_db),
) -> FeedbackAck:
    """落库为优化资产；同一输出重复反馈返回 ``409 FEEDBACK_DUPLICATE``。"""
    service = get_feedback_service(db)
    view = service.submit(
        kind=req.kind,
        dimension=req.dimension,
        message_id=req.message_id,
        session_sid=req.session_sid,
        reason=req.reason,
        correction=req.correction,
    )
    logger.info(
        "反馈落库 feedback_id=%s kind=%s dimension=%s has_snapshot=%s",
        view.feedback_id,
        view.kind,
        view.dimension,
        bool(view.prompt_snapshot or view.response_snapshot),
    )
    return FeedbackAck(ok=True, feedback_id=view.feedback_id)
