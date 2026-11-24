"""T19 · SSE 事件编码与流式缓冲。

职责
----
把对话流水的各类「事件」编码成标准 SSE 报文（``id:`` / ``event:`` / ``data:``），
并提供 Redis 流式缓冲（供 T21 断点续写）。

事件类型（**格式固定，红线**）
------------------------------
- ``meta``     开始：``{stream_id, session_id, intent}``
- ``token``    增量文本：``{text}``（**增量，非全量**）
- ``citation`` 引用：``[{index, title, url, year}]``
- ``ping``     心跳（防代理超时）
- ``done``     结束：``{message_id, model, mocked, grounded, fallback, disclaimer}``
- ``error``    错误：``{code, message}``

红线
----
- 事件格式固定为 ``id:`` / ``event:`` / ``data:``（SSE 规范）；
- **必须发 ``ping`` 心跳**（无 token 期间也要发，防代理超时断流）；
- **不得一次性返回整段再切片**（要真流式）—— 编码函数只负责单事件，流式由生成器控制；
- ``done`` 事件**必须**带 ``message_id``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 允许的事件类型（白名单，防拼写错误）
ALLOWED_EVENTS: frozenset[str] = frozenset({"meta", "token", "citation", "ping", "done", "error"})


def encode_sse(event: str, data: Any, *, event_id: int | None = None) -> str:
    """把一条事件编码成 SSE 报文。

    格式（SSE 规范）：``id: <n>\\nevent: <name>\\ndata: <json>\\n\\n``。

    :param event: 事件名（必须在 ``ALLOWED_EVENTS`` 内）
    :param data: 事件负载（自动 JSON 编码，``ensure_ascii=False``）
    :param event_id: 可选的事件序号（用于前端去重/续写定位）
    """
    if event not in ALLOWED_EVENTS:
        raise ValueError(f"未允许的 SSE 事件类型：{event}")
    payload = json.dumps(data, ensure_ascii=False)
    # 多行 data 需逐行加前缀（这里 JSON 单行，仍按规范处理换行）
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    for line in payload.split("\n"):
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def encode_meta(stream_id: str, session_id: str, intent: str = "", event_id: int = 0) -> str:
    return encode_sse(
        "meta",
        {"stream_id": stream_id, "session_id": session_id, "intent": intent},
        event_id=event_id,
    )


def encode_token(text: str, event_id: int | None = None) -> str:
    """编码一个**增量** token（非全量）。"""
    return encode_sse("token", {"text": text}, event_id=event_id)


def encode_citation(citations: list[dict[str, Any]], event_id: int | None = None) -> str:
    return encode_sse("citation", citations, event_id=event_id)


def encode_ping(event_id: int | None = None) -> str:
    return encode_sse("ping", {"ts": None}, event_id=event_id)


def encode_done(
    message_id: int | str,
    *,
    model: str = "",
    mocked: bool = False,
    grounded: bool = True,
    fallback: bool = False,
    disclaimer: str = "",
    event_id: int | None = None,
) -> str:
    """编码结束事件。**必须带 ``message_id``**（红线）。"""
    return encode_sse(
        "done",
        {
            "message_id": message_id,
            "model": model,
            "mocked": mocked,
            "grounded": grounded,
            "fallback": fallback,
            "disclaimer": disclaimer,
        },
        event_id=event_id,
    )


def encode_error(code: str, message: str, event_id: int | None = None) -> str:
    return encode_sse("error", {"code": code, "message": message}, event_id=event_id)


# ============================================================
#  流式缓冲（Redis，供 T21 断点续写）
# ============================================================


@dataclass
class StreamBuffer:
    """流式缓冲（内存实现；生产可换 Redis）。

    T19 只做「写入 + 计数」；T21 会据此实现断点续写（客户端带 ``Last-Event-ID`` 重连）。
    """

    stream_id: str
    tokens: list[str] = field(default_factory=list)
    finished: bool = False

    def append(self, text: str) -> None:
        self.tokens.append(text)

    @property
    def token_count(self) -> int:
        return len(self.tokens)

    @property
    def content(self) -> str:
        return "".join(self.tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "token_count": self.token_count,
            "content": self.content,
            "finished": self.finished,
        }


class InMemoryStreamStore:
    """内存流缓冲存储（测试/单进程用）。"""

    def __init__(self) -> None:
        self._buffers: dict[str, StreamBuffer] = {}

    def create(self, stream_id: str) -> StreamBuffer:
        buf = StreamBuffer(stream_id=stream_id)
        self._buffers[stream_id] = buf
        return buf

    def get(self, stream_id: str) -> StreamBuffer | None:
        return self._buffers.get(stream_id)

    def delete(self, stream_id: str) -> None:
        self._buffers.pop(stream_id, None)


class RedisStreamStore:
    """Redis 流缓冲存储（生产）。

    - 键 ``stream:{stream_id}``（hash：``content`` / ``token_count`` / ``finished``）；
    - TTL 1 小时；
    - **Redis 不可用时静默降级**（返回 None / 忽略写失败），不阻塞 SSE。
    """

    def __init__(self, redis_client: Any | None = None, *, ttl: int = 3600) -> None:
        self._redis = redis_client
        self.ttl = ttl

    def _client(self) -> Any:
        if self._redis is None:
            from app.core.redis import get_redis

            self._redis = get_redis()
        return self._redis

    def create(self, stream_id: str) -> StreamBuffer:
        return StreamBuffer(stream_id=stream_id)

    def get(self, stream_id: str) -> StreamBuffer | None:
        try:
            client = self._client()
            data = client.hgetall(f"stream:{stream_id}")
            if not data:
                return None
            buf = StreamBuffer(stream_id=stream_id)
            buf.tokens = [data.get("content", "")] if data.get("content") else []
            buf.finished = str(data.get("finished", "0")) == "1"
            return buf
        except Exception:  # noqa: BLE001 - 缓存是优化不是依赖
            return None

    def delete(self, stream_id: str) -> None:
        try:
            self._client().delete(f"stream:{stream_id}")
        except Exception:  # noqa: BLE001
            pass
