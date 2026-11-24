"""专家预约接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import expert as expert_api
from app.db.models import Base
from app.db.session import get_db
from app.main import app


def test_expert_appointment_create_and_admin_flow(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    class User:
        username = "student"

    monkeypatch.setattr(expert_api, "get_current_account", lambda request, db, required=True: User())
    app.dependency_overrides[get_db] = _get_db
    try:
        client = TestClient(app)
        created = client.post(
            "/api/expert-appointments",
            json={
                "plan_tier": "单次深度咨询",
                "price": "699",
                "contact_name": "李家长",
                "contact_info": "13800000000",
                "note": "广东物理类 590 分",
            },
        )
        assert created.status_code == 200, created.text
        appointment_id = created.json()["id"]

        listed = client.get(
            "/api/admin/expert-appointments",
            headers={"X-Admin-Demo-Role": "admin"},
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        updated = client.patch(
            f"/api/admin/expert-appointments/{appointment_id}",
            json={"status": "contacted"},
            headers={"X-Admin-Demo-Role": "admin"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "contacted"
    finally:
        app.dependency_overrides.pop(get_db, None)
