"""服务端账号认证（PBKDF2 密码 + HMAC 无状态令牌）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import UserAccount

PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    import secrets

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(user: UserAccount) -> str:
    payload = {
        "sub": user.username,
        "role": user.role,
        "name": user.display_name,
        "avatar": user.avatar,
        "exp": int(time.time()) + settings.auth_token_ttl_hours * 3600,
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    sig = _b64(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = _b64(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def authenticate(db: Session, username: str, password: str) -> UserAccount | None:
    user = db.execute(
        select(UserAccount).where(UserAccount.username == username, UserAccount.is_active.is_(True))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return None
    from datetime import datetime

    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user
