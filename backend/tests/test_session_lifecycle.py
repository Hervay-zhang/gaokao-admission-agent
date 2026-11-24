"""会话生命周期回归（2026-10-29 建档接口全线 500 事故）。

事故链
------
1. ``get_profile_service()`` 曾在无参时自建 ``SessionLocal()`` 并缓存为**进程级单例**；
   该 Session 永不关闭 → 连接不归还连接池 → 绕过 ``pool_pre_ping`` 探活；
2. 连接闲置超过 MySQL ``wait_timeout``（8h）被服务端断开 → 首次 INSERT 抛
   ``OperationalError(2013, 'Lost connection to MySQL server during query')``；
3. SQLAlchemy 事务因此作废，但代码没有 rollback → 单例上此后每次操作都抛
   ``PendingRollbackError``，所有建档请求 100% 失败。

本文件锁死三件事：服务工厂禁止自建会话、写操作失败必须 rollback、服务必须绑定传入的 Session。
"""

from __future__ import annotations

import pytest

from app.services.feedback import get_feedback_service
from app.services.plan import get_plan_service
from app.services.profile import ProfileService, SqlProfileStore, get_profile_service
from app.services.share import get_share_service


class _FakeSession:
    """最小 Session 替身：记录 commit / rollback 调用，可按需让 commit 抛错。"""

    def __init__(self, fail_on_commit: bool = False) -> None:
        self.fail_on_commit = fail_on_commit
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)
        # 模拟 flush 后填入自增主键（真实 Session 在 commit 时完成）
        try:
            obj.id = 1  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass

    def commit(self) -> None:
        self.commits += 1
        if self.fail_on_commit:
            raise RuntimeError("Lost connection to MySQL server during query")

    def rollback(self) -> None:
        self.rollbacks += 1


# ==================== 1 · 服务工厂禁止自建进程级会话 ====================


@pytest.mark.parametrize(
    ("factory", "name"),
    [
        (get_profile_service, "get_profile_service"),
        (get_plan_service, "get_plan_service"),
        (get_share_service, "get_share_service"),
        (get_feedback_service, "get_feedback_service"),
    ],
)
def test_factory_requires_request_session(factory, name: str) -> None:
    """不传 db 必须直接报错，绝不静默新建一条永不归还的连接。"""
    with pytest.raises((ValueError, RuntimeError)) as exc:
        factory()
    assert "Session" in str(exc.value), f"{name} 的报错应说明需要请求级 Session"


# ==================== 2 · 写操作失败后必须 rollback ====================


def test_store_rollbacks_after_failed_commit() -> None:
    """commit 抛错时立即 rollback，把 Session 拉回可用状态（防 PendingRollbackError 传染）。"""
    db = _FakeSession(fail_on_commit=True)
    store = SqlProfileStore(db)

    with pytest.raises(RuntimeError, match="Lost connection"):
        store.create({"anon_id": None, "owner_username": "u", "province": "广东"})

    assert db.commits == 1
    assert db.rollbacks == 1, "commit 失败后必须 rollback，否则后续操作全部报 PendingRollbackError"


def test_store_commit_ok_no_rollback() -> None:
    """正常提交路径不应产生多余 rollback。"""
    db = _FakeSession(fail_on_commit=False)
    SqlProfileStore(db).create({"anon_id": None, "owner_username": "u", "province": "广东"})
    assert db.commits == 1
    assert db.rollbacks == 0


# ==================== 3 · 服务必须绑定调用方传入的 Session ====================


def test_service_binds_given_session() -> None:
    """生产路径必须复用请求 Session（同一连接、随请求关闭），不得另起连接。"""
    db = _FakeSession()
    service = get_profile_service(db)
    assert isinstance(service, ProfileService)
    assert service.store.db is db
