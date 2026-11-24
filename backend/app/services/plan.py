"""T26 · 志愿表服务（生成 / 增删改 / 排序 / 容量 + 乐观锁）。

职责
----
1. **容量校验**：按省配置（``settings.plan_capacity_for``），超额 → 422 ``PLAN_CAPACITY_EXCEEDED``；
2. **system_tier 只读**：写入后任何更新**不得覆盖**（用户调档只改 ``tier`` 并置 ``user_adjusted=True``）；
3. **乐观锁**：志愿表带 ``version``，客户端提交旧版本 → 409 ``PLAN_VERSION_CONFLICT``；
4. **排序持久化**：``reorder`` 按给定 item id 顺序重排 ``seq``（1..N 连续，刷新后顺序不变）；
5. **重复拦截**：同一 (院校, 专业组) 不重复入表 → 422 ``PLAN_DUPLICATE_ITEM``。

可注入
------
- ``PlanRepository``：SQL 生产 / 内存测试；全部离线可测，不依赖 MySQL。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.config import settings
from app.core.exceptions import NotFoundError, PlanConflictError, ValidationError
from app.core.logging import get_logger
from app.schemas.plan import ALLOWED_TIERS, ItemView, PlanView

logger = get_logger(__name__)

MAX_ITEMS_HARD_CAP = 200  # 兜底上限（防异常省份配置无限扩张）


# ============================================================
#  仓储接口
# ============================================================


class PlanRepository(Protocol):
    """志愿表仓储接口（plan 含 items）。"""

    def create_plan(
        self, profile_id: int, name: str, year: int, owner_username: str | None = None
    ) -> dict[str, Any]: ...

    def get_plan(self, plan_id: int) -> dict[str, Any] | None: ...

    def list_plans(self, profile_id: int, owner_username: str | None = None) -> list[dict[str, Any]]: ...

    def list_owned_plans(self, owner_username: str) -> list[dict[str, Any]]: ...

    def delete_plan(self, plan_id: int) -> None: ...

    def add_item(self, plan_id: int, record: dict[str, Any]) -> dict[str, Any]: ...

    def update_item(self, plan_id: int, item_id: int, changes: dict[str, Any]) -> dict[str, Any]: ...

    def delete_item(self, plan_id: int, item_id: int) -> None: ...

    def set_seqs(self, plan_id: int, seq_by_id: dict[int, int]) -> None: ...

    def set_share(self, plan_id: int, token: str | None, expires_at: Any | None) -> None: ...

    def find_by_share_token(self, token: str) -> dict[str, Any] | None: ...

    def profile_province(self, profile_id: int) -> str | None: ...

    def profile_owner(self, profile_id: int) -> str | None: ...

    def bump_version(self, plan_id: int, expected: int) -> bool: ...


# ============================================================
#  内存实现（测试）
# ============================================================


class InMemoryPlanRepository:
    """内存志愿表仓储（测试用）。"""

    def __init__(
        self,
        provinces: dict[int, str] | None = None,
        owners: dict[int, str] | None = None,
    ) -> None:
        self._plans: dict[int, dict[str, Any]] = {}
        self._items: dict[int, dict[str, Any]] = {}
        self._next_plan = 1
        self._next_item = 1
        self._provinces = provinces or {}  # profile_id → province
        self._owners = owners or {}

    def create_plan(
        self,
        profile_id: int,
        name: str,
        year: int,
        owner_username: str | None = None,
    ) -> dict[str, Any]:
        pid = self._next_plan
        self._next_plan += 1
        self._plans[pid] = {
            "id": pid, "profile_id": profile_id, "name": name, "year": year, "version": 0,
            "owner_username": owner_username, "share_token": None, "share_expires_at": None,
        }
        return {**self._plans[pid], "items": []}

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        p = self._plans.get(plan_id)
        if p is None:
            return None
        items = sorted((i for i in self._items.values() if i["plan_id"] == plan_id), key=lambda x: x["seq"])
        return {**p, "items": [dict(i) for i in items]}

    def list_plans(self, profile_id: int, owner_username: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in self._plans.values():
            if p["profile_id"] != profile_id:
                continue
            if owner_username is not None and p.get("owner_username") != owner_username:
                continue
            items = [i for i in self._items.values() if i["plan_id"] == p["id"]]
            out.append({**p, "items": [dict(i) for i in sorted(items, key=lambda x: x["seq"])]})
        return sorted(out, key=lambda x: x["id"])

    def list_owned_plans(self, owner_username: str) -> list[dict[str, Any]]:
        out = []
        for p in self._plans.values():
            if p.get("owner_username") != owner_username:
                continue
            items = [i for i in self._items.values() if i["plan_id"] == p["id"]]
            out.append({**p, "items": [dict(i) for i in sorted(items, key=lambda x: x["seq"])]})
        return sorted(out, key=lambda x: x["id"])

    def delete_plan(self, plan_id: int) -> None:
        self._plans.pop(plan_id, None)
        for iid in [i["id"] for i in self._items.values() if i["plan_id"] == plan_id]:
            self._items.pop(iid, None)

    def add_item(self, plan_id: int, record: dict[str, Any]) -> dict[str, Any]:
        iid = self._next_item
        self._next_item += 1
        row = {"id": iid, "plan_id": plan_id, **record}
        self._items[iid] = row
        return dict(row)

    def update_item(self, plan_id: int, item_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        row = self._items[item_id]
        row.update(changes)
        return dict(row)

    def delete_item(self, plan_id: int, item_id: int) -> None:
        self._items.pop(item_id, None)

    def set_seqs(self, plan_id: int, seq_by_id: dict[int, int]) -> None:
        for iid, seq in seq_by_id.items():
            if iid in self._items:
                self._items[iid]["seq"] = seq

    def set_share(self, plan_id: int, token: str | None, expires_at: Any | None) -> None:
        p = self._plans.get(plan_id)
        if p is not None:
            p["share_token"] = token
            p["share_expires_at"] = expires_at

    def find_by_share_token(self, token: str) -> dict[str, Any] | None:
        for p in self._plans.values():
            if p.get("share_token") == token:
                return self.get_plan(p["id"])
        return None

    def profile_province(self, profile_id: int) -> str | None:
        return self._provinces.get(profile_id)

    def profile_owner(self, profile_id: int) -> str | None:
        return self._owners.get(profile_id)

    def bump_version(self, plan_id: int, expected: int) -> bool:
        p = self._plans.get(plan_id)
        if p is None or p["version"] != expected:
            return False
        p["version"] = expected + 1
        return True


# ============================================================
#  SQL 实现（生产）
# ============================================================


class SqlPlanRepository:
    """MySQL 志愿表仓储。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _item_dict(self, it: Any) -> dict[str, Any]:
        return {
            "id": int(it.id),
            "plan_id": int(it.plan_id),
            "seq": int(it.seq),
            "university_code": it.university_code,
            "university_name": it.university_name,
            "group_code": it.group_code,
            "tier": it.tier or "",
            "system_tier": it.system_tier or "",
            "user_adjusted": bool(it.user_adjusted),
            "notes": it.notes or "",
        }

    def create_plan(
        self,
        profile_id: int,
        name: str,
        year: int,
        owner_username: str | None = None,
    ) -> dict[str, Any]:
        from app.db.models import VolunteerPlan

        p = VolunteerPlan(
            profile_id=profile_id,
            owner_username=owner_username,
            name=name,
            year=year,
            version=0,
        )
        self._db.add(p)
        self._db.commit()
        return {"id": int(p.id), "profile_id": profile_id, "owner_username": owner_username,
                "name": name, "year": year,
                "version": 0, "share_token": None, "share_expires_at": None, "items": []}

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        from app.db.models import VolunteerPlan

        p = self._db.get(VolunteerPlan, plan_id)
        if p is None:
            return None
        items = sorted(p.items, key=lambda x: x.seq)
        return {
            "id": int(p.id), "profile_id": int(p.profile_id), "name": p.name, "year": int(p.year),
            "owner_username": p.owner_username,
            "version": int(p.version), "share_token": p.share_token,
            "share_expires_at": p.share_expires_at, "items": [self._item_dict(i) for i in items],
        }

    def list_plans(self, profile_id: int, owner_username: str | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.db.models import VolunteerPlan

        stmt = select(VolunteerPlan).where(VolunteerPlan.profile_id == profile_id)
        if owner_username is not None:
            stmt = stmt.where(VolunteerPlan.owner_username == owner_username)
        plans = self._db.execute(stmt.order_by(VolunteerPlan.id)).scalars().all()
        out = []
        for p in plans:
            items = sorted(p.items, key=lambda x: x.seq)
            out.append({
                "id": int(p.id), "profile_id": int(p.profile_id), "name": p.name, "year": int(p.year),
                "owner_username": p.owner_username,
                "version": int(p.version), "share_token": p.share_token,
            "share_expires_at": p.share_expires_at, "items": [self._item_dict(i) for i in items],
            })
        return out

    def list_owned_plans(self, owner_username: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.db.models import VolunteerPlan

        plans = (
            self._db.execute(
                select(VolunteerPlan)
                .where(VolunteerPlan.owner_username == owner_username)
                .order_by(VolunteerPlan.id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": int(p.id),
                "profile_id": int(p.profile_id),
                "owner_username": p.owner_username,
                "name": p.name,
                "year": int(p.year),
                "version": int(p.version),
                "share_token": p.share_token,
                "share_expires_at": p.share_expires_at,
                "items": [self._item_dict(i) for i in sorted(p.items, key=lambda x: x.seq)],
            }
            for p in plans
        ]

    def delete_plan(self, plan_id: int) -> None:
        from app.db.models import VolunteerPlan

        p = self._db.get(VolunteerPlan, plan_id)
        if p is not None:
            self._db.delete(p)
            self._db.commit()

    def add_item(self, plan_id: int, record: dict[str, Any]) -> dict[str, Any]:
        from app.db.models import VolunteerItem

        it = VolunteerItem(plan_id=plan_id, **record)
        self._db.add(it)
        self._db.commit()
        return self._item_dict(it)

    def update_item(self, plan_id: int, item_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        from app.db.models import VolunteerItem

        it = self._db.get(VolunteerItem, item_id)
        if it is None:
            raise NotFoundError("志愿条目不存在", detail={"code": "ITEM_NOT_FOUND"})
        for k, v in changes.items():
            setattr(it, k, v)
        self._db.commit()
        return self._item_dict(it)

    def delete_item(self, plan_id: int, item_id: int) -> None:
        from app.db.models import VolunteerItem

        it = self._db.get(VolunteerItem, item_id)
        if it is not None:
            self._db.delete(it)
            self._db.commit()

    def set_seqs(self, plan_id: int, seq_by_id: dict[int, int]) -> None:
        from app.db.models import VolunteerItem

        # 两阶段：先整体偏移避让唯一约束 (plan_id, seq)，再落最终值
        for iid in seq_by_id:
            it = self._db.get(VolunteerItem, iid)
            if it is not None:
                it.seq = 100000 + iid
        self._db.flush()
        for iid, seq in seq_by_id.items():
            it = self._db.get(VolunteerItem, iid)
            if it is not None:
                it.seq = seq
        self._db.commit()

    def profile_province(self, profile_id: int) -> str | None:
        from app.db.models import UserProfile

        p = self._db.get(UserProfile, profile_id)
        return p.province if p is not None else None

    def profile_owner(self, profile_id: int) -> str | None:
        from app.db.models import UserProfile

        p = self._db.get(UserProfile, profile_id)
        return p.owner_username if p is not None else None

    def set_share(self, plan_id: int, token: str | None, expires_at: Any | None) -> None:
        from app.db.models import VolunteerPlan

        p = self._db.get(VolunteerPlan, plan_id)
        if p is not None:
            p.share_token = token
            p.share_expires_at = expires_at
            self._db.commit()

    def find_by_share_token(self, token: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from app.db.models import VolunteerPlan

        p = self._db.execute(
            select(VolunteerPlan).where(VolunteerPlan.share_token == token)
        ).scalars().first()
        if p is None:
            return None
        return self.get_plan(int(p.id))

    def bump_version(self, plan_id: int, expected: int) -> bool:
        from sqlalchemy import update

        from app.db.models import VolunteerPlan

        res = self._db.execute(
            update(VolunteerPlan)
            .where(VolunteerPlan.id == plan_id, VolunteerPlan.version == expected)
            .values(version=expected + 1)
        )
        self._db.commit()
        return res.rowcount == 1


# ============================================================
#  服务
# ============================================================


class PlanService:
    """志愿表服务。"""

    def __init__(self, repo: PlanRepository) -> None:
        self.repo = repo

    # ---- 查询 ----

    def list_plans(self, profile_id: int, owner_username: str | None = None) -> list[PlanView]:
        self._assert_profile_owner(profile_id, owner_username)
        plans = self.repo.list_plans(profile_id, owner_username=owner_username)
        return [self._to_view(p) for p in plans]

    def list_owned_plans(self, owner_username: str) -> list[PlanView]:
        if not owner_username:
            return []
        return [self._to_view(p) for p in self.repo.list_owned_plans(owner_username)]

    def get_plan(self, plan_id: int, owner_username: str | None = None) -> PlanView:
        p = self._require_plan(plan_id, owner_username)
        return self._to_view(p)

    # ---- 创建 / 删除 ----

    def create_plan(
        self,
        *,
        profile_id: int,
        name: str,
        year: int,
        owner_username: str | None = None,
    ) -> PlanView:
        self._assert_profile_owner(profile_id, owner_username)
        p = self.repo.create_plan(
            profile_id,
            name or "我的志愿表",
            year,
            owner_username=owner_username,
        )
        return self._to_view(p)

    def delete_plan(self, plan_id: int, owner_username: str | None = None) -> None:
        self._require_plan(plan_id, owner_username)
        self.repo.delete_plan(plan_id)

    # ---- 条目增删改 ----

    def add_item(
        self,
        *,
        plan_id: int,
        version: int,
        university_code: str,
        university_name: str,
        group_code: int | None = None,
        tier: str = "",
        notes: str = "",
        owner_username: str | None = None,
    ) -> PlanView:
        """加入一个院校专业组（容量 / 重复 / 档位校验 + 乐观锁）。"""
        plan = self._require_plan(plan_id, owner_username)
        self._check_version(plan, version)
        if tier and tier not in ALLOWED_TIERS:
            raise ValidationError(f"tier 需为 {ALLOWED_TIERS} 之一，收到 {tier!r}", detail={"code": "TIER_INVALID"})

        capacity = self._capacity(plan)
        if len(plan["items"]) >= capacity:
            raise ValidationError(
                f"志愿表已满（上限 {capacity} 个院校专业组），请先删除后再添加",
                detail={"code": "PLAN_CAPACITY_EXCEEDED", "capacity": capacity},
            )
        for it in plan["items"]:
            if it["university_code"] == university_code and it["group_code"] == group_code:
                raise ValidationError(
                    "该院校专业组已在志愿表中（同一院校不同专业组可分别加入）",
                    detail={"code": "PLAN_DUPLICATE_ITEM"},
                )

        seq = max((it["seq"] for it in plan["items"]), default=0) + 1
        # system_tier 只读：写入时与 tier 同值；此后任何更新不得覆盖
        self.repo.add_item(plan_id, {
            "seq": seq,
            "university_code": university_code,
            "university_name": university_name,
            "group_code": group_code,
            "tier": tier or None,
            "system_tier": tier or None,
            "user_adjusted": False,
            "notes": notes or None,
        })
        self._bump(plan_id, version)
        return self.get_plan(plan_id, owner_username)

    def update_item(
        self,
        *,
        plan_id: int,
        item_id: int,
        version: int,
        tier: str | None = None,
        notes: str | None = None,
        owner_username: str | None = None,
    ) -> PlanView:
        """更新条目（**system_tier 不可覆盖**；用户调档只改 tier 并标记 user_adjusted）。"""
        plan = self._require_plan(plan_id, owner_username)
        self._check_version(plan, version)
        item = self._require_item(plan, item_id)

        changes: dict[str, Any] = {}
        if tier is not None:
            if tier not in ALLOWED_TIERS:
                raise ValidationError(f"tier 需为 {ALLOWED_TIERS} 之一，收到 {tier!r}", detail={"code": "TIER_INVALID"})
            changes["tier"] = tier
            # 与系统档位不同 → 标记用户已调整（system_tier 原值保留，绝不覆盖）
            changes["user_adjusted"] = tier != item["system_tier"]
        if notes is not None:
            changes["notes"] = notes or None

        if changes:
            self.repo.update_item(plan_id, item_id, changes)
            self._bump(plan_id, version)
        return self.get_plan(plan_id, owner_username)

    def delete_item(
        self,
        *,
        plan_id: int,
        item_id: int,
        version: int,
        owner_username: str | None = None,
    ) -> PlanView:
        plan = self._require_plan(plan_id, owner_username)
        self._check_version(plan, version)
        self._require_item(plan, item_id)
        self.repo.delete_item(plan_id, item_id)
        self._resequence(plan_id)
        self._bump(plan_id, version)
        return self.get_plan(plan_id, owner_username)

    def reorder(
        self,
        *,
        plan_id: int,
        item_ids: list[int],
        version: int,
        owner_username: str | None = None,
    ) -> PlanView:
        """按给定顺序持久化志愿顺序（1..N 连续）。"""
        plan = self._require_plan(plan_id, owner_username)
        self._check_version(plan, version)
        current = {it["id"] for it in plan["items"]}
        if set(item_ids) != current or len(item_ids) != len(current):
            raise ValidationError(
                "item_ids 必须且只能包含该志愿表的全部条目（顺序可不同）",
                detail={"code": "PLAN_REORDER_MISMATCH"},
            )
        seq_by_id = {iid: idx + 1 for idx, iid in enumerate(item_ids)}
        self.repo.set_seqs(plan_id, seq_by_id)
        self._bump(plan_id, version)
        return self.get_plan(plan_id, owner_username)

    # ---- 分享（供 T28 ShareService 调用）----

    def set_share(
        self,
        plan_id: int,
        token: str | None,
        expires_at: Any | None = None,
        owner_username: str | None = None,
    ) -> None:
        """写入/撤销志愿表的只读分享（token + 过期时间）。"""
        self._require_plan(plan_id, owner_username)
        self.repo.set_share(plan_id, token, expires_at)

    def find_plan_by_share_token(self, token: str) -> tuple[PlanView, Any] | None:
        """按分享 token 查方案（含过期时间）；不存在返回 None。"""
        p = self.repo.find_by_share_token(token)
        if p is None:
            return None
        return self._to_view(p), p.get("share_expires_at")

    # ---- 内部 ----

    def _assert_profile_owner(self, profile_id: int, owner_username: str | None) -> None:
        if owner_username is None:
            return
        if self.repo.profile_owner(profile_id) != owner_username:
            raise NotFoundError("档案不存在", detail={"code": "PROFILE_NOT_FOUND"})

    def _require_plan(
        self,
        plan_id: int,
        owner_username: str | None = None,
    ) -> dict[str, Any]:
        p = self.repo.get_plan(plan_id)
        if p is None:
            raise NotFoundError("志愿表不存在", detail={"code": "PLAN_NOT_FOUND"})
        if owner_username is not None and p.get("owner_username") != owner_username:
            raise NotFoundError("志愿表不存在", detail={"code": "PLAN_NOT_FOUND"})
        return p

    def _require_item(self, plan: dict[str, Any], item_id: int) -> dict[str, Any]:
        for it in plan["items"]:
            if it["id"] == item_id:
                return it
        raise NotFoundError("志愿条目不存在", detail={"code": "ITEM_NOT_FOUND"})

    def _check_version(self, plan: dict[str, Any], version: int) -> None:
        if plan["version"] != version:
            raise PlanConflictError(
                f"志愿表已被其他操作修改（当前版本 {plan['version']}，你的版本 {version}），请刷新后重试",
                detail={"code": "PLAN_VERSION_CONFLICT", "current_version": plan["version"]},
            )

    def _bump(self, plan_id: int, expected: int) -> None:
        if not self.repo.bump_version(plan_id, expected):  # pragma: no cover - 并发兜底
            raise PlanConflictError("志愿表并发更新冲突，请刷新后重试", detail={"code": "PLAN_VERSION_CONFLICT"})

    def _resequence(self, plan_id: int) -> None:
        p = self.repo.get_plan(plan_id)
        if p is None:
            return
        ordered = sorted(p["items"], key=lambda x: x["seq"])
        self.repo.set_seqs(plan_id, {it["id"]: idx + 1 for idx, it in enumerate(ordered)})

    def _capacity(self, plan: dict[str, Any]) -> int:
        province = self.repo.profile_province(plan["profile_id"]) or ""
        cap = settings.plan_capacity_for(province) if province else settings.plan_capacity_default
        return min(cap, MAX_ITEMS_HARD_CAP)

    def _to_view(self, plan: dict[str, Any]) -> PlanView:
        province = self.repo.profile_province(plan["profile_id"]) or ""
        capacity = self._capacity(plan)
        verified = bool(province) and province in settings.plan_capacity_dict
        items = [
            ItemView(
                id=it["id"], seq=it["seq"], university_code=it["university_code"],
                university_name=it["university_name"], group_code=it["group_code"],
                tier=it.get("tier") or "", system_tier=it.get("system_tier") or "",
                user_adjusted=bool(it.get("user_adjusted")), notes=it.get("notes") or "",
            )
            for it in sorted(plan["items"], key=lambda x: x["seq"])
        ]
        return PlanView(
            id=plan["id"], profile_id=plan["profile_id"], name=plan["name"], year=plan["year"],
            version=plan["version"], province=province, capacity=capacity,
            capacity_verified=verified, count=len(items),
            share_token=plan.get("share_token") or "",
            share_expires_at=(
                plan["share_expires_at"].strftime("%Y-%m-%d %H:%M")
                if plan.get("share_expires_at") and hasattr(plan["share_expires_at"], "strftime")
                else ""
            ),
            items=items,
        )


# ============================================================
#  单例
# ============================================================

_service: PlanService | None = None


def get_plan_service(db=None) -> PlanService:  # noqa: ANN001
    """请求级构造（**不再缓存持会话的单例**）。

    旧实现里 ``if _service is not None: return _service`` 排在传参分支之前，一旦单例被
    创建，调用方传进来的请求 Session 就被静默忽略，全部走那条永不关闭的连接；
    该连接被 MySQL ``wait_timeout`` 断开后，单例上的事务永久作废（建档/方案全线 500）。
    """
    if _service is not None:  # 仅测试注入时会命中
        return _service
    if db is None:
        raise ValueError("get_plan_service 需要请求级 Session（禁止自建进程级单例会话）")
    return PlanService(SqlPlanRepository(db))


def set_plan_service(service: PlanService | None) -> None:
    global _service
    _service = service
