"""T28 测试 —— 导出与分享（只读快照 + 脱敏 + 过期 + 撤销）。

对应任务卡「测试场景」J1–J4：

J1 生成分享链接：返回 token / share_path / created_at / expires_at / ttl_days；快照字段完整
J2 脱敏红线：快照只含分数段/位次段，结构性守卫命中敏感键即抛错（不静默）
J3 过期语义：now >= share_expires_at → 410 SHARE_EXPIRED（共享状态走志愿表，跨进程可用）
J4 只读 + 撤销：分享 API 无任何写志愿表入口（结构性断言）；token 不存在 404；撤销立即失效

**架构口径**（本卡实施时定稿）：分享**不用 Redis 存**，事实源 = `volunteer_plans.share_token
/ share_expires_at`（MySQL）。理由见 `services/share.py` 顶部说明（跨设备跨重启可用、
Redis 是本卡降级项而非硬依赖、志愿表同源无副本滞后、过期语义直接）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import share as share_api  # noqa: E402
from app.core.exceptions import NotFoundError, ShareExpiredError  # noqa: E402
from app.main import app  # noqa: E402
from app.services.plan import InMemoryPlanRepository, PlanService  # noqa: E402
from app.services.profile import (  # noqa: E402
    InMemoryProfileStore,
    ProfileInput,
    ProfileService,
)
from app.services.share import (  # noqa: E402
    ShareService,
    assert_desensitized,
    get_share_service,
    set_share_service,
)

PROFILE_ID = 1  # InMemoryProfileStore 自增起点 = 1
PROVINCE = "广东"  # 容量 45，且 capacity_verified=True
OWNER = "student"
_BASE = datetime(2026, 10, 15, 9, 0, 0)


class _Account:
    username = OWNER


@pytest.fixture(autouse=True)
def _api_auth(monkeypatch) -> None:
    monkeypatch.setattr(share_api, "get_current_account", lambda *args, **kwargs: _Account())


class Clock:
    """可控时钟：ShareService 用 `datetime.fromtimestamp(clock())`，故返回 float 时间戳。"""

    def __init__(self, start: datetime = _BASE) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current.timestamp()

    def advance(self, **kwargs: int) -> None:
        self.current = self.current + timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def profiles() -> ProfileService:
    """考生档案：分数 612 / 位次 35412（明文仅内存态，落库只 hash+bucket）。"""
    svc = ProfileService(InMemoryProfileStore())
    view = svc.create_profile(ProfileInput(province=PROVINCE, current_year=2026,
                                           subject_type="物理", score=612, rank=35412))
    assert view.id == PROFILE_ID
    return svc


@pytest.fixture
def plans() -> PlanService:
    return PlanService(InMemoryPlanRepository({PROFILE_ID: PROVINCE}, {PROFILE_ID: OWNER}))


@pytest.fixture
def service(plans: PlanService, profiles: ProfileService, clock: Clock) -> ShareService:
    return ShareService(plans=plans, profiles=profiles, clock=clock)


def _populate(plans: PlanService) -> int:
    """建一个含两所院校的志愿表，返回 plan_id。"""
    plan = plans.create_plan(
        profile_id=PROFILE_ID,
        name="我的志愿表",
        year=2026,
        owner_username=OWNER,
    )
    plan = plans.add_item(plan_id=plan.id, version=plan.version, university_code="10001",
                          university_name="中山大学", group_code=201, tier="chong", notes="重点关注")
    plan = plans.add_item(plan_id=plan.id, version=plan.version, university_code="10002",
                          university_name="华南理工大学", group_code=202, tier="wen")
    return plan.id


# ============================================================
# J1 生成分享链接 + 快照字段完整
# ============================================================


def test_j1_snapshot_fields_complete(plans: PlanService, service: ShareService) -> None:
    pid = _populate(plans)
    created = service.create(plan_id=pid)
    assert created["token"]
    assert created["plan_id"] == pid
    assert created["share_path"] == f"/#/share/{created['token']}"
    assert created["ttl_days"] == 7
    assert created["created_at"] == "2026-10-15 09:00:00"
    assert created["expires_at"] == "2026-10-22 09:00:00"

    snap = service.get(created["token"])
    for key in ("token", "plan_id", "plan_name", "year", "province", "capacity", "count",
                "created_at", "expires_at", "items", "candidate", "disclaimer"):
        assert key in snap, key
    assert snap["plan_name"] == "我的志愿表"
    assert snap["year"] == 2026
    assert snap["province"] == PROVINCE
    assert snap["capacity"] == 45
    assert snap["count"] == 2
    assert snap["disclaimer"] == "本方案仅供参考，不作为最终填报承诺。"
    assert [it["seq"] for it in snap["items"]] == [1, 2]
    assert [it["university_name"] for it in snap["items"]] == ["中山大学", "华南理工大学"]
    assert snap["items"][0]["tier"] == "chong"
    assert snap["items"][0]["system_tier"] == "chong"
    assert snap["items"][0]["user_adjusted"] is False


def test_j1_share_path_and_ttl(service: ShareService, plans: PlanService) -> None:
    pid = _populate(plans)
    created = service.create(plan_id=pid)
    assert created["share_path"].startswith("/#/share/")
    assert created["share_path"].endswith(created["token"])
    assert created["expires_at"] == "2026-10-22 09:00:00"


def test_j1_reflects_live_plan(plans: PlanService, service: ShareService) -> None:
    """快照读的是志愿表**当前**状态（同源，无副本滞后）。"""
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    plan = plans.get_plan(pid)
    plans.add_item(plan_id=pid, version=plan.version, university_code="10003",
                   university_name="暨南大学", group_code=203, tier="bao")
    snap = service.get(token)
    assert snap["count"] == 3
    assert [it["university_name"] for it in snap["items"]] == ["中山大学", "华南理工大学", "暨南大学"]


# ============================================================
# J2 脱敏红线：只含分数段/位次段，结构性守卫
# ============================================================


def test_j2_candidate_only_buckets(plans: PlanService, service: ShareService) -> None:
    pid = _populate(plans)
    snap = service.get(service.create(plan_id=pid)["token"])
    cand = snap["candidate"]
    assert cand["subject_type"] == "物理"
    assert cand["score_bucket"] == "610-619"
    assert cand["rank_bucket"] == "35000-35999"


def test_j2_no_plaintext_score_rank_identity(plans: PlanService, service: ShareService) -> None:
    """快照序列化后不得出现明文分数/位次/身份相关键名。"""
    pid = _populate(plans)
    snap = service.get(service.create(plan_id=pid)["token"])
    text = json.dumps(snap, ensure_ascii=False)
    for forbidden in ("612", "35412", "score_hash", "rank_hash", "id_card", "phone", "mobile", "email"):
        assert forbidden not in text, forbidden
    # 键名级守卫（不静默）：bucket 类键允许，敏感键禁止
    assert_desensitized(snap)


def test_j2_guard_raises_on_sensitive_key() -> None:
    """守卫命中敏感键立即抛 ValueError（不静默），bucket 类键放行。"""
    with pytest.raises(ValueError):
        assert_desensitized({"candidate": {"score": 612}})
    with pytest.raises(ValueError):
        assert_desensitized({"items": [{"rank": 1}]})
    with pytest.raises(ValueError):
        assert_desensitized({"contact": {"phone": "13800000000"}})
    # 正常快照不抛（bucket 不是敏感键）
    assert_desensitized({"candidate": {"score_bucket": "610-619", "rank_bucket": "35000-35999"},
                         "items": [{"tier": "chong", "notes": "x"}]})


def test_j2_endpoint_returns_no_forbidden_keys(plans: PlanService, service: ShareService) -> None:
    with patch.object(share_api, "get_share_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        r = c.post("/api/share", json={"plan_id": _populate(plans)})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        r = c.get(f"/api/share/{token}")
        assert r.status_code == 200, r.text
        assert_desensitized(r.json())


# ============================================================
# J3 过期语义：now >= expires_at → 410
# ============================================================


def test_j3_expired_token_410(plans: PlanService, service: ShareService, clock: Clock) -> None:
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    assert service.get(token)["plan_id"] == pid
    clock.advance(days=7)  # now == expires_at → 恰好过期
    with pytest.raises(ShareExpiredError) as ei:
        service.get(token)
    assert ei.value.detail["code"] == "SHARE_EXPIRED"
    assert ei.value.http_status == 410


def test_j3_not_yet_expired_ok(plans: PlanService, service: ShareService, clock: Clock) -> None:
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    clock.advance(days=6, hours=23, minutes=59)
    assert service.get(token)["plan_id"] == pid


def test_j3_custom_ttl(plans: PlanService, service: ShareService, clock: Clock) -> None:
    pid = _populate(plans)
    created = service.create(plan_id=pid, ttl_days=1)
    assert created["ttl_days"] == 1
    assert created["expires_at"] == "2026-10-16 09:00:00"
    clock.advance(hours=25)
    with pytest.raises(ShareExpiredError):
        service.get(created["token"])


def test_j3_endpoint_expired_410(plans: PlanService, service: ShareService, clock: Clock) -> None:
    with patch.object(share_api, "get_share_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        pid = _populate(plans)
        token = c.post("/api/share", json={"plan_id": pid}).json()["token"]
        clock.advance(days=8)
        r = c.get(f"/api/share/{token}")
        assert r.status_code == 410
        assert r.json()["code"] == "SHARE_EXPIRED"


def test_j3_expiry_from_string_form_is_parsed(plans: PlanService, service: ShareService) -> None:
    """过期时间以字符串回读（驱动差异）时仍能正确判过期。"""
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    plans.repo.set_share(pid, token, "2026-10-15 08:00:00")  # 早于当前（09:00）
    with pytest.raises(ShareExpiredError):
        service.get(token)


# ============================================================
# J4 只读 + 撤销 + 不存在
# ============================================================


def _flatten_routes(routes: Any) -> Any:
    """递归展开路由（兼容新版 FastAPI 的 `_IncludedRouter` 懒挂载包装）。"""
    for r in routes:
        sub = getattr(r, "routes", None)
        if sub:
            yield from _flatten_routes(sub)
        original = getattr(r, "original_router", None)
        if original is not None:
            yield from _flatten_routes(original.routes)
        yield r


def test_j4_share_module_has_no_write_endpoints() -> None:
    """结构性守卫：分享 API 只有 生成/读取/撤销 三个端点，无任何写志愿表入口。"""
    share_routes = sorted(
        (r.path, m)
        for r in _flatten_routes(app.routes)
        if getattr(r, "path", "").startswith("/api/share")
        for m in (getattr(r, "methods", set()) or set())
        if m in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    )
    assert share_routes == [
        ("/api/share", "POST"),
        ("/api/share/{token}", "DELETE"),
        ("/api/share/{token}", "GET"),
    ]
    # 双重保险：分享页不可编辑（不存在 PUT/PATCH）
    assert not [(p, m) for (p, m) in share_routes if m in ("PUT", "PATCH")]


def test_j4_unknown_token_404(service: ShareService) -> None:
    with pytest.raises(NotFoundError) as ei:
        service.get("nonexistent-token")
    assert ei.value.detail["code"] == "SHARE_NOT_FOUND"
    assert ei.value.http_status == 404


def test_j4_revoke_invalidates(plans: PlanService, service: ShareService) -> None:
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    assert service.get(token)["plan_id"] == pid
    service.revoke(token)
    with pytest.raises(NotFoundError):
        service.get(token)
    # 再撤销也不存在（幂等语义：不存在即报不存在）
    with pytest.raises(NotFoundError):
        service.revoke(token)


def test_j4_endpoint_revoke(plans: PlanService, service: ShareService) -> None:
    with patch.object(share_api, "get_share_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        pid = _populate(plans)
        token = c.post("/api/share", json={"plan_id": pid}).json()["token"]
        r = c.delete(f"/api/share/{token}")
        assert r.status_code == 200
        assert r.json()["revoked"] is True
        r = c.get(f"/api/share/{token}")
        assert r.status_code == 404
        # NotFoundError 基类 code=NOT_FOUND，业务码在 detail 里
        assert r.json()["detail"]["code"] == "SHARE_NOT_FOUND"


# ============================================================
# 事实源（MySQL）/ 端点接线 / 单例
# ============================================================


def test_share_persists_token_and_expiry_on_plan(
    plans: PlanService, service: ShareService,
) -> None:
    """事实源 = 志愿表：token/过期时间落在 plan 行上（跨进程可用）。"""
    pid = _populate(plans)
    token = service.create(plan_id=pid)["token"]
    row = plans.repo.get_plan(pid)
    assert row is not None
    assert row["share_token"] == token
    assert row["share_expires_at"] == datetime(2026, 10, 22, 9, 0, 0)
    # 撤销后清空
    service.revoke(token)
    row = plans.repo.get_plan(pid)
    assert row is not None
    assert row["share_token"] is None
    assert row["share_expires_at"] is None


def test_token_is_random(plans: PlanService, service: ShareService) -> None:
    pid = _populate(plans)
    t1 = service.create(plan_id=pid)["token"]
    t2 = service.create(plan_id=pid)["token"]  # 重新生成 → 新 token，旧 token 立即失效
    assert t1 != t2
    assert len(t1) >= 16
    with pytest.raises(NotFoundError):
        service.get(t1)
    assert service.get(t2)["plan_id"] == pid


def test_endpoint_create_unknown_plan_404(plans: PlanService, service: ShareService) -> None:
    with patch.object(share_api, "get_share_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        r = c.post("/api/share", json={"plan_id": 999999})
        assert r.status_code == 404
        # NotFoundError 基类 code=NOT_FOUND，业务码在 detail 里
        assert r.json()["detail"]["code"] == "PLAN_NOT_FOUND"


def test_endpoint_create_ttl_validation(plans: PlanService, service: ShareService) -> None:
    """ttl_days 越界 → 422（pydantic Field ge=1, le=30）。"""
    with patch.object(share_api, "get_share_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        pid = _populate(plans)
        assert c.post("/api/share", json={"plan_id": pid, "ttl_days": 0}).status_code == 422
        assert c.post("/api/share", json={"plan_id": pid, "ttl_days": 31}).status_code == 422


def test_singleton_injection(service: ShareService) -> None:
    set_share_service(service)
    try:
        assert get_share_service() is service
    finally:
        set_share_service(None)
