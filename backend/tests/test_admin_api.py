"""T31 测试 —— 管理后台 API（Token 鉴权 + 功能委托 + 导入状态机）。

对应任务卡 J3「管理后台各功能可用」。设计：**不依赖 MySQL** ——
服务单例全部 patch 成假实现，验证 API 层契约（鉴权 / 委托参数 / 状态机 / 脱敏字段透传）。

覆盖：
- 鉴权：无 token / 错 token → 404 ADMIN_NOT_FOUND（不暴露端点存在性）
- 总览：表行数 / 当前版本 / 限流阈值结构完整
- 版本：历史列表 + 切换委托（含 dry_run 与非法入参 422）
- 导入：触发 → 状态可查；运行中重复触发 409 IMPORT_RUNNING
- 反馈：recent 透传（快照字段在列）
- 免责：只读清单（红线常量，不含可写入口）
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import admin as admin_api  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.exceptions import ValidationError  # noqa: E402
from app.main import app  # noqa: E402

GOOD = settings.admin_token  # 本机默认 "change-me"（测试无密钥语义）


def _client(token: str | None) -> TestClient:
    c = TestClient(app)
    c.headers = {"X-Admin-Token": token} if token is not None else {}
    return c


# ============================================================
# 鉴权
# ============================================================


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_no_or_wrong_token_404(token: str | None) -> None:
    for path in ("/api/admin/overview", "/api/admin/versions", "/api/admin/import",
                 "/api/admin/feedback", "/api/admin/disclaimer"):
        r = _client(token).get(path)
        assert r.status_code == 404, path
        assert r.json()["detail"]["code"] == "ADMIN_NOT_FOUND"


def test_switch_requires_token_too() -> None:
    r = TestClient(app).post("/api/admin/versions/switch", json={"note": "x"})
    assert r.status_code == 404


def test_mock_mode_admin_demo_role_allows_readonly_access(monkeypatch) -> None:
    """本地 Mock 模式允许登录页管理员角色查看后台；生产模式仍要求 Token。"""
    client = TestClient(app)
    client.headers = {"X-Admin-Demo-Role": "admin"}

    monkeypatch.setattr(settings, "mock_mode", True)
    assert client.get("/api/admin/disclaimer").status_code == 200

    monkeypatch.setattr(settings, "mock_mode", False)
    assert client.get("/api/admin/disclaimer").status_code == 404


# ============================================================
# 总览
# ============================================================


def test_overview_structure() -> None:
    counts = {"score_segments": 3503, "universities": 1095, "admission_lines": 15868, "feedback": 7}
    version = {"current_year": 2026, "ref_years": [2024, 2025, 2026],
               "year_weights": [0.2, 0.3, 0.5], "note": "T31 测试"}
    with (
        patch.object(admin_api, "_table_counts", lambda: counts),
        patch.object(admin_api, "_active_version", lambda: version),
        patch.object(admin_api, "_ratelimit_view", lambda: {"ip_per_min": 60, "sse_concurrent": 3}),
    ):
        r = _client(GOOD).get("/api/admin/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table_counts"] == counts
    assert body["data_version"] == version
    assert body["feedback_total"] == 7
    assert body["ratelimit"]["sse_concurrent"] == 3


# ============================================================
# 版本切换
# ============================================================


class _FakeVersionSvc:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_active(self):
        from app.services.data_version import DataVersionConfig

        return DataVersionConfig.from_strings(current_year=2026, ref_years=[2024, 2025, 2026],
                                              year_weights=[0.2, 0.3, 0.5])

    def list_snapshots(self) -> list:
        return []

    def switch(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("current_year") == 2030:
            raise ValidationError("时效红线校验不过（无 2030 数据）", detail={"code": "YEAR_INVALID"})
        return self.get_active()


def test_versions_list() -> None:
    with patch.object(admin_api, "get_data_version_service", _FakeVersionSvc):
        r = _client(GOOD).get("/api/admin/versions")
    assert r.status_code == 200, r.text
    assert r.json()["active"]["current_year"] == 2026
    assert r.json()["active"]["ref_years"] == [2024, 2025, 2026]


def test_versions_switch_delegates_with_dry_run() -> None:
    fake = _FakeVersionSvc()
    with patch.object(admin_api, "get_data_version_service", lambda: fake):
        r = _client(GOOD).post("/api/admin/versions/switch", json={
            "current_year": 2027, "note": "新一年入库", "dry_run": True,
        })
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True and r.json()["ok"] is True
    assert fake.calls[0]["current_year"] == 2027
    assert fake.calls[0]["actor"] == "admin-api"
    assert fake.calls[0]["dry_run"] is True


def test_versions_switch_invalid_422() -> None:
    """pydantic 粗界(2000-2100)放行的值，由服务层时效红线拦下 → 422 YEAR_INVALID。"""
    with patch.object(admin_api, "get_data_version_service", _FakeVersionSvc):
        r = _client(GOOD).post("/api/admin/versions/switch", json={"current_year": 2030})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "YEAR_INVALID"


# ============================================================
# 导入触发 / 状态机
# ============================================================


@pytest.fixture
def clean_import_state():
    with admin_api._import_lock:
        saved = dict(admin_api._import_state)
        admin_api._import_state.update(running=False, started_at=None, finished_at=None,
                                       result=None, error=None)
    yield
    with admin_api._import_lock:
        admin_api._import_state.clear()
        admin_api._import_state.update(saved)


def test_import_trigger_and_status(clean_import_state) -> None:
    with patch.object(admin_api, "_run_import", lambda: None):
        r = _client(GOOD).post("/api/admin/import")
        assert r.status_code == 200 and r.json()["running"] is True
        r = _client(GOOD).get("/api/admin/import")
        assert r.status_code == 200
        body = r.json()
        assert body["started_at"] is not None
        assert "result" in body and "error" in body


def test_import_running_conflict_409(clean_import_state) -> None:
    with admin_api._import_lock:
        admin_api._import_state["running"] = True
    r = _client(GOOD).post("/api/admin/import")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "IMPORT_RUNNING"


# ============================================================
# 反馈查看 / 免责清单
# ============================================================


def test_feedback_recent_passthrough() -> None:
    rows = [{
        "feedback_id": 3, "message_id": 42, "kind": "down", "dimension": "answer",
        "rating": -1, "reason": "推荐不准", "correction": None,
        "prompt_snapshot": "快照", "response_snapshot": "快照", "model": "mock",
        "created_at": "2026-10-15 09:00:00",
    }]

    class _FakeFb:
        def recent(self, limit: int) -> list:
            assert limit == 10
            return rows

    with patch("app.services.feedback.get_feedback_service", lambda *a, **k: _FakeFb()):
        r = _client(GOOD).get("/api/admin/feedback", params={"limit": 10})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["kind"] == "down"
    assert r.json()["items"][0]["reason"] == "推荐不准"


def test_disclaimer_readonly_listing() -> None:
    r = _client(GOOD).get("/api/admin/disclaimer")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "只读" in body["note"]
    assert set(body["entries"]) == {"recommend_report", "chat_stream", "share_snapshot"}
    assert all("仅供参考" in v for v in body["entries"].values())
