"""首页滚动咨询消息接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.session import get_db
from app.main import app


def test_consultations_public_read_and_admin_update() -> None:
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
    try:
        client = TestClient(app)
        assert client.get("/api/consultations").json()["items"] == []
        assert client.put("/api/admin/consultations", json={"items": ["测试咨询"]}).status_code == 404

        updated = client.put(
            "/api/admin/consultations",
            json={
                "items": [
                    "2027 志愿填报咨询已开启",
                    {"content": "查看阳光高考平台", "url": "https://gaokao.chsi.com.cn/"},
                ]
            },
            headers={"X-Admin-Demo-Role": "admin"},
        )
        assert updated.status_code == 200, updated.text
        assert [item["content"] for item in updated.json()["items"]] == [
            "2027 志愿填报咨询已开启",
            "查看阳光高考平台",
        ]
        public = client.get("/api/consultations")
        assert public.status_code == 200
        assert len(public.json()["items"]) == 2
        assert public.json()["items"][1]["url"] == "https://gaokao.chsi.com.cn/"
    finally:
        app.dependency_overrides.pop(get_db, None)
