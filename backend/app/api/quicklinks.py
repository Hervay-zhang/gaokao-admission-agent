"""页面底部快捷链接公开接口。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/quick-links` | 公开读取启用的快捷链接（footer 渲染用，无需鉴权） |

管理端增删改见 `/api/admin/quick-links`（X-Admin-Token 鉴权，admin.py）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import QuickLink
from app.db.session import get_db
from app.schemas.quicklinks import QuickLinkItem, QuickLinkListResponse

logger = get_logger(__name__)
router = APIRouter(prefix="/api/quick-links", tags=["quick-links"])


@router.get("", response_model=QuickLinkListResponse, summary="快捷链接列表（公开）")
def list_quick_links(db: Session = Depends(get_db)) -> QuickLinkListResponse:
    rows = db.execute(
        select(QuickLink)
        .where(QuickLink.is_active.is_(True))
        .order_by(QuickLink.sort_order.asc(), QuickLink.id.asc())
    ).scalars().all()
    items = [
        QuickLinkItem(
            id=r.id,
            label=r.label,
            url=r.url,
            icon=r.icon or "",
            group=r.group or "gaokao",
            sort_order=r.sort_order or 0,
            is_active=r.is_active,
        )
        for r in rows
    ]
    return QuickLinkListResponse(total=len(items), items=items)
