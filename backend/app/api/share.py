"""T28 · 只读分享 API（生成 / 读取 / 撤销）。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST   | `/api/share` | 为志愿表生成只读分享链接（默认 7 天） |
| GET    | `/api/share/{token}` | 读取**脱敏**只读快照（过期 → 410） |
| DELETE | `/api/share/{token}` | 撤销分享 |

红线：分享快照只含分数段/位次段（幂等脱敏守卫）；过期失效；**只读**（无任何写入口）。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_account
from app.core.logging import get_logger
from app.db.session import get_db
from app.services.share import get_share_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/share", tags=["share"])


class ShareCreateRequest(BaseModel):
    plan_id: int = Field(..., ge=1)
    ttl_days: int | None = Field(None, ge=1, le=30, description="有效期（天），默认取配置 7 天")


@router.post("", summary="生成只读分享链接")
async def create_share(
    request: Request,
    payload: ShareCreateRequest = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_share_service(db).create(
        plan_id=payload.plan_id,
        ttl_days=payload.ttl_days,
        owner_username=account.username,
    )


@router.get("/{token}", summary="读取分享（脱敏只读快照）")
async def read_share(token: str, db: Session = Depends(get_db)) -> dict:
    return get_share_service(db).get(token)


@router.delete("/{token}", summary="撤销分享")
async def revoke_share(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    get_share_service(db).revoke(token, owner_username=account.username)
    return {"ok": True, "revoked": True}
