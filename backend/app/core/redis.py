"""Redis 客户端（缓存 / 会话热态 / 流式缓冲 / 限流）。

T01 仅提供连接与基础能力；具体用途在后续卡（T17 会话、T19 缓冲、T20 限流）实现。

T30 修复（E2E 发现的真实缺陷）
--------------------------------
旧桥接把协程丢进 ``asyncio.run`` 的**临时事件循环**执行，协程内的 Redis 命令会把
共享连接池里的连接绑定到该临时循环；临时循环关闭后连接即死亡，主循环复用时抛
``RuntimeError: Event loop is closed``——限流存储 fail-open，等于**限流可被间歇绕过**。
修法：``run_sync`` 每次调用创建**专用临时客户端**（用完即关），与共享单例完全隔离；
`get_redis()` 在桥接线程内返回该专用客户端（线程局部 override）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from redis.asyncio import Redis, from_url

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None
_local = threading.local()  # 桥接线程专用客户端（override），见 run_sync


def get_redis() -> Redis:
    """获取全局 Redis 客户端（懒加载单例，异步）。

    在 ``run_sync`` 的桥接线程内，返回该次调用专用的临时客户端（线程局部）。
    """
    override = getattr(_local, "client", None)
    if override is not None:
        return override
    global _client
    if _client is None:
        _client = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
        )
        logger.info("Redis 客户端已初始化: %s", settings.redis_url)
    return _client


def run_sync(make_coro: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """同步上下文里执行协程（**专用临时 Redis 客户端**，绝不污染共享连接池）。

    :param make_coro: 协程工厂（**不是已创建的协程**）——必须在桥接线程内调用，
        这样 ``get_redis()`` 才会解析到本次专用的临时客户端；
    有运行中事件循环（async 路由内）→ 独立线程执行（不嵌套循环）；无循环 → 当前线程直接跑。
    """
    def _go() -> Any:
        client = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
        )
        _local.client = client
        try:
            async def _run() -> Any:
                try:
                    return await make_coro()
                finally:
                    await client.aclose()

            return asyncio.run(_run())
        finally:
            _local.client = None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _go()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_go).result()


async def close_redis() -> None:
    """关闭连接（应用退出时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Redis 连接已关闭")
