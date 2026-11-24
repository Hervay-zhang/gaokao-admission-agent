"""报告历史快照服务（账号级，仅保存区间与摘要）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.models import ReportSnapshot


def _item(row: ReportSnapshot) -> dict:
    try:
        counts = json.loads(row.tier_counts or "{}")
    except (TypeError, ValueError):
        counts = {}
    return {
        "id": int(row.id),
        "profile_id": int(row.profile_id) if row.profile_id else None,
        "title": row.title,
        "province": row.province,
        "subject_type": row.subject_type,
        "score_bucket": row.score_bucket,
        "rank_bucket": row.rank_bucket,
        "current_year": int(row.current_year),
        "tier_counts": counts,
        "summary": row.summary,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


def save_report_history(
    db: Session,
    *,
    owner_username: str,
    profile_id: int | None,
    title: str,
    province: str,
    subject_type: str,
    score_bucket: str | None,
    rank_bucket: str | None,
    current_year: int,
    tier_counts: dict[str, int],
    summary: str,
) -> dict:
    recent = (
        db.execute(
            select(ReportSnapshot)
            .where(
                ReportSnapshot.owner_username == owner_username,
                ReportSnapshot.profile_id == profile_id,
                ReportSnapshot.subject_type == subject_type,
                ReportSnapshot.current_year == current_year,
                ReportSnapshot.created_at >= datetime.now() - timedelta(minutes=10),
            )
            .order_by(ReportSnapshot.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if recent is not None:
        return _item(recent)
    row = ReportSnapshot(
        owner_username=owner_username,
        profile_id=profile_id,
        title=title[:128] or "冲稳保路径报告",
        province=province,
        subject_type=subject_type,
        score_bucket=score_bucket,
        rank_bucket=rank_bucket,
        current_year=current_year,
        tier_counts=json.dumps(tier_counts, ensure_ascii=False),
        summary=summary[:1000],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _item(row)


def list_report_history(db: Session, owner_username: str, limit: int = 30) -> list[dict]:
    rows = (
        db.execute(
            select(ReportSnapshot)
            .where(ReportSnapshot.owner_username == owner_username)
            .order_by(ReportSnapshot.created_at.desc(), ReportSnapshot.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [_item(row) for row in rows]


def get_report_history(db: Session, owner_username: str, snapshot_id: int) -> dict:
    row = db.execute(
        select(ReportSnapshot).where(
            ReportSnapshot.id == snapshot_id,
            ReportSnapshot.owner_username == owner_username,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("报告记录不存在", detail={"code": "REPORT_NOT_FOUND"})
    return _item(row)
