"""首页滚动咨询消息接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.db.session import get_db
from app.schemas.consultations import (
    ConsultationListResponse,
    ConsultationUpdateRequest,
)
from app.services.consultations import get_consultation_service

router = APIRouter(prefix="/api", tags=["consultations"])


@router.get("/consultations", response_model=ConsultationListResponse, summary="公开咨询消息")
def list_consultations(db: Session = Depends(get_db)) -> ConsultationListResponse:
    return ConsultationListResponse(**get_consultation_service().list_active(db))


@router.put("/admin/consultations", response_model=ConsultationListResponse, summary="更新咨询消息")
def update_consultations(
    payload: ConsultationUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ConsultationListResponse:
    require_admin(request)
    return ConsultationListResponse(**get_consultation_service().replace_all(db, payload.items))
