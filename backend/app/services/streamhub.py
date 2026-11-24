"""T21 · 流式对话中心枢纽（StreamHub）—— 断点续写的核心。

设计（关键决策）
----------------
把「生成」与「连接」**解耦**：

- 生成（`run_chat_stream`）跑在**独立的后台任务**里，产出的事件按序追加进
  进程内事件日志（`LiveStream.events`）——**即使客户端断开，生成也不会中断**；
- HTTP 连接只是事件日志的一个「读者」：断连后凭 ``stream_id + last_event_id``
  重连续读（`GET /api/chat/{stream_id}/resume?last_event_id=N`），**不重复、不丢字**；
- 读者空闲等待时由 ``cond`` 挂起，超时则产出 ``ping`` 心跳（与 T19 行为一致）。

为什么不用 Redis 作为续写缓冲
------------------------------
事件日志需要**高频原子追加 + 条件唤醒**，Redis List 轮询延迟高且无推送语义；
本项目为**单实例部署**（uvicorn 单进程），进程内日志即已满足「断连后生成不中断」。
``StreamStore``（T19，Redis 可观测缓冲）仍按原样保留，作为跨进程观测/排障快照。

红线
----
- 续写**不重复、不丢字**：事件带全局递增 ``event_id``，读者严格按 ``> last_event_id`` 过滤；
- 断连不丢生成：生产者不感知读者生命周期；
- 内存有界：完成后保留 ``TTL`` 秒供续写，超量淘汰最旧的已完成流。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.services.chat import ChatDeps, run_chat_stream

logger = get_logger(__name__)

# 完成后的流保留时长（秒）——供断线重连续写；超时清理
STREAM_TTL_SECONDS = 600
# 内存上限（最多同时保留多少条流日志，超量淘汰最旧已完成的）
MAX_LIVE_STREAMS = 200
# 心跳间隔（秒），与 api/chat.py 保持一致
PING_INTERVAL = 1.0

_TERMINAL_EVENTS = ("done", "error")


def _event_name(raw: str) -> str:
    """从 SSE 帧中解析事件名（帧首行是 ``id:``，事件名在 ``event:`` 行）。"""
    for line in raw.splitlines():
        if line.startswith("event: "):
            return line[len("event: ") :].strip()
    return ""


@dataclass
class LiveStream:
    """一条正在生成（或已完成）的流的事件日志。"""

    stream_id: str
    session_id: str = ""
    owner_username: str | None = None
    # (event_id, raw_sse) 按序追加；event_id 与 SSE 帧内 `id:` 一致
    events: list[tuple[int, str]] = field(default_factory=list)
    next_id: int = 0
    finished: bool = False
    failed: bool = False
    created_at: float = field(default_factory=time.time)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None  # 生产者任务引用（便于测试与观测）

    @property
    def last_event_id(self) -> int:
        return self.next_id - 1


class StreamHub:
    """流中心枢纽：启动生成、按位置续读。

    :param deps_factory: 返回 ``ChatDeps`` 的零参函数（懒构造，失败返回 None → error 事件）
    """

    def __init__(self, deps_factory=None) -> None:
        self._streams: dict[str, LiveStream] = {}
        self._deps_factory = deps_factory or _default_deps_factory
        self.ping_interval = PING_INTERVAL
        self.stream_ttl = STREAM_TTL_SECONDS
        self.max_streams = MAX_LIVE_STREAMS

    # ---------------- 生命周期 ----------------

    def get(self, stream_id: str) -> LiveStream | None:
        return self._streams.get(stream_id)

    def _evict_if_needed(self) -> None:
        """内存有界：超量时先淘汰最旧的已完成流；仍超量再淘汰最旧的。"""
        if len(self._streams) <= self.max_streams:
            return
        finished = [s for s in self._streams.values() if s.finished]
        for s in sorted(finished, key=lambda x: x.created_at):
            self._streams.pop(s.stream_id, None)
            if len(self._streams) <= self.max_streams:
                return
        for s in sorted(self._streams.values(), key=lambda x: x.created_at):
            self._streams.pop(s.stream_id, None)
            if len(self._streams) <= self.max_streams:
                return

    def cleanup_expired(self) -> int:
        """清理已过保留期的流（返回清理数；测试与后台维护用）。"""
        now = time.time()
        expired = [sid for sid, s in self._streams.items() if s.finished and (now - s.created_at) >= self.stream_ttl]
        for sid in expired:
            self._streams.pop(sid, None)
        return len(expired)

    # ---------------- 生产：启动一条流 ----------------

    async def start(
        self,
        question: str,
        *,
        session_id: str,
        stream_id: str | None = None,
        owner_username: str | None = None,
    ) -> LiveStream:
        """启动一条新的流式对话（生产者后台任务即刻开跑，与读者无关）。"""
        sid = stream_id or uuid.uuid4().hex[:16]
        stream = LiveStream(
            stream_id=sid,
            session_id=session_id,
            owner_username=owner_username,
        )
        self._streams[sid] = stream
        self._evict_if_needed()
        stream.task = asyncio.ensure_future(self._produce(stream, question))
        return stream

    async def _produce(self, stream: LiveStream, question: str) -> None:
        """生产者：消费 run_chat_stream，把每条事件按序写入日志。

        任何异常（含依赖不可用）都落成 error 事件——**绝不裸抛**（红线）。
        """
        deps: ChatDeps | None = None
        try:
            deps = self._deps_factory()
        except Exception as exc:  # noqa: BLE001 - 依赖构造失败 → error 事件收尾
            logger.warning("StreamHub 依赖不可用：%r", exc)

        agen = (
            run_chat_stream(
                question,
                session_id=stream.session_id,
                deps=deps,
                stream_id=stream.stream_id,
                owner_username=stream.owner_username,
            )
            if deps
            else _deps_unavailable()
        )
        try:
            async for raw in agen:
                async with stream.cond:
                    stream.events.append((stream.next_id, raw))
                    stream.next_id += 1
                    name = _event_name(raw)
                    if name in _TERMINAL_EVENTS:
                        stream.finished = True
                        if name == "error":
                            stream.failed = True
                    stream.cond.notify_all()
        except Exception as exc:  # noqa: BLE001 - 兜底：生产异常转 error 事件
            logger.error("StreamHub 生成异常：%r", exc, exc_info=True)
            from app.agent.sse import encode_error

            async with stream.cond:
                stream.events.append((stream.next_id, encode_error("STREAM_ERROR", "服务暂时不可用，请稍后重试")))
                stream.next_id += 1
                stream.finished = True
                stream.failed = True
                stream.cond.notify_all()
        finally:
            async with stream.cond:
                # 若产出被意外截断（无 done/error 收尾），补一条 error 保证读者可退出
                if not stream.finished:
                    from app.agent.sse import encode_error

                    stream.events.append(
                        (stream.next_id, encode_error("STREAM_TRUNCATED", "生成中断，请重试或稍后续写"))
                    )
                    stream.next_id += 1
                    stream.finished = True
                    stream.failed = True
                stream.cond.notify_all()
            # 释放本次生产自建的 DB 会话（每次流各自创建，用完即关，避免连接泄漏）
            if deps is not None:
                try:
                    deps.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不得影响读者
                    logger.warning("对话依赖关闭失败（忽略）：%r", exc)

    # ---------------- 消费：从任意位置续读 ----------------

    async def read(
        self,
        stream_id: str,
        *,
        last_event_id: int = -1,
        max_seconds: int = 0,
    ) -> AsyncIterator[str]:
        """从 ``last_event_id`` 之后开始读取事件（**严格 > 过滤，不重复、不丢字**）。

        - 已产生的事件立即回放（断线重连的首屏快照）；
        - 未完成则等待新事件，空闲超 ``ping_interval`` 产出心跳；
        - ``max_seconds`` 连接存活上限（T20）：超时产出 STREAM_TIMEOUT error 并结束。
        - 流不存在 → 产出 STREAM_NOT_FOUND error 事件（仍是合法 SSE，不裸抛 404）。
        """
        from app.agent.sse import encode_error, encode_ping

        stream = self._streams.get(stream_id)
        if stream is None:
            yield encode_error("STREAM_NOT_FOUND", "会话流已过期或不存在，请重新提问")
            return

        pos = 0
        started = time.time()
        while True:
            batch: list[str] = []
            timed_out = False
            async with stream.cond:
                while pos < len(stream.events):
                    eid, raw = stream.events[pos]
                    if eid > last_event_id:
                        batch.append(raw)
                    pos += 1
                done = stream.finished and pos >= len(stream.events)
                if not done:
                    try:
                        await asyncio.wait_for(stream.cond.wait(), timeout=self.ping_interval)
                    except TimeoutError:
                        if max_seconds and (time.time() - started) > max_seconds:
                            timed_out = True
                        else:
                            batch.append(encode_ping())
            if timed_out:
                yield encode_error("STREAM_TIMEOUT", "会话时长已达上限，已自动结束，可重新提问继续")
                return
            for raw in batch:
                yield raw
                if _event_name(raw) in _TERMINAL_EVENTS:
                    return
            if done:
                return

    def is_finished(self, stream_id: str) -> bool:
        s = self._streams.get(stream_id)
        return bool(s and s.finished)


async def _deps_unavailable():
    """依赖不可用的替代生成器：单条 error 事件收尾。"""
    from app.agent.sse import encode_error

    yield encode_error("DEPS_UNAVAILABLE", "服务依赖暂不可用，请稍后重试或使用降级模式")


def _default_deps_factory() -> ChatDeps:
    """默认生产依赖（复用 chat 服务懒构造；失败抛异常由 _produce 兜底）。"""
    from app.services.chat import get_chat_deps

    return get_chat_deps()


# ============================================================
#  单例
# ============================================================

_hub: StreamHub | None = None


def get_stream_hub() -> StreamHub:
    global _hub
    if _hub is None:
        _hub = StreamHub()
        logger.info("StreamHub 已初始化")
    return _hub


def set_stream_hub(hub: StreamHub | None) -> None:
    """替换全局 hub（测试用）。"""
    global _hub
    _hub = hub
