"""T19+T21 · 流式对话接口（SSE）+ 断点续写。

- ``GET /api/chat/stream?q=...&session_id=...``                → 全新流
- ``GET /api/chat/stream/{stream_id}/resume?last_event_id=N``  → 从第 N 个事件后续读

事件序列：``meta → token* → citation? → done``（无 token 期间穿插 ``ping`` 心跳）。

实现要点（T21 升级）
--------------------
- **生成与连接解耦**：生成跑在 StreamHub 后台任务里，断连不中断生成；
  连接只是事件日志的读者，断线后带 ``last_event_id`` 重连续读（不重复、不丢字）；
- 心跳 / 连接存活上限 / 限流释放逻辑与 T19/T20 一致；
- 异常兜底：所有错误路径都以 ``error`` 事件收尾，**不裸抛**（保证 SSE 客户端可解析）。

红线
----
- 事件格式固定；``done`` 带 ``message_id``；必须有心跳；不得一次性返回整段再切片；
- 续写**不重复、不丢字**（严格按 ``event_id > last_event_id`` 过滤）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.sse import encode_error
from app.api.deps import get_current_account
from app.core.logging import get_logger
from app.core.ratelimit import client_ip, get_rate_limiter
from app.db.session import get_db
from app.services.streamhub import get_stream_hub

logger = get_logger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class SessionRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=128)


@router.get("/stream", summary="流式对话（SSE 打字机）")
async def chat_stream(
    request: Request,
    q: str = Query(..., min_length=1, description="用户问题"),
    session_id: str = Query("", description="会话 ID（匿名 token；不传则临时会话）"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """SSE 流式对话接口（事件类型见 ``app/agent/sse.py``）。

    限流（T20）：频率（IP + 会话）与 SSE 并发连接超限 → ``429 + Retry-After``。
    """
    account = get_current_account(request, db, required=False)
    return await _start_or_resume(
        request,
        q=q,
        session_id=session_id,
        resume_stream_id=None,
        last_event_id=-1,
        owner_username=account.username if account else None,
    )


@router.get("/stream/{stream_id}/resume", summary="断点续写（带 last_event_id 重连续读）")
async def chat_stream_resume(
    request: Request,
    stream_id: str,
    q: str = Query("", description="原问题（仅用于降级兜底，续写不使用）"),
    last_event_id: int = Query(-1, ge=-1, description="客户端已收到的最后一个事件 id"),
    session_id: str = Query("", description="会话 ID（与原流一致）"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """断点续写：从事件日志的 ``last_event_id`` 之后继续推送（不重复、不丢字）。"""
    account = get_current_account(request, db, required=False)
    return await _start_or_resume(
        request,
        q=q,
        session_id=session_id,
        resume_stream_id=stream_id,
        last_event_id=last_event_id,
        owner_username=account.username if account else None,
    )


async def _start_or_resume(
    request: Request,
    *,
    q: str,
    session_id: str,
    resume_stream_id: str | None,
    last_event_id: int,
    owner_username: str | None = None,
) -> StreamingResponse:
    """公共入口：限流 → 启动/续读 → SSE 响应（释放连接计数的包装在生成器 finally 中）。"""
    sid = session_id or "anonymous"
    ip = client_ip(request)
    token = request.headers.get("x-admin-token", "")
    limiter = get_rate_limiter()
    hub = get_stream_hub()

    # ---- 频率限流（IP / 会话）----
    freq = await limiter.check_request(ip=ip, session_id=sid, token=token)
    if not freq.allowed:
        return _ratelimit_response(freq.retry_after, freq.reason)

    # ---- SSE 并发连接限流（先于依赖检查：连接上限门禁所有流式请求）----
    conn = await limiter.acquire_connection(ip=ip, session_id=sid, token=token)
    if not conn.allowed:
        return _ratelimit_response(conn.retry_after, conn.reason)

    max_seconds = int(limiter.s.rate_limit_sse_max_seconds)

    if resume_stream_id is None:
        # 全新流：后台任务即刻开跑（断连不中断）
        start_kwargs = {"session_id": sid}
        if owner_username:
            start_kwargs["owner_username"] = owner_username
        stream = await hub.start(q, **start_kwargs)
        target_id = stream.stream_id
        resume_from = -1
    else:
        target_id = resume_stream_id
        resume_from = last_event_id

    return _sse_response(_read_with_release(hub, target_id, ip=ip, sid=sid, token=token,
                                             resume_from=resume_from, max_seconds=max_seconds))


def _session_service(db: Session):
    from app.services.session import SessionService, SqlSessionStore

    return SessionService(SqlSessionStore(db))


@router.get("/sessions", summary="当前账号 AI 会话历史")
async def list_chat_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    items = _session_service(db).list_sessions(account.username, limit=limit)
    return {"total": len(items), "items": items}


@router.get("/sessions/{session_id}", summary="读取 AI 会话消息")
async def get_chat_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    view = _session_service(db).get_session(session_id)
    if view is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("会话不存在", detail={"code": "SESSION_NOT_FOUND"})
    sess = _session_service(db).store.get_session(session_id) or {}
    if sess.get("owner_username") != account.username:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("会话不存在", detail={"code": "SESSION_NOT_FOUND"})
    return view.to_dict()


@router.patch("/sessions/{session_id}", summary="重命名 AI 会话")
async def rename_chat_session(
    request: Request,
    session_id: str,
    payload: SessionRenameRequest = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    ok = _session_service(db).rename_session(session_id, account.username, payload.title)
    if not ok:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("会话不存在", detail={"code": "SESSION_NOT_FOUND"})
    return {"ok": True}


@router.delete("/sessions/{session_id}", summary="删除 AI 会话")
async def delete_chat_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    ok = _session_service(db).delete_session(session_id, owner_username=account.username)
    if not ok:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("会话不存在", detail={"code": "SESSION_NOT_FOUND"})
    return {"ok": True}


async def _read_with_release(
    hub,
    stream_id: str,
    *,
    ip: str,
    sid: str,
    token: str,
    resume_from: int,
    max_seconds: int,
) -> AsyncIterator[str]:
    limiter = get_rate_limiter()
    try:
        async for raw in hub.read(stream_id, last_event_id=resume_from, max_seconds=max_seconds):
            yield raw
    finally:
        await limiter.release_connection(ip=ip, session_id=sid, token=token)


def _ratelimit_response(retry_after: int, reason: str) -> StreamingResponse:
    """限流响应：429 + ``Retry-After``（仍以 error 事件体收尾，兼容 SSE 客户端）。"""

    async def _body() -> AsyncIterator[str]:
        yield encode_error("RATE_LIMITED", reason)

    return StreamingResponse(
        _body(),
        status_code=429,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Retry-After": str(retry_after),
            "X-Accel-Buffering": "no",
        },
    )


def _sse_response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 反代禁用缓冲，保证逐 token 到达
        },
    )
