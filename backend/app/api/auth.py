"""登录、当前用户与退出接口。"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_account
from app.core.exceptions import BadRequestError, RateLimitError, ValidationError
from app.core.ratelimit import client_ip, get_rate_limiter
from app.db.models import UserAccount
from app.db.session import get_db
from app.services.auth import (
    authenticate,
    create_token,
    hash_password,
    verify_password,
)
from app.services.captcha import get_captcha_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    remember: bool = True


class AvatarUploadRequest(BaseModel):
    data_url: str = Field(..., min_length=32, max_length=800_000)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class ProfileUpdateRequest(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=64)
    grade: str = Field(default="", max_length=16)


MAX_AVATAR_BYTES = 350_000


def _current_user(request: Request, db: Session) -> UserAccount:
    user = get_current_account(request, db, required=True)
    assert user is not None
    return user


def _user_payload(user: UserAccount) -> dict:
    return {
        "username": user.username,
        "role": user.role,
        "roleLabel": {"student": "考生", "parent": "家长", "admin": "管理员"}.get(user.role, "用户"),
        "name": user.nickname or user.display_name,
        "nickname": user.nickname or user.display_name,
        "grade": user.grade or "",
        "avatar": user.avatar,
        "avatar_url": user.avatar_data or "",
    }


def _valid_image_magic(mime: str, raw: bytes) -> bool:
    if mime == "data:image/jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if mime == "data:image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "data:image/webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return False


@router.post("/login", summary="账号登录")
async def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """登录：同一 IP 连续失败过多（默认 20 次/小时）直接拒绝，防撞库爆破。"""
    ip = client_ip(request)
    limiter = get_rate_limiter()

    # 先看该 IP 是否已被失败次数封住（只读探测，不额外计数；>= 阈值即封）
    fail_limit = limiter.s.rate_limit_login_fail_per_hour
    pre = await limiter.check_scope(f"loginf:{ip}", "login_fail_hour", fail_limit, 3600, count=False)
    if not pre.allowed or pre.current >= fail_limit:
        raise RateLimitError(pre.reason, detail={"retry_after": pre.retry_after, "scope": pre.scope})

    user = authenticate(db, payload.username.strip(), payload.password)
    if user is None:
        # 失败计数（窗口内累计），达到阈值后本小时内后续登录直接被拒
        await limiter.check_login_fail(ip=ip)
        raise ValidationError("账号或密码错误", detail={"code": "AUTH_INVALID"})
    return {
        "ok": True,
        "token": create_token(user),
        "user": _user_payload(user),
    }


USERNAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{2,31}$"
_REGISTERABLE_ROLES = {"student", "parent"}


class RegisterRequest(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=32,
        pattern=USERNAME_PATTERN,
        description="3~32 位，字母开头，仅字母/数字/下划线",
    )
    password: str = Field(..., min_length=8, max_length=128)
    nickname: str = Field(default="", max_length=32)
    role: str = Field(default="student", description="student | parent（管理员不可自注册）")
    # 图形验证码（T33 防批量注册）：服务端签发 id，用户填图中字符
    captcha_id: str = Field(default="", max_length=128)
    captcha_code: str = Field(default="", max_length=16)


@router.get("/captcha", summary="获取图形验证码")
async def captcha(request: Request) -> dict:
    """签发一张图形验证码（SVG），答案仅存服务端，前端不可见。

    同 IP 每小时签发次数受 ``rate_limit_captcha_per_hour`` 约束，防无限取码后暴力枚举。
    """
    ip = client_ip(request)
    limiter = get_rate_limiter()
    decision = await limiter.check_scope(
        f"capi:{ip}",
        "captcha_hour",
        limiter.s.rate_limit_captcha_per_hour,
        3600,
    )
    if not decision.allowed:
        raise RateLimitError(decision.reason, detail={"retry_after": decision.retry_after, "scope": decision.scope})
    return await get_captcha_service().issue()


@router.post("/register", summary="自助注册（考生/家长）")
async def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """开放注册：角色只允许 student/parent；管理员账号仅能由后台/运维创建。

    防滥用两道闸（顺序即成本从低到高）：
    1. **图形验证码** —— 拦住脚本批量建号（校验通过才计注册次数，避免输错也吃配额）；
    2. **单 IP 注册限频** —— 即使验证码被绕过，一小时内也建不了几个号。
    """
    import re

    from sqlalchemy import select

    if not await get_captcha_service().verify(payload.captcha_id, payload.captcha_code):
        raise ValidationError("验证码错误或已过期，请点击图片刷新", detail={"code": "CAPTCHA_INVALID"})

    ip = client_ip(request)
    freq = await get_rate_limiter().check_register(ip=ip)
    if not freq.allowed:
        raise RateLimitError(freq.reason, detail={"retry_after": freq.retry_after, "scope": freq.scope})

    if payload.role not in _REGISTERABLE_ROLES:
        raise ValidationError("注册角色不合法", detail={"code": "AUTH_ROLE_INVALID"})
    username = payload.username.strip()
    if not re.match(USERNAME_PATTERN, username):
        raise ValidationError(
            "账号需 3~32 位、字母开头，仅含字母/数字/下划线",
            detail={"code": "AUTH_USERNAME_INVALID"},
        )
    exists = db.execute(
        select(UserAccount).where(UserAccount.username == username)
    ).scalar_one_or_none()
    if exists is not None:
        raise ValidationError("该账号已被注册", detail={"code": "AUTH_USER_EXISTS"})

    display = payload.nickname.strip() or username
    user = UserAccount(
        username=username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=display[:64],
        nickname=display[:64] if payload.nickname.strip() else None,
        avatar=display[0] if display else "",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "ok": True,
        "token": create_token(user),
        "user": _user_payload(user),
    }


@router.get("/me", summary="当前登录用户")
async def me(request: Request, db: Session = Depends(get_db)) -> dict:
    user = _current_user(request, db)
    return {"ok": True, "user": _user_payload(user)}


@router.patch("/profile", summary="更新当前账号昵称与年级")
async def update_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = _current_user(request, db)
    user.nickname = payload.nickname.strip()
    user.grade = payload.grade.strip() or None
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_payload(user)}


@router.post("/avatar", summary="上传当前用户头像")
async def upload_avatar(
    payload: AvatarUploadRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = _current_user(request, db)
    data_url = payload.data_url.strip()
    if not data_url.startswith("data:image/") or ";base64," not in data_url:
        raise BadRequestError("头像格式不支持", detail={"code": "AVATAR_INVALID"})
    mime, encoded = data_url.split(";base64,", 1)
    if mime not in {"data:image/jpeg", "data:image/png", "data:image/webp"}:
        raise BadRequestError("头像仅支持 JPG、PNG 或 WebP", detail={"code": "AVATAR_INVALID"})
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise BadRequestError("头像数据损坏", detail={"code": "AVATAR_INVALID"}) from exc
    if len(raw) > MAX_AVATAR_BYTES:
        raise BadRequestError("头像文件过大，请压缩后重试", detail={"code": "AVATAR_TOO_LARGE"})
    if not _valid_image_magic(mime, raw):
        raise BadRequestError("头像文件内容与格式不匹配", detail={"code": "AVATAR_INVALID"})
    user.avatar_data = data_url
    db.commit()
    return {"ok": True, "avatar_url": data_url}


@router.delete("/avatar", summary="删除当前用户头像")
async def delete_avatar(request: Request, db: Session = Depends(get_db)) -> dict:
    user = _current_user(request, db)
    user.avatar_data = None
    db.commit()
    return {"ok": True}


@router.post("/password", summary="修改当前用户密码")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = _current_user(request, db)
    if not verify_password(payload.current_password, user.password_hash):
        raise ValidationError("当前密码错误", detail={"code": "PASSWORD_INVALID"})
    if payload.current_password == payload.new_password:
        raise BadRequestError("新密码不能与当前密码相同", detail={"code": "PASSWORD_UNCHANGED"})
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@router.post("/logout", summary="退出登录")
async def logout() -> dict[str, bool]:
    # 无状态令牌：客户端删除即可；接口保留给未来黑名单/审计扩展。
    return {"ok": True}
