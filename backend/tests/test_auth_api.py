"""服务端登录与令牌测试。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.models import Base, UserAccount  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import create_token, decode_token, hash_password, verify_password  # noqa: E402
from app.services.captcha import CaptchaService, InMemoryCaptchaStore, set_captcha_service  # noqa: E402


def test_password_hash_and_token_roundtrip() -> None:
    encoded = hash_password("student123")
    assert verify_password("student123", encoded)
    assert not verify_password("wrong", encoded)


def test_login_and_me() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with test_session() as db:
        user = UserAccount(
            id=1,
            username="student",
            password_hash=hash_password("student123"),
            role="student",
            display_name="李同学",
            avatar="李",
        )
        db.add(user)
        db.commit()
        assert decode_token(create_token(user))["role"] == "student"

    def _get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        client = TestClient(app)
        r = client.post("/api/auth/login", json={"username": "student", "password": "student123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["role"] == "student"
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "student"

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl0kX0AAAAASUVORK5CYII="
        )
        avatar = "data:image/png;base64," + base64.b64encode(png).decode()
        uploaded = client.post(
            "/api/auth/avatar",
            json={"data_url": avatar},
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["avatar_url"] == avatar
        me_after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me_after.json()["user"]["avatar_url"] == avatar

        deleted = client.delete("/api/auth/avatar", headers={"Authorization": f"Bearer {body['token']}"})
        assert deleted.status_code == 200
        password = client.post(
            "/api/auth/password",
            json={"current_password": "student123", "new_password": "student456"},
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert password.status_code == 200, password.text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_register_flow() -> None:
    """注册成功即自动登录；重复账号 / 非法角色 / 弱密码 / 非法用户名均被拒。"""
    import asyncio

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # 注册需图形验证码（T33）：注入内存实现并种一张已知答案的码
    captcha_store = InMemoryCaptchaStore()
    set_captcha_service(CaptchaService(captcha_store))
    asyncio.run(captcha_store.save("cid-test", "AB3D", 300))
    captcha = {"captcha_id": "cid-test", "captcha_code": "AB3D"}

    def _get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        client = TestClient(app)

        # 成功注册（家长身份）→ 返回 token，可直接访问 /me
        r = client.post(
            "/api/auth/register",
            json={
                "username": "newparent",
                "password": "p@ssw0rd9",
                "nickname": "王家长",
                "role": "parent",
                **captcha,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["role"] == "parent"
        assert body["user"]["name"] == "王家长"
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "newparent"

        # 重复账号 → 422（验证码一次性，需再种一张）
        asyncio.run(captcha_store.save("cid-test2", "AB3D", 300))
        dup = client.post(
            "/api/auth/register",
            json={
                "username": "newparent",
                "password": "p@ssw0rd9",
                "captcha_id": "cid-test2",
                "captcha_code": "AB3D",
            },
        )
        assert dup.status_code == 422
        assert dup.json()["detail"]["code"] == "AUTH_USER_EXISTS"

        # 角色不合法（不允许自注册管理员）
        asyncio.run(captcha_store.save("cid-test3", "AB3D", 300))
        admin_try = client.post(
            "/api/auth/register",
            json={
                "username": "hacker",
                "password": "p@ssw0rd9",
                "role": "admin",
                "captcha_id": "cid-test3",
                "captcha_code": "AB3D",
            },
        )
        assert admin_try.status_code == 422
        assert admin_try.json()["detail"]["code"] == "AUTH_ROLE_INVALID"

        # 用户名不合法（数字开头）
        asyncio.run(captcha_store.save("cid-test4", "AB3D", 300))
        bad_name = client.post(
            "/api/auth/register",
            json={
                "username": "1abc",
                "password": "p@ssw0rd9",
                "captcha_id": "cid-test4",
                "captcha_code": "AB3D",
            },
        )
        assert bad_name.status_code == 422

        # 密码过短（Pydantic min_length=8）
        weak = client.post(
            "/api/auth/register",
            json={"username": "shortpw", "password": "123"},
        )
        assert weak.status_code == 422

        # 注册后可直接登录
        login = client.post("/api/auth/login", json={"username": "newparent", "password": "p@ssw0rd9"})
        assert login.status_code == 200, login.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        set_captcha_service(None)
