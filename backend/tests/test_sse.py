"""T19 测试 —— SSE 流式接口（事件编码 + 对话编排 + 心跳 + 端点）。

对应任务卡「测试场景（必做）」：
    J1 事件序列正确（meta → token* → citation → done）；
    J2 TTFT < 3s（mock 下更快）；
    J3 前端打字机逐步渲染（非一次性）—— 断言 token 事件**多个**且**增量**；
    J4 无 token 期间有心跳。

补足：
- 事件格式固定（``id:`` / ``event:`` / ``data:``）；``done`` **必须**带 ``message_id``；
- 非法事件类型被拒；
- 越界问题**不检索、不调 LLM**，直接拒答；
- 缺参回问走澄清话术；
- 流内异常以 ``error`` 事件收尾（不裸抛）；
- 心跳：慢生成时注入 ``ping``（用假 QA 制造延迟）；
- 端点返回 ``text/event-stream`` + ``X-Accel-Buffering: no``。

设计：**不依赖 MySQL / Redis / 真实 LLM** —— 注入假 router/QA/session，全离线可复现。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent.router import Intent, RouteResult  # noqa: E402
from app.agent.sse import (  # noqa: E402
    ALLOWED_EVENTS,
    InMemoryStreamStore,
    StreamBuffer,
    encode_citation,
    encode_done,
    encode_error,
    encode_meta,
    encode_ping,
    encode_sse,
    encode_token,
)
from app.services.chat import ChatDeps, _chunk_text, _merge_pending_route, run_chat_stream  # noqa: E402

# ============================================================
#  测试替身
# ============================================================


class _FakeCitation:
    def __init__(self, index: int, title: str, url: str) -> None:
        self.index = index
        self.title = title
        self.url = url

    def to_dict(self) -> dict:
        return {"index": self.index, "doc_id": self.index, "title": self.title, "url": self.url, "year": 2026, "doc_type": "通知"}


class _FakeQAAnswer:
    def __init__(self, answer: str, citations=None, grounded=True, fallback=False) -> None:
        self.answer = answer
        self.citations = citations or []
        self.grounded = grounded
        self.fallback = fallback
        self.model = "mock-model"
        self.mocked = True


class _FakeQA:
    """假政策问答：返回预置答案（可带延迟，构造心跳场景）。"""

    def __init__(self, answer: str, *, citations=None, delay: float = 0.0, raise_exc: bool = False) -> None:
        self._answer = answer
        self._citations = citations or []
        self._delay = delay
        self._raise = raise_exc

    async def answer(self, question: str) -> _FakeQAAnswer:  # noqa: ARG002
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise RuntimeError("boom")
        return _FakeQAAnswer(self._answer, self._citations)


class _FakeRouter:
    def __init__(self, route: RouteResult) -> None:
        self._route = route

    async def route(self, question: str) -> RouteResult:  # noqa: ARG002
        return self._route


def _deps(route: RouteResult, qa: _FakeQA, session=None) -> ChatDeps:
    return ChatDeps(router=_FakeRouter(route), qa=qa, session=session, lookup=None)


async def _collect(gen) -> list[str]:
    return [ev async for ev in gen]


def _event_names(events: list[str]) -> list[str]:
    names: list[str] = []
    for ev in events:
        for line in ev.splitlines():
            if line.startswith("event: "):
                names.append(line[len("event: ") :])
    return names


def _decode_tokens(events: list[str]) -> str:
    """把 token 事件解码并拼接成完整文本（打字机最终呈现的内容）。"""
    import json

    pieces: list[str] = []
    for ev in events:
        if "event: token" not in ev:
            continue
        data_line = next(line for line in ev.splitlines() if line.startswith("data: "))
        pieces.append(json.loads(data_line[len("data: ") :])["text"])
    return "".join(pieces)


# ============================================================
#  J1 · 事件序列
# ============================================================


async def test_j1_event_sequence_meta_token_citation_done():
    route = RouteResult(intent=Intent.POLICY.value, confidence=0.9, source="rule")
    qa = _FakeQA("提前批录取安排在本科批次之前。[1]", citations=[_FakeCitation(1, "通知", "http://x")])
    events = await _collect(run_chat_stream("提前批", session_id="s1", deps=_deps(route, qa)))

    names = _event_names(events)
    assert names[0] == "meta", "首个事件必须是 meta"
    assert names[-1] == "done", "最后一个事件必须是 done"
    assert "token" in names, "应有 token 事件"
    assert "citation" in names, "应有 citation 事件"
    # done 在 citation 之后
    assert names.index("citation") < names.index("done")


async def test_j1_meta_contains_stream_id_and_intent():
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    events = await _collect(run_chat_stream("问题", session_id="s1", deps=_deps(route, _FakeQA("答"))))
    meta = events[0]
    assert "stream_id" in meta
    assert "event: meta" in meta
    assert "policy" in meta


async def test_j1_done_has_message_id():
    """红线：done 事件必须带 message_id。"""
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    events = await _collect(run_chat_stream("问题", session_id="s1", deps=_deps(route, _FakeQA("答"))))
    done = events[-1]
    assert "event: done" in done
    assert "message_id" in done


# ============================================================
#  J2 · TTFT（首 token 时间）
# ============================================================


async def test_j2_first_token_fast():
    """J2：MOCK 下首个 token 事件应很快（< 3s，实测远小于）。"""
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    qa = _FakeQA("根据政策，本科批次填报时间为6月28日。[1]")
    start = asyncio.get_event_loop().time()
    first_token_at: float | None = None
    async for ev in run_chat_stream("填报时间", session_id="s1", deps=_deps(route, qa)):
        if "event: token" in ev:
            first_token_at = asyncio.get_event_loop().time()
            break
    assert first_token_at is not None
    assert (first_token_at - start) < 3.0


# ============================================================
#  J3 · 打字机（多 token，增量）
# ============================================================


async def test_j3_multiple_token_events_incremental():
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    answer = "根据政策，本科批次志愿填报时间为6月28日至7月4日。[1]" + "本建议仅供参考，不作为最终填报承诺"
    qa = _FakeQA(answer)
    events = await _collect(run_chat_stream("填报时间", session_id="s1", deps=_deps(route, qa)))

    tokens = [
        ev for ev in events if "event: token" in ev
    ]
    assert len(tokens) > 1, "应产出多个 token 事件（非一次性）"

    # 断言增量（拼接后等于完整回答，且每个 token 都是片段）
    import json

    pieces: list[str] = []
    for ev in tokens:
        data_line = next(line for line in ev.splitlines() if line.startswith("data: "))
        pieces.append(json.loads(data_line[len("data: ") :])["text"])
    assert "".join(pieces) == answer, "token 增量拼接应等于完整回答"
    # 单个 token 不应等于整段（否则是「一次性切片」）
    assert all(p != answer for p in pieces)


def test_chunk_text_incremental():
    assert _chunk_text("") == []
    chunks = _chunk_text("abcdefghij", size=4)
    assert "".join(chunks) == "abcdefghij"
    assert len(chunks) > 1


# ============================================================
#  J4 · 心跳
# ============================================================


async def test_j4_ping_when_slow_generation():
    """慢生成时，读取端应注入 ping（T21 后由 StreamHub.read 提供心跳）。"""
    from app.services import streamhub as hub_mod

    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    qa = _FakeQA("答案", delay=0.15)  # 生成慢于心跳间隔（0.05s）
    hub = hub_mod.StreamHub(deps_factory=lambda: _deps(route, qa))
    hub.ping_interval = 0.05

    stream = await hub.start("问题", session_id="s1")
    events: list[str] = []
    async for ev in hub.read(stream.stream_id):
        events.append(ev)
        if len(events) > 80:  # 安全阀
            break
    names = _event_names(events)
    assert "ping" in names, "慢生成期间应有心跳"
    await stream.task


async def test_ping_encoding():
    assert "event: ping" in encode_ping()


# ============================================================
#  事件编码（格式固定）
# ============================================================


def test_encode_sse_format():
    out = encode_sse("token", {"text": "你好"}, event_id=3)
    assert "id: 3" in out
    assert "event: token" in out
    assert 'data: {"text": "你好"}' in out
    assert out.endswith("\n\n")


def test_encode_sse_rejects_unknown_event():
    with pytest.raises(ValueError):
        encode_sse("bogus", {"x": 1})


def test_allowed_events_whitelist():
    assert ALLOWED_EVENTS == {"meta", "token", "citation", "ping", "done", "error"}


def test_encoders_smoke():
    assert "event: meta" in encode_meta("sid", "sess", "policy")
    assert "event: token" in encode_token("x")
    assert "event: citation" in encode_citation([{"index": 1}])
    assert "event: done" in encode_done(42)
    assert "message_id" in encode_done(42)
    assert "event: error" in encode_error("E", "msg")


# ============================================================
#  分派：越界 / 缺参
# ============================================================


async def test_out_of_scope_no_qa_call():
    """越界：不调用 QA（不检索、不调 LLM），直接拒答。"""
    route = RouteResult(
        intent=Intent.OUT_OF_SCOPE.value,
        out_of_scope_reply="抱歉，超出范围。",
        source="rule",
    )

    class _ExplodingQA:
        async def answer(self, question):  # noqa: ANN001, ARG002
            raise AssertionError("越界不应调用 QA")

    events = await _collect(run_chat_stream("今天天气", session_id="s1", deps=_deps(route, _ExplodingQA())))
    joined = _decode_tokens(events)
    assert "超出范围" in joined
    assert _event_names(events)[-1] == "done"


async def test_need_clarification_uses_clarify_text():
    route = RouteResult(
        intent=Intent.RECOMMEND.value,
        need_clarification=True,
        clarify_question="请告诉我你的分数。",
        source="rule",
    )

    class _ExplodingQA:
        async def answer(self, question):  # noqa: ANN001, ARG002
            raise AssertionError("缺参不应调用 QA")

    events = await _collect(run_chat_stream("帮我推荐", session_id="s1", deps=_deps(route, _ExplodingQA())))
    assert "请告诉我你的分数" in _decode_tokens(events)


async def test_recommend_intent_uses_recommend_engine(monkeypatch):
    """推荐意图必须走结构化推荐，不能误入政策 RAG。"""
    route = RouteResult(
        intent=Intent.RECOMMEND.value,
        params={"score": 590, "subject_type": "物理", "province": "河南"},
        source="rule",
    )

    class _ExplodingQA:
        async def answer(self, question):  # noqa: ANN001, ARG002
            raise AssertionError("推荐意图不应调用政策 QA")

    async def _fake_recommend(params):  # noqa: ANN001
        assert params["province"] == "河南"
        return "河南数据待补，暂不能生成院校清单。"

    import app.services.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_recommend_answer", _fake_recommend)
    events = await _collect(
        run_chat_stream("590分物理类河南", session_id="s-rec", deps=_deps(route, _ExplodingQA()))
    )
    assert "河南数据待补" in _decode_tokens(events)
    done = next(ev for ev in events if "event: done" in ev)
    assert '"model": "recommend-engine"' in done


def test_clarification_reply_merges_pending_params():
    """用户只补「物理类」时，上一轮暂存的分数与省份必须参与本轮路由。"""
    route = RouteResult(
        intent=Intent.POLICY.value,
        params={"subject_type": "物理"},
        source="rule",
    )
    merged = _merge_pending_route(
        route,
        {"score": 590, "province": "河南"},
    )
    assert merged.intent == Intent.RECOMMEND.value
    assert merged.params == {"score": 590, "province": "河南", "subject_type": "物理"}
    assert not merged.need_clarification


# ============================================================
#  异常 → error 事件
# ============================================================


async def test_stream_exception_becomes_error_event():
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    qa = _FakeQA("", raise_exc=True)
    events = await _collect(run_chat_stream("问题", session_id="s1", deps=_deps(route, qa)))
    assert _event_names(events)[-1] == "error"


# ============================================================
#  流缓冲
# ============================================================


def test_stream_buffer_accumulates():
    buf = StreamBuffer(stream_id="s1")
    buf.append("你")
    buf.append("好")
    assert buf.content == "你好"
    assert buf.token_count == 2
    assert buf.to_dict()["content"] == "你好"


def test_in_memory_stream_store():
    store = InMemoryStreamStore()
    store.create("s1")
    assert store.get("s1") is not None
    store.delete("s1")
    assert store.get("s1") is None


# ============================================================
#  端点（TestClient，离线）
# ============================================================


def test_endpoint_returns_event_stream(monkeypatch):
    """端点返回 text/event-stream + X-Accel-Buffering: no。"""
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app
    from app.services.streamhub import StreamHub

    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    fake_deps = _deps(route, _FakeQA("本科批次填报时间为6月28日。[1]"))
    hub = StreamHub(deps_factory=lambda: fake_deps)
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: hub)

    client = TestClient(app)
    with client.stream("GET", "/api/chat/stream", params={"q": "填报时间", "session_id": "s1"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("x-accel-buffering") == "no"
        body = "".join(resp.iter_text())
    assert "event: meta" in body
    assert "event: done" in body


def test_endpoint_deps_unavailable_returns_error_event(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app
    from app.services.streamhub import StreamHub

    hub = StreamHub(deps_factory=lambda: None)
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: hub)
    client = TestClient(app)
    resp = client.get("/api/chat/stream", params={"q": "问题"})
    assert resp.status_code == 200
    assert "event: error" in resp.text
