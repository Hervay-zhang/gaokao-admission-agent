"""T20 · 限流防刷测试（IP + 会话 + SSE 并发连接）。

测试场景（任务卡必做）
----------------------
- J1 超过阈值返回 429 且带 ``Retry-After``；
- J2 窗口滑动后恢复；
- J3 SSE 并发超限被拒；
- J4 白名单不受限。

补充：维度独立、fail-open（存储故障放行）、连接释放与回滚、阈值不硬编码。
（pytest ``asyncio_mode = auto``，故 async test 直接写。）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.exceptions import RateLimitError
from app.core.ratelimit import (
    WINDOW_DAY,
    WINDOW_MINUTE,
    InMemoryRateLimitStore,
    RateLimiter,
    client_ip,
    get_rate_limiter,
    set_rate_limiter,
)

# ------------------------- 夹具 -------------------------


def make_settings(**overrides) -> SimpleNamespace:
    """构造测试用配置（阈值可控，证明「阈值不硬编码」）。"""
    base = {
        "rate_limit_ip_per_min": 3,
        "rate_limit_ip_per_day": 10,
        "rate_limit_session_per_min": 2,
        "rate_limit_session_per_day": 8,
        "rate_limit_sse_concurrent": 2,
        "rate_limit_sse_max_seconds": 300,
        "rate_limit_retry_after": 60,
        "ratelimit_whitelist_ip_list": ["127.0.0.1", "::1"],
        "ratelimit_whitelist_token_list": ["secret-admin"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def store() -> InMemoryRateLimitStore:
    return InMemoryRateLimitStore()


@pytest.fixture
def limiter(store: InMemoryRateLimitStore) -> RateLimiter:
    return RateLimiter(store, settings_obj=make_settings())


# ==================== J1 超限 429 ====================


async def test_j1_ip_minute_exceeded(limiter: RateLimiter) -> None:
    """J1：IP 每分钟超阈值 → 不允许，scope=ip_minute，带 retry_after。"""
    for _ in range(3):
        assert (await limiter.check_request(ip="1.2.3.4")).allowed
    d = await limiter.check_request(ip="1.2.3.4")
    assert not d.allowed
    assert d.scope == "ip_minute"
    assert d.limit == 3
    assert d.retry_after > 0


async def test_j1_enforce_raises_rate_limit_error(limiter: RateLimiter) -> None:
    """J1：enforce_request 超限抛 RateLimitError，detail 含 retry_after。"""
    for _ in range(3):
        await limiter.enforce_request(ip="1.2.3.4")
    with pytest.raises(RateLimitError) as ei:
        await limiter.enforce_request(ip="1.2.3.4")
    assert ei.value.http_status == 429
    assert ei.value.detail["retry_after"] > 0
    assert ei.value.detail["scope"] == "ip_minute"


async def test_j1_session_minute_exceeded(limiter: RateLimiter) -> None:
    """会话维度独立生效（阈值 2）。"""
    assert (await limiter.check_request(ip="9.9.9.9", session_id="s1")).allowed
    assert (await limiter.check_request(ip="9.9.9.9", session_id="s1")).allowed
    d = await limiter.check_request(ip="9.9.9.9", session_id="s1")
    assert not d.allowed
    assert d.scope == "session_minute"


def test_j1_retry_after_header_on_429() -> None:
    """J1：429 响应必须带 Retry-After 头（端到端走 TestClient）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.exceptions import register_exception_handlers

    probe = FastAPI()
    register_exception_handlers(probe)
    set_rate_limiter(RateLimiter(InMemoryRateLimitStore(), settings_obj=make_settings(rate_limit_ip_per_min=1)))

    @probe.get("/probe")
    async def _probe() -> dict[str, bool]:
        await get_rate_limiter().enforce_request(ip="5.5.5.5")
        return {"ok": True}

    client = TestClient(probe)
    try:
        assert client.get("/probe").status_code == 200
        r = client.get("/probe")
        assert r.status_code == 429
        assert "retry-after" in {k.lower() for k in r.headers}
        assert int(r.headers["retry-after"]) > 0
        assert r.json()["code"] == "RATE_LIMITED"
    finally:
        set_rate_limiter(None)


# ==================== J2 窗口滑动恢复 ====================


async def test_j2_window_recovers_after_slide(store: InMemoryRateLimitStore) -> None:
    """J2：跨两个窗口后计数归零 → 恢复放行。"""
    limiter = RateLimiter(store, settings_obj=make_settings(rate_limit_ip_per_min=3))
    for _ in range(3):
        await limiter.check_request(ip="2.2.2.2")
    assert not (await limiter.check_request(ip="2.2.2.2")).allowed

    import app.core.ratelimit as rl

    orig = rl.time.time
    try:
        base = orig()
        rl.time.time = lambda: base + 2 * WINDOW_MINUTE + 1  # type: ignore[assignment]
        d = await limiter.check_request(ip="2.2.2.2")
        assert d.allowed, "跨两个窗口后应恢复"
    finally:
        rl.time.time = orig  # type: ignore[assignment]


async def test_j2_partial_decay_next_window(store: InMemoryRateLimitStore) -> None:
    """J2：仅进入下一窗口时，上一窗口计数仍参与加权（防临界突刺）。

    用**固定时钟**从首个请求前即接管，消除真实时间跨分钟桶导致的抖动。
    """
    limiter = RateLimiter(store, settings_obj=make_settings(rate_limit_ip_per_min=4))

    import app.core.ratelimit as rl

    orig = rl.time.time
    clock = [600.0]  # 固定落在某个分钟桶内（600 // 60 = 10）
    rl.time.time = lambda: clock[0]  # type: ignore[assignment]
    try:
        for _ in range(2):
            await limiter.check_request(ip="3.3.3.3")
        clock[0] += WINDOW_MINUTE + 1  # 进入下一桶：cur(0) + prev(2) 起步
        # 新窗口：cur=1 + prev(2) = 3 <= 4，仍放行
        assert (await limiter.check_request(ip="3.3.3.3")).allowed
        # 再来一次：cur=2 + prev(2) = 4 <= 4，仍放行
        assert (await limiter.check_request(ip="3.3.3.3")).allowed
        # 第三次：cur=3 + prev(2) = 5 > 4，拒绝
        assert not (await limiter.check_request(ip="3.3.3.3")).allowed
    finally:
        rl.time.time = orig  # type: ignore[assignment]


async def test_j2_day_window_independent(limiter: RateLimiter) -> None:
    """日窗口与分钟窗口独立：日阈值高，分钟先触发。"""
    for _ in range(3):
        await limiter.check_request(ip="4.4.4.4")
    assert (await limiter.check_request(ip="4.4.4.4")).scope == "ip_minute"


# ==================== J3 SSE 并发超限 ====================


async def test_j3_sse_concurrent_exceeded(limiter: RateLimiter) -> None:
    """J3：SSE 并发连接达上限（2）后再申请被拒。"""
    assert (await limiter.acquire_connection(ip="6.6.6.6")).allowed
    assert (await limiter.acquire_connection(ip="6.6.6.6")).allowed
    d = await limiter.acquire_connection(ip="6.6.6.6")
    assert not d.allowed
    assert d.scope == "sse_concurrent"
    assert d.limit == 2


async def test_j3_release_allows_reacquire(limiter: RateLimiter) -> None:
    """J3：释放一条连接后可再次申请。"""
    await limiter.acquire_connection(ip="7.7.7.7")
    await limiter.acquire_connection(ip="7.7.7.7")
    assert not (await limiter.acquire_connection(ip="7.7.7.7")).allowed
    await limiter.release_connection(ip="7.7.7.7")
    assert (await limiter.acquire_connection(ip="7.7.7.7")).allowed


async def test_j3_over_limit_rollback_session_key(limiter: RateLimiter) -> None:
    """J3：超限时回滚已占用的会话计数（不污染会话维度）。"""
    await limiter.acquire_connection(ip="8.8.8.8", session_id="sx")
    await limiter.acquire_connection(ip="8.8.8.8", session_id="sx")
    # 第三次：ip 与 session 都到 3 > 2，拒绝并回滚
    d = await limiter.acquire_connection(ip="8.8.8.8", session_id="sx")
    assert not d.allowed
    # 会话计数应已被回滚为 2（而非 3）
    assert await limiter.current_connections(session_id="sx") == 2
    assert await limiter.current_connections(ip="8.8.8.8") == 2


async def test_j3_enforce_connection_raises(limiter: RateLimiter) -> None:
    """J3：enforce_connection 超限抛 RateLimitError。"""
    await limiter.enforce_connection(ip="10.0.0.1")
    await limiter.enforce_connection(ip="10.0.0.1")
    with pytest.raises(RateLimitError):
        await limiter.enforce_connection(ip="10.0.0.1")


async def test_connection_count_never_negative(limiter: RateLimiter) -> None:
    """多余释放不会把计数压成负数。"""
    await limiter.release_connection(ip="1.1.1.1")
    await limiter.release_connection(ip="1.1.1.1")
    assert await limiter.current_connections(ip="1.1.1.1") == 0


# ==================== J4 白名单 ====================


async def test_j4_whitelist_ip_not_limited(limiter: RateLimiter) -> None:
    """J4：白名单 IP 连打不限流（频率）。"""
    for _ in range(50):
        assert (await limiter.check_request(ip="127.0.0.1")).allowed


async def test_j4_whitelist_token_not_limited(limiter: RateLimiter) -> None:
    """J4：白名单 Token 不限流。"""
    for _ in range(50):
        assert (await limiter.check_request(ip="1.2.3.4", token="secret-admin")).allowed


async def test_j4_whitelist_sse_connection_not_counted(limiter: RateLimiter) -> None:
    """J4：白名单 IP 的 SSE 连接不计数，也不受限。"""
    for _ in range(10):
        assert (await limiter.acquire_connection(ip="127.0.0.1")).allowed
    assert await limiter.current_connections(ip="127.0.0.1") == 0


def test_whitelist_helper(limiter: RateLimiter) -> None:
    assert limiter.is_whitelisted(ip="::1")
    assert limiter.is_whitelisted(token="secret-admin")
    assert not limiter.is_whitelisted(ip="8.8.8.8", token="nope")


# ==================== 其他红线 ====================


async def test_thresholds_not_hardcoded() -> None:
    """阈值全部来自配置对象（改配置即改行为）。"""
    s = make_settings(rate_limit_ip_per_min=1)
    limiter = RateLimiter(InMemoryRateLimitStore(), settings_obj=s)
    assert (await limiter.check_request(ip="a")).allowed
    assert not (await limiter.check_request(ip="a")).allowed


async def test_fail_open_on_store_error() -> None:
    """存储故障 → fail-open 放行（限流是保护不是业务依赖）。"""

    class BoomStore:
        async def incr_window(self, key: str, window: int) -> int:  # noqa: ARG002
            raise RuntimeError("redis down")

        async def get_window(self, key: str, window: int) -> int:  # noqa: ARG002
            raise RuntimeError("redis down")

        async def add_connection(self, key: str, *, ttl: int) -> int:  # noqa: ARG002
            raise RuntimeError("redis down")

        async def remove_connection(self, key: str) -> int:  # noqa: ARG002
            raise RuntimeError("redis down")

        async def get_connections(self, key: str) -> int:  # noqa: ARG002
            raise RuntimeError("redis down")

    limiter = RateLimiter(BoomStore(), settings_obj=make_settings())
    d = await limiter.check_request(ip="1.2.3.4")
    assert d.allowed and d.reason == "store_error"
    c = await limiter.acquire_connection(ip="1.2.3.4")
    assert c.allowed and c.reason == "store_error"


async def test_scope_absent_when_dimension_missing(limiter: RateLimiter) -> None:
    """仅传 ip 时不检查会话维度；仅传 session 时不检查 IP 维度。"""
    for _ in range(3):
        assert (await limiter.check_request(ip="1.1.1.1")).allowed
    assert (await limiter.check_request(session_id="fresh")).allowed


# ==================== client_ip / 单例 ====================


def test_client_ip_from_forwarded_header() -> None:
    req = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, client=SimpleNamespace(host="127.0.0.1")
    )
    assert client_ip(req) == "203.0.113.9"


def test_client_ip_fallback_to_client() -> None:
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="198.51.100.7"))
    assert client_ip(req) == "198.51.100.7"


def test_singleton_rate_limiter() -> None:
    set_rate_limiter(None)
    a = get_rate_limiter()
    b = get_rate_limiter()
    assert a is b
    set_rate_limiter(None)


def test_window_constants() -> None:
    assert WINDOW_MINUTE == 60
    assert WINDOW_DAY == 86400


# ==================== 端点级（/api/chat/stream 接线）====================


class _StubStreamHub:
    """T21 重构后的端点测试替身：start 即回一个空流，read 只产出一条 done。"""

    async def start(self, question, *, session_id=""):
        return SimpleNamespace(stream_id="stub-stream")

    async def read(self, stream_id, *, last_event_id=-1, max_seconds=0):
        yield 'id: 0\nevent: done\ndata: {"message_id": null}\n\n'


def test_endpoint_returns_429_with_retry_after(monkeypatch) -> None:
    """J1（端点）：请求频率超限 → 429 + Retry-After + RATE_LIMITED error 事件。"""
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app

    # 注入低阈值限流器（IP 每分钟 1 次）
    probe_limiter = RateLimiter(InMemoryRateLimitStore(), settings_obj=make_settings(rate_limit_ip_per_min=1))
    monkeypatch.setattr(chat_api, "get_rate_limiter", lambda: probe_limiter)
    # 依赖注入（避免真连 DB/LLM）：stub StreamHub 直接产出一条 done 事件
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: _StubStreamHub())
    # 关闭心跳等待影响：直接不发真生成
    client = TestClient(app)

    first = client.get("/api/chat/stream", params={"q": "填报时间", "session_id": "sA"})
    assert first.status_code == 200  # deps 不可用 → error 事件，200

    second = client.get("/api/chat/stream", params={"q": "填报时间", "session_id": "sA"})
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) > 0
    assert "RATE_LIMITED" in second.text


def test_endpoint_sse_concurrent_rejected(monkeypatch) -> None:
    """J3（端点）：SSE 并发连接超限 → 429。"""
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app

    # 并发上限 1，但频率阈值放宽（避免被频率先拦）
    probe_limiter = RateLimiter(
        InMemoryRateLimitStore(),
        settings_obj=make_settings(rate_limit_ip_per_min=100, rate_limit_sse_concurrent=1),
    )
    monkeypatch.setattr(chat_api, "get_rate_limiter", lambda: probe_limiter)
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: _StubStreamHub())
    client = TestClient(app)

    # 手工占满 1 条并发连接（模拟另一条仍在进行的流）
    import asyncio

    asyncio.run(probe_limiter.acquire_connection(ip="testclient"))

    resp = client.get("/api/chat/stream", params={"q": "问题"})
    assert resp.status_code == 429
    assert "RATE_LIMITED" in resp.text


def test_health_ratelimit_endpoint(monkeypatch) -> None:
    """管理后台探针：/api/health/ratelimit 暴露阈值与占用（不含敏感信息）。"""
    from fastapi.testclient import TestClient

    from app.core import ratelimit as rl_mod
    from app.main import app

    probe_limiter = RateLimiter(InMemoryRateLimitStore(), settings_obj=make_settings(rate_limit_ip_per_min=7))
    monkeypatch.setattr(rl_mod, "get_rate_limiter", lambda: probe_limiter)
    client = TestClient(app)

    r = client.get("/api/health/ratelimit")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["thresholds"]["ip_per_min"] == 7
    assert "sse_concurrent" in body["thresholds"]
    assert "current_connections" in body
    assert body["caller"]["ip"] == "testclient"
