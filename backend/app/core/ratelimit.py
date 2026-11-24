"""T20 · 限流防刷（IP + 会话 双维度 + SSE 并发连接）。

目标
----
保护 SSE 长连接与 LLM Token 成本：既限**请求频率**（每 IP / 每会话，分钟 + 每日窗口），
也限 **SSE 并发连接数**（单 IP / 单会话同时最多 N 条流）。两者是**不同维度**（本卡兼做）。

设计
----
- **滑动窗口**（sliding window）：用「当前桶 + 上一桶」计数近似，避免固定窗口的临界突刺；
- **全异步**：生产用 ``redis.asyncio`` 客户端（``app.core.redis`` 返回的是异步客户端），
  故存储接口与限流器方法一律 ``async``，**必须 await**（否则 Redis 命令不会真正执行）；
- **可注入存储**：``RateLimitStore`` Protocol —— 生产用 **Redis**，测试用 **内存**，单测离线可复现；
- **Redis 不可用静默降级**：限流是保护而非业务依赖 → 存储异常时**放行**（fail-open），
  但记 warning 日志，避免「Redis 挂了全站 500」；
- **白名单**：本地开发 IP / 管理 Token 命中即不限流；
- **超限**：抛 ``RateLimitError``（HTTP 429），携带 ``Retry-After``。

红线
----
- 限流**必须在后端**实现（前端提示不算数）；
- **SSE 连接必须有上限与超时**（防长连接耗尽）；
- **阈值不得硬编码** —— 一律从 ``settings`` 读取（即 ``.env``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import settings
from app.core.exceptions import RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 窗口长度（秒）
WINDOW_MINUTE = 60
WINDOW_HOUR = 3600
WINDOW_DAY = 86400


@dataclass
class RateLimitDecision:
    """一次限流判定结果。"""

    allowed: bool
    scope: str = ""          # 触发的维度：ip_minute / ip_day / session_minute / ...
    limit: int = 0           # 该维度阈值
    current: int = 0         # 当前计数（含本次）
    retry_after: int = 0     # 建议重试等待（秒）
    reason: str = ""         # 人可读原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "scope": self.scope,
            "limit": self.limit,
            "current": self.current,
            "retry_after": self.retry_after,
            "reason": self.reason,
        }


# ============================================================
#  存储（可注入，全异步）
# ============================================================


class RateLimitStore(Protocol):
    """限流计数器存储接口（**全异步**）。"""

    async def incr_window(self, key: str, window: int) -> int:
        """在 ``key`` 对应的滑动窗口内自增并返回**当前窗口计数**（当前桶 + 上一桶近似）。"""
        ...

    async def get_window(self, key: str, window: int) -> int:
        """读取当前窗口计数（不自增）。"""
        ...

    async def add_connection(self, key: str, *, ttl: int) -> int:
        """SSE 连接数 +1，返回当前连接数。"""
        ...

    async def remove_connection(self, key: str) -> int:
        """SSE 连接数 -1，返回当前连接数（不小于 0）。"""
        ...

    async def get_connections(self, key: str) -> int:
        """读取当前 SSE 连接数。"""
        ...


def _bucket_no(window: int, now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // window)


class InMemoryRateLimitStore:
    """内存实现（测试 / 单进程）。

    用「窗口序号 + 计数」模拟滑动窗口：``bucket = now // window``，返回「当前桶 + 上一桶」做线性近似。
    """

    def __init__(self) -> None:
        self._counters: dict[str, dict[int, int]] = {}
        self._conns: dict[str, int] = {}

    async def incr_window(self, key: str, window: int) -> int:
        b = _bucket_no(window)
        buckets = self._counters.setdefault(key, {})
        # 清理陈旧窗口（保留当前与上一窗口）
        for old in [k for k in buckets if k < b - 1]:
            buckets.pop(old, None)
        buckets[b] = buckets.get(b, 0) + 1
        return buckets[b] + buckets.get(b - 1, 0)

    async def get_window(self, key: str, window: int) -> int:
        b = _bucket_no(window)
        buckets = self._counters.get(key, {})
        return buckets.get(b, 0) + buckets.get(b - 1, 0)

    async def add_connection(self, key: str, *, ttl: int) -> int:  # noqa: ARG002 - 内存无需 TTL
        self._conns[key] = self._conns.get(key, 0) + 1
        return self._conns[key]

    async def remove_connection(self, key: str) -> int:
        cur = max(0, self._conns.get(key, 0) - 1)
        self._conns[key] = cur
        return cur

    async def get_connections(self, key: str) -> int:
        return self._conns.get(key, 0)

    def reset(self) -> None:
        """清空（测试用）。"""
        self._counters.clear()
        self._conns.clear()


class RedisRateLimitStore:
    """Redis 实现（生产，``redis.asyncio`` 客户端）。

    - 频率窗口：键 ``rl:{key}:{bucket}``（INCR + EXPIRE），返回「当前桶 + 上一桶」近似滑动窗口；
    - SSE 连接：键 ``rlc:{key}``（INCR / DECR），TTL 兜底防泄漏；
    - **任何 Redis 异常向上抛出**，由 ``RateLimiter`` 捕获后 fail-open（放行）。
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client

    def _client(self) -> Any:
        if self._redis is None:
            from app.core.redis import get_redis

            self._redis = get_redis()
        return self._redis

    async def incr_window(self, key: str, window: int) -> int:
        client = self._client()
        b = _bucket_no(window)
        cur_key = f"rl:{key}:{b}"
        cur = await client.incr(cur_key)
        if cur == 1:
            await client.expire(cur_key, window * 2)
        prev = await client.get(f"rl:{key}:{b - 1}")
        return int(cur) + (int(prev) if prev else 0)

    async def get_window(self, key: str, window: int) -> int:
        client = self._client()
        b = _bucket_no(window)
        cur = await client.get(f"rl:{key}:{b}") or 0
        prev = await client.get(f"rl:{key}:{b - 1}") or 0
        return int(cur) + int(prev)

    async def add_connection(self, key: str, *, ttl: int) -> int:
        client = self._client()
        conn_key = f"rlc:{key}"
        cur = await client.incr(conn_key)
        await client.expire(conn_key, ttl)
        return int(cur)

    async def remove_connection(self, key: str) -> int:
        client = self._client()
        conn_key = f"rlc:{key}"
        cur = await client.decr(conn_key)
        if cur <= 0:
            await client.delete(conn_key)
            return 0
        return int(cur)

    async def get_connections(self, key: str) -> int:
        client = self._client()
        cur = await client.get(f"rlc:{key}")
        return int(cur) if cur else 0


# ============================================================
#  限流器（全异步）
# ============================================================


class RateLimiter:
    """双维度限流器（频率 + SSE 并发连接）。

    :param store: 存储（默认内存；生产传 ``RedisRateLimitStore``）
    :param settings_obj: 配置对象（默认全局 ``settings``，便于测试替换阈值）
    """

    def __init__(self, store: RateLimitStore | None = None, *, settings_obj: Any | None = None) -> None:
        self.store: RateLimitStore = store or InMemoryRateLimitStore()
        self.s = settings_obj or settings

    # ---------- 白名单 ----------

    def is_whitelisted(self, *, ip: str = "", token: str = "") -> bool:
        """白名单命中即不限流。"""
        if ip and ip in self.s.ratelimit_whitelist_ip_list:
            return True
        if token and token in self.s.ratelimit_whitelist_token_list:
            return True
        return False

    # ---------- 频率（滑动窗口）----------

    async def check_request(self, *, ip: str = "", session_id: str = "", token: str = "") -> RateLimitDecision:
        """检查一次请求频率（IP 分钟/每日 + 会话 分钟/每日）。

        命中任一维度超限 → 返回不允许（携带对应 scope / retry_after）。
        """
        if self.is_whitelisted(ip=ip, token=token):
            return RateLimitDecision(allowed=True, reason="whitelist")

        checks: list[tuple[str, str, int, int]] = []
        if ip:
            checks.append((f"ip:{ip}", "ip_minute", self.s.rate_limit_ip_per_min, WINDOW_MINUTE))
            checks.append((f"ipd:{ip}", "ip_day", self.s.rate_limit_ip_per_day, WINDOW_DAY))
        if session_id:
            checks.append((f"ss:{session_id}", "session_minute", self.s.rate_limit_session_per_min, WINDOW_MINUTE))
            checks.append((f"ssd:{session_id}", "session_day", self.s.rate_limit_session_per_day, WINDOW_DAY))

        for key, scope, limit, window in checks:
            try:
                current = await self.store.incr_window(key, window)
            except Exception as exc:  # noqa: BLE001 - 存储故障 → fail-open
                logger.warning("限流存储异常（放行）scope=%s err=%r", scope, exc)
                return RateLimitDecision(allowed=True, reason="store_error")
            if current > limit:
                retry = self._retry_after(window)
                logger.warning("触发限流 scope=%s key=%s current=%s limit=%s", scope, key, current, limit)
                return RateLimitDecision(
                    allowed=False,
                    scope=scope,
                    limit=limit,
                    current=current,
                    retry_after=retry,
                    reason=f"请求过于频繁（{scope} 超过 {limit} 次）",
                )
        return RateLimitDecision(allowed=True)

    # ---------- 自定义维度（注册 / 登录失败）----------

    async def check_scope(
        self, key: str, scope: str, limit: int, window: int, *, count: bool = True
    ) -> RateLimitDecision:
        """检查任意自定义维度（``limit <= 0`` 视为不限）。

        供注册/登录等**独立阈值**场景复用，避免每个场景都写一遍计数逻辑。

        :param count: 是否自增计数；``False`` 表示**只读探测**（如登录前先查该 IP 是否已被封）。
        """
        if limit <= 0:
            return RateLimitDecision(allowed=True, reason="disabled")
        try:
            current = await self.store.incr_window(key, window) if count else await self.store.get_window(key, window)
        except Exception as exc:  # noqa: BLE001 - 存储故障 → fail-open
            logger.warning("限流存储异常（放行）scope=%s err=%r", scope, exc)
            return RateLimitDecision(allowed=True, reason="store_error")
        if current > limit:
            logger.warning("触发限流 scope=%s key=%s current=%s limit=%s", scope, key, current, limit)
            return RateLimitDecision(
                allowed=False,
                scope=scope,
                limit=limit,
                current=current,
                retry_after=self._retry_after(window),
                reason=f"操作过于频繁（{scope} 超过 {limit} 次）",
            )
        # 放行也带 current：调用方可能需要「达到阈值即封锁」的只读探测语义
        return RateLimitDecision(allowed=True, scope=scope, limit=limit, current=current)

    async def check_register(self, *, ip: str = "") -> RateLimitDecision:
        """注册限频（单 IP：小时窗口 + 日窗口）——防脚本批量建号。"""
        if self.is_whitelisted(ip=ip):
            return RateLimitDecision(allowed=True, reason="whitelist")
        for key, scope, limit, window in (
            (f"reg:{ip}", "register_hour", self.s.rate_limit_register_per_hour, WINDOW_HOUR),
            (f"regd:{ip}", "register_day", self.s.rate_limit_register_per_day, WINDOW_DAY),
        ):
            decision = await self.check_scope(key, scope, limit, window)
            if not decision.allowed:
                return decision
        return RateLimitDecision(allowed=True)

    async def check_login_fail(self, *, ip: str = "") -> RateLimitDecision:
        """登录失败限频（单 IP：小时窗口）——防撞库爆破；超限时登录直接拒绝。"""
        if self.is_whitelisted(ip=ip):
            return RateLimitDecision(allowed=True, reason="whitelist")
        return await self.check_scope(
            f"loginf:{ip}",
            "login_fail_hour",
            self.s.rate_limit_login_fail_per_hour,
            WINDOW_HOUR,
        )

    def _retry_after(self, window: int) -> int:
        """给出建议重试等待：不短于配置值，也不超过窗口长度。"""
        cfg = int(getattr(self.s, "rate_limit_retry_after", 60))
        if window < cfg:
            return max(1, window)
        return cfg

    # ---------- SSE 并发连接 ----------

    async def acquire_connection(self, *, ip: str = "", session_id: str = "", token: str = "") -> RateLimitDecision:
        """申请一条 SSE 长连接（并发数 +1）。

        超限时**回滚已占用**的连接数，直接返回不允许。命中白名单不计数。
        """
        if self.is_whitelisted(ip=ip, token=token):
            return RateLimitDecision(allowed=True, reason="whitelist")

        limit = self.s.rate_limit_sse_concurrent
        ttl = int(self.s.rate_limit_sse_max_seconds) + 60  # TTL 兜底防连接泄漏
        keys = [f"c:{ip}"] if ip else []
        if session_id:
            keys.append(f"cs:{session_id}")

        acquired: list[str] = []
        try:
            for key in keys:
                current = await self.store.add_connection(key, ttl=ttl)
                if current > limit:
                    # 回滚：把刚加的以及之前加的连接数都减回去，保持原子性
                    await self.store.remove_connection(key)
                    for done_key in acquired:
                        await self.store.remove_connection(done_key)
                    logger.warning("SSE 并发连接超限 key=%s current=%s limit=%s", key, current, limit)
                    return RateLimitDecision(
                        allowed=False,
                        scope="sse_concurrent",
                        limit=limit,
                        current=current,
                        retry_after=int(self.s.rate_limit_retry_after),
                        reason=f"当前流式会话过多（上限 {limit}），请稍后重试",
                    )
                acquired.append(key)
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning("SSE 连接计数异常（放行）：%r", exc)
            return RateLimitDecision(allowed=True, reason="store_error")
        return RateLimitDecision(allowed=True)

    async def release_connection(self, *, ip: str = "", session_id: str = "", token: str = "") -> None:
        """释放一条 SSE 长连接（并发数 -1）。白名单未计数，无需释放。"""
        if self.is_whitelisted(ip=ip, token=token):
            return
        keys = [f"c:{ip}"] if ip else []
        if session_id:
            keys.append(f"cs:{session_id}")
        for key in keys:
            try:
                await self.store.remove_connection(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SSE 连接释放异常（忽略）：%r", exc)

    async def current_connections(self, *, ip: str = "", session_id: str = "") -> int:
        """读当前连接数（供 /health 或管理后台展示）。"""
        keys = [f"c:{ip}"] if ip else []
        if session_id:
            keys.append(f"cs:{session_id}")
        values: list[int] = []
        for k in keys:
            try:
                values.append(await self.store.get_connections(k))
            except Exception:  # noqa: BLE001
                values.append(0)
        return max(values, default=0)

    # ---------- 便捷：超限即抛 ----------

    async def enforce_request(self, *, ip: str = "", session_id: str = "", token: str = "") -> None:
        """检查请求频率，超限直接抛 ``RateLimitError``（携带 Retry-After）。"""
        decision = await self.check_request(ip=ip, session_id=session_id, token=token)
        if not decision.allowed:
            raise RateLimitError(decision.reason, detail={"retry_after": decision.retry_after, "scope": decision.scope})

    async def enforce_connection(self, *, ip: str = "", session_id: str = "", token: str = "") -> None:
        """申请 SSE 连接，超限直接抛 ``RateLimitError``。"""
        decision = await self.acquire_connection(ip=ip, session_id=session_id, token=token)
        if not decision.allowed:
            raise RateLimitError(decision.reason, detail={"retry_after": decision.retry_after, "scope": decision.scope})


# ============================================================
#  单例
# ============================================================

_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """获取全局限流器单例（生产用 Redis 存储；Redis 不可用时 fail-open）。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(RedisRateLimitStore())
        logger.info("限流器已初始化（Redis 存储）")
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """替换全局限流器（测试用）。"""
    global _limiter
    _limiter = limiter


def client_ip(request: Any) -> str:
    """从请求中提取客户端 IP（优先反代头 ``X-Forwarded-For`` 首段，回落 ``request.client``）。"""
    try:
        hdr = request.headers.get("x-forwarded-for", "") or ""
    except Exception:  # noqa: BLE001
        hdr = ""
    if hdr:
        return hdr.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""
