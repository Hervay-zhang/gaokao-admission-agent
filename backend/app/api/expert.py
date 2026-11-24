"""专家服务预约接口。"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_account, require_admin
from app.core.exceptions import NotFoundError
from app.db.models import AuditLog, ExpertAppointment
from app.db.session import get_db
from app.schemas.expert import (
    AppointmentCreateRequest,
    AppointmentItem,
    AppointmentListResponse,
    AppointmentStatusRequest,
)

router = APIRouter(prefix="/api", tags=["expert"])


def _item(row: ExpertAppointment) -> AppointmentItem:
    return AppointmentItem(
        id=row.id,
        username=row.username,
        plan_tier=row.plan_tier,
        price=row.price,
        contact_name=row.contact_name,
        contact_info=row.contact_info,
        note=row.note or "",
        status=row.status,
        created_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    )


@router.post("/expert-appointments", response_model=AppointmentItem, summary="提交专家预约")
def create_appointment(
    payload: AppointmentCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> AppointmentItem:
    user = get_current_account(request, db, required=True)
    assert user is not None
    row = ExpertAppointment(
        username=user.username,
        plan_tier=payload.plan_tier.strip(),
        price=payload.price,
        contact_name=payload.contact_name.strip(),
        contact_info=payload.contact_info.strip(),
        note=payload.note.strip(),
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _item(row)


@router.get("/expert-appointments/mine", response_model=AppointmentListResponse, summary="我的专家预约")
def list_my_appointments(
    request: Request,
    db: Session = Depends(get_db),
) -> AppointmentListResponse:
    user = get_current_account(request, db, required=True)
    assert user is not None
    rows = db.execute(
        select(ExpertAppointment)
        .where(ExpertAppointment.username == user.username)
        .order_by(ExpertAppointment.created_at.desc())
    ).scalars().all()
    items = [_item(row) for row in rows]
    return AppointmentListResponse(total=len(items), items=items)


@router.get("/admin/expert-appointments", response_model=AppointmentListResponse, summary="管理员查看预约")
def list_appointments(
    request: Request,
    status: str | None = Query(default=None, pattern="^(pending|contacted|completed|cancelled)$"),
    keyword: str = Query(default="", max_length=80),
    db: Session = Depends(get_db),
) -> AppointmentListResponse:
    require_admin(request)
    stmt = select(ExpertAppointment)
    if status:
        stmt = stmt.where(ExpertAppointment.status == status)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                ExpertAppointment.username.like(like),
                ExpertAppointment.contact_name.like(like),
                ExpertAppointment.contact_info.like(like),
                ExpertAppointment.plan_tier.like(like),
            )
        )
    rows = db.execute(stmt.order_by(ExpertAppointment.created_at.desc())).scalars().all()
    items = [_item(row) for row in rows]
    return AppointmentListResponse(total=len(items), items=items)


@router.get("/admin/expert-appointments/export", summary="导出专家预约 CSV")
def export_appointments(
    request: Request,
    status: str | None = Query(default=None, pattern="^(pending|contacted|completed|cancelled)$"),
    db: Session = Depends(get_db),
) -> Response:
    require_admin(request)
    stmt = select(ExpertAppointment)
    if status:
        stmt = stmt.where(ExpertAppointment.status == status)
    rows = db.execute(stmt.order_by(ExpertAppointment.created_at.desc())).scalars().all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["编号", "用户", "方案", "价格", "联系人", "联系方式", "状态", "提交时间", "备注"])
    for row in rows:
        writer.writerow([
            row.id,
            row.username,
            row.plan_tier,
            str(row.price),
            row.contact_name,
            row.contact_info,
            row.status,
            row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            row.note or "",
        ])
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=expert-appointments.csv"},
    )


@router.patch("/admin/expert-appointments/{appointment_id}", response_model=AppointmentItem, summary="更新预约状态")
def update_appointment_status(
    appointment_id: int,
    payload: AppointmentStatusRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> AppointmentItem:
    require_admin(request)
    row = db.execute(
        select(ExpertAppointment).where(ExpertAppointment.id == appointment_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("预约记录不存在", detail={"code": "APPOINTMENT_NOT_FOUND"})
    row.status = payload.status
    db.commit()
    db.add(
        AuditLog(
            action="expert_appointment_status_update",
            actor="admin",
            detail=json.dumps({"id": appointment_id, "status": payload.status}, ensure_ascii=False),
        )
    )
    db.commit()
    db.refresh(row)
    return _item(row)
