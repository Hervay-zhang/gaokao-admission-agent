"""API 共享依赖：登录态解析与管理员鉴权。"""

from __future__ import annotations

import hmac

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.db.models import UserAccount


def get_current_account(
    request: Request,
    db: Session,
    *,
    required: bool = True,
) -> UserAccount | None:
    """解析 Bearer 登录态并返回当前账号。

    ``required=False`` 用于匿名可访问、登录后增强的数据接口。

    认证失败统一抛 **401**：``AUTH_REQUIRED``（没带令牌）与 ``AUTH_EXPIRED``
    （令牌无效/账号已停用）。前端遇到 401 会清本地态并跳登录页，比 422 更符合
    用户预期，也让匿名/登录两种路径的接口行为一致。
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        if required:
            raise UnauthorizedError("请先登录后再操作", detail={"code": "AUTH_REQUIRED"})
        return None

    from app.services.auth import decode_token

    payload = decode_token(auth[7:].strip())
    if not payload:
        if required:
            raise UnauthorizedError("登录已失效，请重新登录", detail={"code": "AUTH_EXPIRED"})
        return None

    user = db.execute(
        select(UserAccount).where(
            UserAccount.username == payload.get("sub"),
            UserAccount.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if user is None:
        if required:
            raise UnauthorizedError("登录已失效，请重新登录", detail={"code": "AUTH_EXPIRED"})
        return None
    return user


def require_account(request: Request, db: Session) -> UserAccount:
    """必须登录的接口用这个：直接返回账号，调用方无需再写非空断言。"""
    user = get_current_account(request, db, required=True)
    if user is None:  # pragma: no cover —— required=True 时上面已抛 401
        raise UnauthorizedError("请先登录后再操作", detail={"code": "AUTH_REQUIRED"})
    return user


def require_admin(request: Request) -> None:
    """校验管理员 Bearer Token、X-Admin-Token 或本地演示身份。"""
    if settings.mock_mode and request.headers.get("x-admin-demo-role", "").lower() == "admin":
        return
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        from app.services.auth import decode_token

        payload = decode_token(auth[7:].strip())
        if payload and payload.get("role") == "admin":
            return
    token = request.headers.get("x-admin-token", "")
    if not token or not hmac.compare_digest(token, settings.admin_token):
        raise NotFoundError("资源不存在", detail={"code": "ADMIN_NOT_FOUND"})
