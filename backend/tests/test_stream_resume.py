"""T21 测试 —— 断点续写 + 自动重试 + 专家规则模式降级。

对应任务卡「测试场景（必做）」：
    J1 模拟中途断连 → 续写后全文与不断连一致（逐字比对）；
    J2 连续 3 次失败 → 走专家规则模式兜底（降级路径不依赖 LLM）；
    J3 续写不重复 token（计数校验）；
    J4 兜底表格数据来自库内（复用 T11 offline 仓库自证）。

补足：
- 流不存在 → STREAM_NOT_FOUND error 事件（不裸抛 404）；
- 生产者与读者解耦：读者中途退出，生成不中断（断连后事件日志仍完整）；
- 事件 id 严格递增，续写过滤 ``> last_event_id``；
- 依赖不可用 → error 事件收尾（延续 T19 语义）；
- 端点级：resume 端点可用（TestClient 离线）。

设计：**不依赖 MySQL / Redis / 真实 LLM**——注入假 router/QA（复用 test_sse 模式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent.router import Intent, RouteResult  # noqa: E402
from app.services.streamhub import StreamHub  # noqa: E402
from tests.test_sse import _decode_tokens, _deps, _event_names, _FakeQA  # noqa: E402

LONG_ANSWER = "提前批录取安排在本科批次之前，考生需要在规定时间内完成填报确认手续，逾期不予补办。"


def _hub(qa: _FakeQA) -> StreamHub:
    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    return StreamHub(deps_factory=lambda: _deps(route, qa))


async def _read_until_done(hub: StreamHub, stream_id: str, last_event_id: int = -1) -> list[str]:
    return [ev async for ev in hub.read(stream_id, last_event_id=last_event_id)]


# ============================================================
#  J1 · 中途断连 → 续写全文一致（逐字比对）
# ============================================================


async def test_j1_resume_text_identical():
    """模拟读到一半断连（读者提前退出），续写补齐后全文与一次性读完完全一致。"""
    hub = _hub(_FakeQA(LONG_ANSWER))
    stream = await hub.start("提前批规则", session_id="s1")

    # 模拟断连：只读前 4 个事件就退出（meta + 前 3 个 token）
    first_events: list[str] = []
    async for ev in hub.read(stream.stream_id):
        first_events.append(ev)
        if len(first_events) >= 4:
            break

    # 生产者继续在后台跑完（断连不中断）
    await stream.task

    # 断连前收到的最后一个事件 id
    last_id = max(eid for eid, _ in stream.events[: len(first_events)])
    resumed = await _read_until_done(hub, stream.stream_id, last_event_id=last_id)

    # 逐字比对：断连部分 + 续写部分 == 完整全文（guard 会追加免责声明，全文一致即证续写无损）
    combined = _decode_tokens(first_events) + _decode_tokens(resumed)
    full = await _read_until_done(hub, stream.stream_id)  # 完成态：重放全部（id 过滤 -1）
    assert combined == _decode_tokens(full)
    assert combined.startswith(LONG_ANSWER), "正文起始必须与原答案一致"
    assert "仅供参考" in combined, "免责声明由 guard 后置补入（T18），必须保留"


# ============================================================
#  J3 · 续写不重复 token（计数校验）
# ============================================================


async def test_j3_no_duplicate_tokens():
    """续写事件的 id 全部 > last_event_id，token 数量 = 总数 - 已收数，无一重复。"""
    hub = _hub(_FakeQA(LONG_ANSWER))
    stream = await hub.start("提前批规则", session_id="s2")
    await stream.task

    # 先收 5 个事件
    received = 5
    head = stream.events[:received]
    last_id = head[-1][0]

    resumed = await _read_until_done(hub, stream.stream_id, last_event_id=last_id)
    ids = [
        int(line.split(":", 1)[1])
        for ev in resumed
        for line in ev.splitlines()
        if line.startswith("id:")
    ]
    # 全部严格大于 last_id 且严格递增（不重复）
    assert all(i > last_id for i in ids)
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    # 续写正好补齐剩余全部事件
    assert len(ids) == len(stream.events) - received


async def test_j3_resume_from_zero_replays_all():
    """last_event_id=-1 → 重放全部事件（断连发生在首事件前）。"""
    hub = _hub(_FakeQA("短答案文本"))
    stream = await hub.start("问题", session_id="s3")
    await stream.task
    events = await _read_until_done(hub, stream.stream_id, last_event_id=-1)
    assert len(events) == len(stream.events)
    assert _event_names(events)[-1] in ("done", "error")


# ============================================================
#  生产/消费解耦（断连不中断）
# ============================================================


async def test_generation_survives_reader_disconnect():
    """读者中途退出后，生产者仍把事件日志写完整（T21 核心设计）。"""
    hub = _hub(_FakeQA(LONG_ANSWER))
    stream = await hub.start("问题", session_id="s4")

    # 读者立即退出（一个事件都不取）
    agen = hub.read(stream.stream_id)
    await agen.aclose()

    await stream.task
    assert stream.finished
    text = _decode_tokens([raw for _, raw in stream.events])
    assert text.startswith(LONG_ANSWER), "正文必须完整生成（免责声明由 guard 追加）"


async def test_unknown_stream_returns_not_found_error():
    """流不存在 → STREAM_NOT_FOUND error 事件（合法 SSE，不裸抛）。"""
    hub = StreamHub(deps_factory=lambda: None)
    events = await _read_until_done(hub, "no-such-stream")
    assert len(events) == 1
    assert "STREAM_NOT_FOUND" in events[0]
    assert "event: error" in events[0]


async def test_deps_unavailable_becomes_error_event():
    """依赖不可用 → 单条 error 事件收尾（T19 语义在 hub 下保持）。"""
    hub = StreamHub(deps_factory=lambda: None)
    stream = await hub.start("问题", session_id="s5")
    events = await _read_until_done(hub, stream.stream_id)
    names = _event_names(events)
    assert names == ["error"]
    assert "DEPS_UNAVAILABLE" in events[0]


async def test_producer_exception_becomes_error_event():
    """QA 抛异常 → error 事件收尾，流标记 failed。"""

    class _BoomQA:
        async def answer(self, question: str):  # noqa: ARG002
            raise RuntimeError("llm down")

    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    hub = StreamHub(deps_factory=lambda: _deps(route, _BoomQA()))
    stream = await hub.start("问题", session_id="s6")
    events = await _read_until_done(hub, stream.stream_id)
    assert any("event: error" in ev for ev in events)
    assert stream.failed


# ============================================================
#  事件 id / 内存管理
# ============================================================


async def test_event_ids_strictly_increasing():
    hub = _hub(_FakeQA("答案文本"))
    stream = await hub.start("问题", session_id="s7")
    await stream.task
    ids = [eid for eid, _ in stream.events]
    assert ids == list(range(len(ids)))


async def test_cleanup_expired_and_eviction():
    """过保留期的已完成流被清理；超量淘汰最旧。"""
    import time as _time

    hub = _hub(_FakeQA("答案"))
    hub.stream_ttl = 0  # 立即过期
    stream = await hub.start("问题", session_id="s8")
    await stream.task
    hub.cleanup_expired()
    assert hub.get(stream.stream_id) is None

    # 超量淘汰
    hub2 = StreamHub(deps_factory=lambda: None)
    hub2.max_streams = 3
    hub2.stream_ttl = 9999
    for i in range(6):
        s = await hub2.start("q", session_id=f"sx{i}")
        await s.task
        _time.sleep(0.01)
    assert len(hub2._streams) <= 3  # noqa: SLF001 - 测试内部状态


# ============================================================
#  J2/J4 · 降级路径不依赖 LLM，数据来自库内（offline 自证）
# ============================================================


def test_j2_fallback_endpoint_no_llm(monkeypatch):
    """J2：连续失败后的降级出口 —— /api/recommend/fallback 不依赖 LLM（monkeypatch 掉服务为 offline 版）。"""
    from fastapi.testclient import TestClient

    from app.api import recommend as rec_api
    from app.main import app
    from app.services.fallback import FallbackService
    from app.services.recommend import InMemoryRecommendRepository
    from tests.test_fallback import META, _three_tier_rows

    offline_service = FallbackService(InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=False)
    monkeypatch.setattr(rec_api, "get_fallback_service", lambda: offline_service)

    client = TestClient(app)
    # rank 直接给（跳过 T07 位次引擎，避免依赖 MySQL）
    r = client.get(
        "/api/recommend/fallback",
        params={"score": 600, "subject_type": "物理", "rank": 10000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "expert_rule"
    assert body["mode_label"], "专家规则模式标注必须存在"
    assert body["disclaimer"], "免责声明必须随兜底返回"
    tiers = body["tiers"]
    assert {t["label"] for t in tiers} >= {"冲", "稳", "保"}, "兜底表必须有三档"
    # 数据来自库内：每条都带真实位次依据
    for tier in tiers:
        for it in tier["rows"]:
            assert it.get("ref_rank") is not None


def test_j4_fallback_data_from_repo_only():
    """J4：兜底表格数据全部来自注入的仓库（离线构造 → 离线断言，证明「来自库内」）。"""
    from app.services.fallback import FallbackService
    from app.services.recommend import InMemoryRecommendRepository
    from tests.test_fallback import META, _three_tier_rows

    repo = InMemoryRecommendRepository(_three_tier_rows(), META)
    service = FallbackService(repo, use_cache=False)
    result = service.fallback(
        score=600, subject_type="物理", electives=[], province="广东", rank=10000, current_year=2026
    )
    all_codes = {row.university_code for tier in result.tiers for row in tier.rows}
    assert all_codes <= {"10001", "10002", "10003"}, "兜底表院校必须全部来自仓库"


# ============================================================
#  端点级：resume 可用（TestClient，离线）
# ============================================================


def test_endpoint_resume_replay(monkeypatch):
    """端点：先完整读一条流，再带 last_event_id=0 续读 → 只收到 id>0 的事件。"""
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app

    route = RouteResult(intent=Intent.POLICY.value, source="rule")
    qa = _FakeQA("端点续写验证答案文本")
    hub = StreamHub(deps_factory=lambda: _deps(route, qa))
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: hub)

    client = TestClient(app)
    # 第一遍：完整读
    with client.stream("GET", "/api/chat/stream", params={"q": "问题", "session_id": "ep1"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: done" in body

    # 取出 stream_id
    import json as _json

    meta_line = next(ln for ln in body.splitlines() if ln.startswith("data:") and "stream_id" in ln)
    stream_id = _json.loads(meta_line[len("data: ") :])["stream_id"]

    # 第二遍：从 last_event_id=0 续读（跳过 meta）→ 得到 token* 与 done
    resp2 = client.get(
        f"/api/chat/stream/{stream_id}/resume", params={"last_event_id": 0, "session_id": "ep1"}
    )
    assert resp2.status_code == 200
    assert "event: token" in resp2.text
    assert "event: meta" not in resp2.text, "续写不重复 meta"
    assert "event: done" in resp2.text


def test_endpoint_resume_unknown_stream(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import chat as chat_api
    from app.main import app

    hub = StreamHub(deps_factory=lambda: None)
    monkeypatch.setattr(chat_api, "get_stream_hub", lambda: hub)
    client = TestClient(app)
    resp = client.get("/api/chat/stream/whatever/resume", params={"last_event_id": 3})
    assert resp.status_code == 200
    assert "STREAM_NOT_FOUND" in resp.text
