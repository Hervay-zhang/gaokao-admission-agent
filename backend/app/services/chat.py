"""T19 · 对话编排（路由 → 政策问答/推荐 → 后置校验 → SSE 事件流）。

职责
----
把 T16（路由）、T14（RAG 政策问答）、T18（Prompt/Guard）串成**一条流式对话链路**，
并以**异步生成器**逐个产出 SSE 事件（真流式，非一次性切片）。

流程
----
1. ``meta``     事件：告知 ``stream_id`` / ``session_id`` / 路由出的 ``intent``；
2. 分派：
   - ``out_of_scope`` → 直接产出固定拒答话术（**不检索、不调 LLM**）；
   - ``recommend``    → 缺参则产出澄清话术；否则提示走推荐接口（本卡聚焦政策问答流式）；
   - ``policy`` / ``school`` → 走 ``PolicyQA`` 检索 + 生成；
3. ``token``    事件：把最终文本**按句/片段**增量推送；
4. ``citation`` 事件：产出引用（**由检索结果构造**，非模型）；
5. ``done``     事件：带 ``message_id`` 等元数据。

红线
----
- **真流式**：token 逐个产出（本卡 MOCK 下按固定切片模拟，真实模式走 ``stream_chat``）；
- **心跳**：无 token 期间由 SSE 端点补 ``ping``（见 ``app/api/chat.py``）；
- **越界不检索**：``out_of_scope`` 直接拒答，不消耗检索/LLM 资源；
- **免责与引用**：所有路径都经 ``guard.post_validate`` 补免责（前置于 RAG 的免责先手）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.agent import guard as guard_mod
from app.agent import prompt as prompt_mod
from app.agent.router import (
    OUT_OF_SCOPE_REPLY,
    Intent,
    Router,
    RouteResult,
    _clarify_for,
    _missing_for_recommend,
    get_router,
)
from app.agent.sse import (
    ALLOWED_EVENTS,  # noqa: F401 - re-export for tests
    StreamBuffer,
    encode_citation,
    encode_done,
    encode_error,
    encode_meta,
    encode_token,
)
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# token 切片大小（MOCK/降级模式：把最终文本按此长度切分模拟流式）
CHUNK_SIZE = 6
PENDING_CLARIFY_TTL_SECONDS = 600
_PENDING_CLARIFY: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class ChatDeps:
    """对话依赖（可注入，便于测试离线复现）。

    自建资源（``qa`` / ``lookup`` 内部创建 DB 会话）在使用结束后统一由
    ``close()`` 释放；外部注入的实现不提供 ``close`` 时自动跳过。
    """

    router: Router
    qa: Any                        # PolicyQA（duck-typed：有 .answer/_fallback）
    session: Any | None = None     # SessionService（可选）
    lookup: guard_mod.UniversityLookup | None = None
    stream_store: Any | None = None

    def close(self) -> None:
        """释放自建 DB 会话（幂等；外部注入的 db 不关闭）。"""
        for dep in (self.qa, self.lookup, self.session):
            closer = getattr(dep, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不得冒泡
                    logger.warning("对话依赖资源关闭失败（忽略）：%r", exc)


def _chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """把文本切成若干片段（MOCK/降级模式的「伪流式」；真实模式由 LLM 逐 token 返回）。"""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


async def run_chat_stream(
    question: str,
    *,
    session_id: str,
    deps: ChatDeps,
    stream_id: str | None = None,
    owner_username: str | None = None,
) -> AsyncIterator[str]:
    """执行一次流式对话，逐个产出 SSE 报文。

    **真流式**：每个 ``yield`` 是一条完整的 SSE 事件；调用方（FastAPI）直接转发。
    """
    stream_id = stream_id or uuid.uuid4().hex[:16]
    buf = StreamBuffer(stream_id=stream_id)
    event_id = 0

    try:
        # ---- 1) 路由 ----
        route = await deps.router.route(question)
        pending = _pop_pending_clarify(session_id)
        if pending:
            route = _merge_pending_route(route, pending)
        yield encode_meta(stream_id, session_id, intent=route.intent, event_id=event_id)
        event_id += 1

        # ---- 2) 分派 ----
        if route.intent == Intent.OUT_OF_SCOPE.value:
            final_text = route.out_of_scope_reply or OUT_OF_SCOPE_REPLY
            citations: list[dict[str, Any]] = []
            model, mocked, grounded, fallback = "", False, False, False
        elif route.need_clarification:
            _save_pending_clarify(session_id, route.params)
            final_text = route.clarify_question
            citations = []
            model, mocked, grounded, fallback = "", False, False, False
        elif route.intent == Intent.RECOMMEND.value:
            final_text = await _recommend_answer(route.params)
            citations = []
            model, mocked, grounded, fallback = "recommend-engine", False, True, False
        else:
            qa_answer = await deps.qa.answer(question)
            final_text = qa_answer.answer
            citations = [c.to_dict() for c in qa_answer.citations]
            model = qa_answer.model
            mocked = qa_answer.mocked
            grounded = qa_answer.grounded
            fallback = qa_answer.fallback

        # ---- 3) 后置校验（免责兜底 / 越界 / 编造标记）----
        guard_result = guard_mod.post_validate(final_text, lookup=deps.lookup)
        final_text = guard_result.answer

        # ---- 4) token 流（增量）----
        for piece in _chunk_text(final_text):
            buf.append(piece)
            yield encode_token(piece, event_id=event_id)
            event_id += 1

        # ---- 5) 引用 ----
        if citations:
            yield encode_citation(citations, event_id=event_id)
            event_id += 1

        # ---- 6) 会话落库（可选）----
        message_id: int | str = -1
        if deps.session is not None:
            try:
                view = deps.session.append_turn(
                    session_id,
                    question,
                    final_text,
                    intent=route.intent,
                    citations=citations,
                    model=model,
                    mocked=mocked,
                    owner_username=owner_username,
                )
                if view is not None and view.messages:
                    message_id = view.messages[-1].seq
            except Exception as exc:  # noqa: BLE001 - 落库失败不得中断流
                logger.warning("会话落库失败（忽略）：%r", exc)

        # ---- 7) done ----
        buf.finished = True
        yield encode_done(
            message_id,
            model=model,
            mocked=mocked,
            grounded=grounded,
            fallback=fallback,
            disclaimer=prompt_mod.DISCLAIMER,
            event_id=event_id,
        )
    except Exception as exc:  # noqa: BLE001 - SSE 内异常必须以 error 事件收尾，绝不能裸抛
        logger.error("流式对话异常：%r", exc, exc_info=True)
        yield encode_error("STREAM_ERROR", "服务暂时不可用，请稍后重试", event_id=event_id)


def _save_pending_clarify(session_id: str, params: dict[str, Any]) -> None:
    if params:
        _PENDING_CLARIFY[session_id] = (time.time() + PENDING_CLARIFY_TTL_SECONDS, dict(params))


def _pop_pending_clarify(session_id: str) -> dict[str, Any]:
    item = _PENDING_CLARIFY.pop(session_id, None)
    if item is None:
        return {}
    expires_at, params = item
    return params if expires_at >= time.time() else {}


def _merge_pending_route(route: RouteResult, pending: dict[str, Any]) -> RouteResult:
    """把上一轮缺参问题保存的参数合并到本轮路由结果。"""
    params = {**pending, **route.params}
    if "score" not in params and "rank" not in params:
        return route
    missing = _missing_for_recommend(params)
    merged = RouteResult(
        intent=Intent.RECOMMEND.value,
        confidence=max(route.confidence, 0.9),
        params=params,
        source="pending",
        reason="合并上一轮推荐澄清参数",
    )
    if missing:
        merged.need_clarification = True
        merged.clarify_question = _clarify_for(missing)
    return merged


async def _recommend_answer(params: dict[str, Any]) -> str:
    """执行结构化推荐；数据未覆盖省份时明确说明，不退化成政策 RAG 兜底。"""
    province = str(params["province"])
    subject_type = str(params["subject_type"])
    score = int(params["score"])
    rank = params.get("rank")
    electives = list(params.get("electives") or [])

    if rank is None:
        from app.services.score import get_score_engine

        def _rank() -> int:
            result = get_score_engine().query_rank(settings.current_year, province, subject_type, score)
            if result.rank is None:
                raise RuntimeError("rank unavailable")
            return result.rank

        try:
            rank = await asyncio.to_thread(_rank)
        except Exception:  # noqa: BLE001
            return (
                f"目前库内尚未接入{province}的一分一段与投档数据，"
                f"因此不能可靠判断{score}分/{subject_type}类能上哪些学校。"
                "请以该省教育考试院官方数据为准；目前可完整演示的是广东省。"
            )

    try:
        from app.services.fallback import get_fallback_service

        result = await asyncio.to_thread(
            lambda: get_fallback_service().fallback(
                score=score,
                subject_type=subject_type,
                electives=electives,
                province=province,
                rank=rank,
                current_year=settings.current_year,
                tier_limit=5,
            )
        )
    except Exception:  # noqa: BLE001
        return (
            f"目前库内尚未接入{province}的完整招生计划与投档数据，"
            f"暂时不能生成{score}分/{subject_type}类的院校清单。"
            "请以该省教育考试院官方数据为准；目前可完整演示的是广东省。"
        )

    lines = [
        f"{province} {settings.current_year} 年 · {subject_type}类 · {score} 分"
        f"（参考位次约 {rank}）："
    ]
    for tier in result.tiers:
        if not tier.rows:
            continue
        lines.append(f"\n【{tier.label}】")
        for row in tier.rows:
            tags = f"（{row.level_tags}）" if row.level_tags else ""
            ref = f"，参考位次 {row.ref_rank}" if row.ref_rank is not None else "，参考位次待补"
            lines.append(f"- {row.university_name}{tags} · 专业组 {row.group_code}{ref}")
    lines.append(f"\n{result.caliber_text}")
    return "\n".join(lines)


# ============================================================
#  生产便捷构造
# ============================================================


def get_chat_deps() -> ChatDeps:
    """生产构造：SQL 检索 + 真实 LLM + 默认路由 + 会话（DB 不可用时降级）。"""
    from app.rag.qa import get_policy_qa

    qa = get_policy_qa()
    session = None
    lookup = None
    try:
        from app.services.session import SessionService, SqlSessionStore

        session = SessionService(SqlSessionStore(qa.retriever.repo.db))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话服务不可用（忽略）：%r", exc)
    try:
        lookup = guard_mod.get_university_lookup()
    except Exception as exc:  # noqa: BLE001
        logger.warning("院校查找源不可用（忽略）：%r", exc)
    return ChatDeps(router=get_router(), qa=qa, session=session, lookup=lookup)
