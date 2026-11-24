"""T33 · 注册防滥用测试：图形验证码 + IP 注册限频 + 登录失败限频。

全部用**内存实现**（captcha / 限流存储 / SQLite），离线可复现、不依赖 Redis/MySQL。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings  # noqa: E402
from app.core.ratelimit import InMemoryRateLimitStore, RateLimiter, set_rate_limiter  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.captcha import (  # noqa: E402
    CaptchaService,
    InMemoryCaptchaStore,
    set_captcha_service,
)

# TestClient 的默认 client.host 是 "testclient"，不在限流白名单（127.0.0.1/::1）里，
# 正好用于验证限频真实生效。
CLIENT_IP = "testclient"


@pytest.fixture()
def guard_env():
    """构造：内存验证码 + 内存限流 + 可调阈值的 Settings + SQLite 库。"""
    cfg = Settings(
        captcha_enabled=True,
        captcha_ttl_seconds=300,
        rate_limit_register_per_hour=3,
        rate_limit_register_per_day=10,
        rate_limit_login_fail_per_hour=3,
        rate_limit_captcha_per_hour=100,
    )
    captcha_store = InMemoryCaptchaStore()
    set_captcha_service(CaptchaService(captcha_store, settings_obj=cfg))
    set_rate_limiter(RateLimiter(InMemoryRateLimitStore(), settings_obj=cfg))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    yield {
        "captcha_store": captcha_store,
        "cfg": cfg,
        "client": TestClient(app),
    }
    app.dependency_overrides.pop(get_db, None)
    set_captcha_service(None)
    set_rate_limiter(None)


def _plant_captcha(env: dict, captcha_id: str, code: str) -> None:
    """往内存存储里直接种一张已知答案的验证码（测试专用的「发码后门」）。"""
    import asyncio

    asyncio.run(env["captcha_store"].save(captcha_id, code, 300))


def _register(client: TestClient, username: str, *, captcha_id: str = "", captcha_code: str = "", role: str = "student"):
    return client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": "p@ssw0rd9",
            "role": role,
            "captcha_id": captcha_id,
            "captcha_code": captcha_code,
        },
    )


def test_captcha_endpoint_issues_png(guard_env) -> None:
    """验证码签发接口：返回位图 data_url（非 SVG 明文），答案不下发。"""
    client = guard_env["client"]
    r = client.get("/api/auth/captcha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["captcha_id"]
    assert body["data_url"].startswith("data:image/png;base64,")
    # 答案绝不随响应下发
    assert "captcha_code" not in body
    assert "code" not in body


def test_register_requires_captcha(guard_env) -> None:
    """不带验证码 / 带错验证码 → 拒绝（CAPTCHA_INVALID）。"""
    client = guard_env["client"]
    r = _register(client, "nocaptcha")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CAPTCHA_INVALID"

    _plant_captcha(guard_env, "cid-1", "AB3D")
    r = _register(client, "wrongcaptcha", captcha_id="cid-1", captcha_code="XXXX")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CAPTCHA_INVALID"


def test_register_with_valid_captcha_and_one_time(guard_env) -> None:
    """正确验证码（大小写不敏感）→ 注册成功；同一验证码重放 → 拒绝。"""
    client = guard_env["client"]
    _plant_captcha(guard_env, "cid-ok", "AB3D")
    r = _register(client, "captchauser", captcha_id="cid-ok", captcha_code="ab3d")
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "captchauser"

    # 一次性：重放已被作废
    r2 = _register(client, "replayuser", captcha_id="cid-ok", captcha_code="AB3D")
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "CAPTCHA_INVALID"


def test_register_rate_limit_per_ip(guard_env) -> None:
    """同 IP 超过阈值（3 次/小时）→ 429，即使验证码正确。"""
    client = guard_env["client"]
    for i in range(3):
        cid = f"cid-rl-{i}"
        _plant_captcha(guard_env, cid, "AB3D")
        r = _register(client, f"rluser{i}", captcha_id=cid, captcha_code="AB3D")
        assert r.status_code == 200, r.text

    _plant_captcha(guard_env, "cid-rl-4", "AB3D")
    r = _register(client, "rluser4", captcha_id="cid-rl-4", captcha_code="AB3D")
    assert r.status_code == 429, r.text
    # 429 响应：code 在顶层，retry_after 在 detail 里
    assert r.json()["code"] == "RATE_LIMITED"
    assert r.json()["detail"]["retry_after"] > 0


def test_login_fail_rate_limit(guard_env) -> None:
    """同 IP 连续登录失败超过阈值（3 次/小时）→ 429；未超限前错误密码是 422。"""
    client = guard_env["client"]
    for _ in range(3):
        r = client.post("/api/auth/login", json={"username": "ghost", "password": "wrong-password"})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "AUTH_INVALID"

    r = client.post("/api/auth/login", json={"username": "ghost", "password": "wrong-password"})
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "RATE_LIMITED"
