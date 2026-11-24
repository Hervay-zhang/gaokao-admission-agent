"""T26 测试 —— 志愿表生成与管理（CRUD + 排序 + 容量 + 乐观锁）。

对应任务卡「测试场景」J1–J4：

J1 CRUD 全通过（创建 / 列表 / 详情 / 增 / 改 / 删 / 删除方案）
J2 排序持久化（刷新后顺序不变）
J3 超额拦截（容量按省配置）
J4 系统档位字段不可被覆盖（system_tier 只读 + user_adjusted 标记）

外加红线守卫：乐观锁 409、重复条目 422、条目/方案不存在 404、reorder 不匹配 422、容量省份差异。

设计：**不依赖 MySQL** —— ``InMemoryPlanRepository`` 构造确定性场景，离线可复现。
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

from app.api import plan as plan_api  # noqa: E402
from app.core.exceptions import (  # noqa: E402
    NotFoundError,
    PlanConflictError,
    ValidationError,
)
from app.main import app  # noqa: E402
from app.services.plan import InMemoryPlanRepository, PlanService  # noqa: E402

PROFILE_ID = 7
PROVINCE = "广东"  # 容量 45
OWNER = "student"


class _Account:
    username = OWNER


@pytest.fixture(autouse=True)
def _api_auth(monkeypatch) -> None:
    monkeypatch.setattr(plan_api, "get_current_account", lambda *args, **kwargs: _Account())


@pytest.fixture
def service() -> PlanService:
    return PlanService(InMemoryPlanRepository({PROFILE_ID: PROVINCE}, {PROFILE_ID: OWNER}))


@pytest.fixture
def small_service() -> PlanService:
    """小容量场景（构造省份容量=2）用于超额测试。"""
    import app.services.plan as plan_mod

    orig = plan_mod.settings.plan_capacity_map
    plan_mod.settings.plan_capacity_map = "测试省:2"
    svc = PlanService(InMemoryPlanRepository({PROFILE_ID: "测试省"}))
    yield svc
    plan_mod.settings.plan_capacity_map = orig


def _add(svc: PlanService, plan_id: int, code: str, name: str, group: int, tier: str, version: int):
    return svc.add_item(
        plan_id=plan_id, version=version, university_code=code,
        university_name=name, group_code=group, tier=tier,
    )


# ============================================================
# J1 CRUD 全通过
# ============================================================


def test_j1_create_and_list(service: PlanService) -> None:
    p1 = service.create_plan(profile_id=PROFILE_ID, name="方案A", year=2026)
    p2 = service.create_plan(profile_id=PROFILE_ID, name="方案B", year=2026)
    assert p1.id != p2.id
    assert p1.version == 0 and p1.count == 0
    plans = service.list_plans(PROFILE_ID)
    assert [p.name for p in plans] == ["方案A", "方案B"]


def test_plan_owner_isolation() -> None:
    svc = PlanService(InMemoryPlanRepository({PROFILE_ID: PROVINCE}, {PROFILE_ID: OWNER}))
    plan = svc.create_plan(
        profile_id=PROFILE_ID,
        name="归属测试",
        year=2026,
        owner_username=OWNER,
    )
    assert svc.get_plan(plan.id, owner_username=OWNER).id == plan.id
    with pytest.raises(NotFoundError):
        svc.get_plan(plan.id, owner_username="other")


def test_j1_add_update_delete_flow(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="主方案", year=2026)
    # 增
    plan = _add(service, plan.id, "10001", "院校A", 201, "chong", plan.version)
    assert plan.count == 1
    assert plan.items[0].seq == 1
    assert plan.items[0].system_tier == "chong"
    # 改备注
    item_id = plan.items[0].id
    plan = service.update_item(plan_id=plan.id, item_id=item_id, version=plan.version, notes="重点关注")
    assert plan.items[0].notes == "重点关注"
    # 删
    plan = service.delete_item(plan_id=plan.id, item_id=item_id, version=plan.version)
    assert plan.count == 0
    # 删方案
    service.delete_plan(plan.id)
    with pytest.raises(NotFoundError):
        service.get_plan(plan.id)


def test_j1_version_increments_on_mutation(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="v", year=2026)
    assert plan.version == 0
    plan = _add(service, plan.id, "10001", "A", 1, "chong", plan.version)
    assert plan.version == 1
    plan = service.update_item(plan_id=plan.id, item_id=plan.items[0].id, version=plan.version, notes="x")
    assert plan.version == 2


# ============================================================
# J2 排序持久化
# ============================================================


def test_j2_reorder_persists(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="排序", year=2026)
    for i, (code, name) in enumerate([("10001", "A"), ("10002", "B"), ("10003", "C")], start=1):
        plan = _add(service, plan.id, code, name, i, "wen", plan.version)
    ids = [it.id for it in plan.items]  # A,B,C
    # 反转为 C,B,A
    plan = service.reorder(plan_id=plan.id, item_ids=list(reversed(ids)), version=plan.version)
    assert [it.university_name for it in plan.items] == ["C", "B", "A"]
    assert [it.seq for it in plan.items] == [1, 2, 3]
    # 「刷新」= 重新查询 → 顺序不变
    fresh = service.get_plan(plan.id)
    assert [it.university_name for it in fresh.items] == ["C", "B", "A"]


def test_j2_delete_resequences(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="删除重排", year=2026)
    for i, (code, name) in enumerate([("10001", "A"), ("10002", "B"), ("10003", "C")], start=1):
        plan = _add(service, plan.id, code, name, i, "wen", plan.version)
    mid = plan.items[1].id  # B
    plan = service.delete_item(plan_id=plan.id, item_id=mid, version=plan.version)
    assert [it.university_name for it in plan.items] == ["A", "C"]
    assert [it.seq for it in plan.items] == [1, 2]  # 连续无空洞


def test_j2_reorder_mismatch_rejected(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="x", year=2026)
    plan = _add(service, plan.id, "10001", "A", 1, "wen", plan.version)
    plan = _add(service, plan.id, "10002", "B", 2, "wen", plan.version)
    with pytest.raises(ValidationError) as ei:
        service.reorder(plan_id=plan.id, item_ids=[plan.items[0].id], version=plan.version)
    assert ei.value.detail["code"] == "PLAN_REORDER_MISMATCH"


# ============================================================
# J3 超额拦截
# ============================================================


def test_j3_capacity_exceeded(small_service: PlanService) -> None:
    plan = small_service.create_plan(profile_id=PROFILE_ID, name="小容量", year=2026)
    plan = _add(small_service, plan.id, "10001", "A", 1, "chong", plan.version)
    plan = _add(small_service, plan.id, "10002", "B", 2, "wen", plan.version)
    assert plan.count == 2
    assert plan.capacity == 2
    with pytest.raises(ValidationError) as ei:
        _add(small_service, plan.id, "10003", "C", 3, "bao", plan.version)
    assert ei.value.detail["code"] == "PLAN_CAPACITY_EXCEEDED"
    assert ei.value.detail["capacity"] == 2


def test_j3_capacity_from_province(service: PlanService) -> None:
    """广东容量 45，且标记为已核实。"""
    plan = service.create_plan(profile_id=PROFILE_ID, name="容量", year=2026)
    assert plan.capacity == 45
    assert plan.capacity_verified is True
    assert plan.province == PROVINCE


def test_j3_capacity_default_unverified() -> None:
    """未配置省份 → 默认容量 + capacity_verified=False（不假装已核实）。"""
    svc = PlanService(InMemoryPlanRepository({PROFILE_ID: "某未配置省"}))
    plan = svc.create_plan(profile_id=PROFILE_ID, name="x", year=2026)
    assert plan.capacity == 45
    assert plan.capacity_verified is False


def test_j3_duplicate_item_rejected(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="重复", year=2026)
    plan = _add(service, plan.id, "10001", "A", 201, "chong", plan.version)
    with pytest.raises(ValidationError) as ei:
        _add(service, plan.id, "10001", "A", 201, "chong", plan.version)
    assert ei.value.detail["code"] == "PLAN_DUPLICATE_ITEM"
    # 同院校不同专业组可加入
    plan = _add(service, plan.id, "10001", "A", 202, "wen", plan.version)
    assert plan.count == 2


# ============================================================
# J4 系统档位字段不可被覆盖
# ============================================================


def test_j4_system_tier_not_overwritten(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="只读档位", year=2026)
    plan = _add(service, plan.id, "10001", "A", 201, "chong", plan.version)
    it = plan.items[0]
    assert it.system_tier == "chong" and it.user_adjusted is False
    # 用户改档位 → tier 变、system_tier 不变、user_adjusted=True
    plan = service.update_item(plan_id=plan.id, item_id=it.id, version=plan.version, tier="bao")
    it2 = plan.items[0]
    assert it2.tier == "bao"
    assert it2.system_tier == "chong", "system_tier 只读，不得被用户调整覆盖"
    assert it2.user_adjusted is True


def test_j4_adjust_back_to_system_clears_flag(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="回退", year=2026)
    plan = _add(service, plan.id, "10001", "A", 201, "wen", plan.version)
    it_id = plan.items[0].id
    plan = service.update_item(plan_id=plan.id, item_id=it_id, version=plan.version, tier="chong")
    assert plan.items[0].user_adjusted is True
    # 改回系统档位 → 标记清除（与系统一致）
    plan = service.update_item(plan_id=plan.id, item_id=it_id, version=plan.version, tier="wen")
    assert plan.items[0].tier == "wen" and plan.items[0].system_tier == "wen"
    assert plan.items[0].user_adjusted is False


def test_j4_invalid_tier_rejected(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="非法档位", year=2026)
    with pytest.raises(ValidationError) as ei:
        _add(service, plan.id, "10001", "A", 1, "super", plan.version)
    assert ei.value.detail["code"] == "TIER_INVALID"


# ============================================================
# 红线：乐观锁 / 不存在
# ============================================================


def test_optimistic_lock_conflict_on_add(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="锁", year=2026)
    plan = _add(service, plan.id, "10001", "A", 1, "chong", plan.version)  # version 1
    with pytest.raises(PlanConflictError) as ei:
        _add(service, plan.id, "10002", "B", 2, "wen", 0)  # 旧版本
    assert ei.value.detail["code"] == "PLAN_VERSION_CONFLICT"


def test_optimistic_lock_conflict_on_reorder(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="锁2", year=2026)
    plan = _add(service, plan.id, "10001", "A", 1, "wen", plan.version)
    with pytest.raises(PlanConflictError):
        service.reorder(plan_id=plan.id, item_ids=[plan.items[0].id], version=99)


def test_item_not_found(service: PlanService) -> None:
    plan = service.create_plan(profile_id=PROFILE_ID, name="x", year=2026)
    with pytest.raises(NotFoundError):
        service.update_item(plan_id=plan.id, item_id=999, version=plan.version, notes="x")


# ============================================================
# 端点级（/api/plan 接线）
# ============================================================


def _client(service: PlanService) -> TestClient:
    """返回注入了内存志愿表服务的 TestClient（调用方需在 with patch 上下文中使用）。"""
    return TestClient(app)


def test_endpoint_crud_flow(service: PlanService) -> None:
    with patch.object(plan_api, "get_plan_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        r = c.post("/api/plan", json={"profile_id": PROFILE_ID, "name": "端点方案", "year": 2026})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert r.json()["capacity"] == 45

        # 列表
        r = c.get("/api/plan", params={"profile_id": PROFILE_ID})
        assert r.json()["total"] == 1

        # 加入条目
        r = c.post(f"/api/plan/{pid}/items", params={"version": 0},
                   json={"university_code": "10001", "university_name": "院校A", "group_code": 201, "tier": "chong"})
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1 and r.json()["version"] == 1
        item_id = r.json()["items"][0]["id"]

        # 改档位（system_tier 保留）
        r = c.patch(f"/api/plan/{pid}/items/{item_id}",
                    json={"version": 1, "tier": "bao"})
        assert r.status_code == 200, r.text
        assert r.json()["items"][0]["system_tier"] == "chong"
        assert r.json()["items"][0]["user_adjusted"] is True

        # 排序
        r = c.post(f"/api/plan/{pid}/reorder", json={"version": 2, "item_ids": [item_id]})
        assert r.status_code == 200, r.text

        # 删除条目
        r = c.delete(f"/api/plan/{pid}/items/{item_id}", params={"version": 3})
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0

        # 删除方案
        r = c.delete(f"/api/plan/{pid}")
        assert r.status_code == 200


def test_endpoint_capacity_422(service: PlanService) -> None:
    with patch.object(plan_api, "get_plan_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        pid = c.post("/api/plan", json={"profile_id": PROFILE_ID, "name": "x", "year": 2026}).json()["id"]
        r = c.post(f"/api/plan/{pid}/items", params={"version": 0},
                   json={"university_code": "10001", "university_name": "A", "group_code": 1, "tier": "bad"})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "TIER_INVALID"


def test_endpoint_optimistic_lock_409(service: PlanService) -> None:
    with patch.object(plan_api, "get_plan_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        pid = c.post("/api/plan", json={"profile_id": PROFILE_ID, "name": "x", "year": 2026}).json()["id"]
        c.post(f"/api/plan/{pid}/items", params={"version": 0},
               json={"university_code": "10001", "university_name": "A", "group_code": 1, "tier": "wen"})
        # 用旧版本再提交
        r = c.post(f"/api/plan/{pid}/items", params={"version": 0},
                   json={"university_code": "10002", "university_name": "B", "group_code": 2, "tier": "wen"})
        assert r.status_code == 409
        assert r.json()["code"] == "PLAN_VERSION_CONFLICT"


def test_endpoint_plan_not_found_404(service: PlanService) -> None:
    with patch.object(plan_api, "get_plan_service", lambda *args, **kwargs: service):
        c = TestClient(app)
        r = c.get("/api/plan/999999")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "PLAN_NOT_FOUND"
