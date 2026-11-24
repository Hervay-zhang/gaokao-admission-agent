"""页面底部快捷链接 API 测试（公开读取 + 管理端增删改）。

设计：不依赖 MySQL —— 用 SQLite 内存库建 quick_links 表，
patch 管理端 `admin_api.SessionLocal` 与公开接口的 `get_db` 依赖，
验证 API 层契约（鉴权 / URL 安全校验 / 排序 / 启停过滤）。

覆盖：
- 公开列表只返回启用项（is_active=True）
- 管理列表含禁用项
- 无 token / 错 token → 404 ADMIN_NOT_FOUND
- 新增 / 更新 / 删除（含不存在的 404）
- 非法 URL（非 http/https）→ 422
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import admin as admin_api  # noqa: E402
from app.api import quicklinks as quicklinks_api  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.models import Base, QuickLink  # noqa: E402
from app.main import app  # noqa: E402

GOOD = settings.admin_token  # 本机默认 "change-me"（测试无密钥语义）


@pytest.fixture(autouse=True)
def _sqlite_db():
    """内存 SQLite：建表 + 种子 + 注入依赖，测试后用 patch 自动还原。

    说明：ORM 的 QuickLink.id 是 BigInteger 主键，SQLite 方言下 BIGINT 不自动
    自增（仅 INTEGER PRIMARY KEY 自增），故手动建表让 id 可自增，再建其余表。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # TestClient 的 ASGI 在独立线程，须共享同一内存连接
        echo=False,
    )
    with engine.begin() as conn:
        conn.execute(sa_text(
            "CREATE TABLE quick_links ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "label VARCHAR(64) NOT NULL, "
            "url VARCHAR(512) NOT NULL, "
            "icon VARCHAR(16) NOT NULL DEFAULT '', "
            '"group" VARCHAR(32) NOT NULL DEFAULT \'gaokao\', '
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "is_active BOOLEAN NOT NULL DEFAULT 1, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        ))
    Base.metadata.create_all(engine, checkfirst=True)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def _get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[admin_api.get_db] = _get_db
    app.dependency_overrides[quicklinks_api.get_db] = _get_db
    with test_session() as db:
        db.add_all([
            QuickLink(id=1, label="阳光高考（教育部）", url="https://gaokao.chsi.com.cn/",
                      icon="官", group="gaokao", sort_order=1, is_active=True),
            QuickLink(id=2, label="广东省教育考试院", url="https://eea.gd.gov.cn/",
                      icon="粤", group="gaokao", sort_order=2, is_active=True),
            QuickLink(id=3, label="已停用链接", url="https://example.com/disabled",
                      icon="停", group="tool", sort_order=3, is_active=False),
        ])
        db.commit()
        yield
    app.dependency_overrides.pop(admin_api.get_db, None)
    app.dependency_overrides.pop(quicklinks_api.get_db, None)


client = TestClient(app)


def _admin(token: str | None) -> TestClient:
    c = TestClient(app)
    c.headers = {"X-Admin-Token": token} if token is not None else {}
    return c


class TestPublicList:
    def test_public_only_active(self) -> None:
        r = client.get("/api/quick-links")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        labels = [i["label"] for i in body["items"]]
        assert "阳光高考（教育部）" in labels
        assert "已停用链接" not in labels  # 禁用项不对公开暴露

    def test_public_sorted(self) -> None:
        r = client.get("/api/quick-links")
        items = r.json()["items"]
        orders = [i["sort_order"] for i in items]
        assert orders == sorted(orders)


class TestAdminAuth:
    @pytest.mark.parametrize("token", [None, "", "wrong-token"])
    def test_no_or_wrong_token_404(self, token: str | None) -> None:
        r = _admin(token).get("/api/admin/quick-links")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "ADMIN_NOT_FOUND"


class TestAdminCrud:
    def test_list_includes_disabled(self) -> None:
        r = _admin(GOOD).get("/api/admin/quick-links")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        assert any(i["label"] == "已停用链接" for i in body["items"])

    def test_create_success(self) -> None:
        r = _admin(GOOD).post("/api/admin/quick-links", json={
            "label": "学信网", "url": "https://www.chsi.com.cn/",
            "icon": "学", "group": "gaokao", "sort_order": 2, "is_active": True,
        })
        assert r.status_code == 200, r.text
        item = r.json()
        assert item["id"] > 0
        assert item["url"] == "https://www.chsi.com.cn/"
        # 公开接口可见新链接
        pub = client.get("/api/quick-links").json()
        assert pub["total"] == 3

    def test_create_rejects_unsafe_url(self) -> None:
        r = _admin(GOOD).post("/api/admin/quick-links", json={
            "label": "危险链接", "url": "javascript:alert(1)", "group": "tool",
        })
        assert r.status_code == 422, r.text

    def test_update_success(self) -> None:
        r = _admin(GOOD).put("/api/admin/quick-links/1", json={
            "label": "阳光高考（教育部）新版", "url": "https://gaokao.chsi.com.cn/",
            "icon": "官", "group": "gaokao", "sort_order": 1, "is_active": True,
        })
        assert r.status_code == 200, r.text
        assert r.json()["label"] == "阳光高考（教育部）新版"
        labels = [i["label"] for i in client.get("/api/quick-links").json()["items"]]
        assert "阳光高考（教育部）新版" in labels

    def test_update_not_found(self) -> None:
        r = _admin(GOOD).put("/api/admin/quick-links/9999", json={
            "label": "不存在", "url": "https://example.com/", "group": "tool",
        })
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "QUICK_LINK_NOT_FOUND"

    def test_delete_success(self) -> None:
        r = _admin(GOOD).delete("/api/admin/quick-links/2")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        labels = [i["label"] for i in client.get("/api/quick-links").json()["items"]]
        assert "广东省教育考试院" not in labels

    def test_delete_not_found(self) -> None:
        r = _admin(GOOD).delete("/api/admin/quick-links/9999")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "QUICK_LINK_NOT_FOUND"
