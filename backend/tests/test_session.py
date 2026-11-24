"""T17 测试 —— 会话与多轮上下文（持久化 + 压缩 + 组装）。

对应任务卡「测试场景（必做）」：
    J3 长会话触发摘要压缩且关键信息保留；

补足：
- 会话幂等创建；消息顺序（seq 递增）；标题取首问；
- **滚动摘要压缩**：旧消息 → 摘要，只保留最近 N 条；**关键意图/要点进摘要不丢**；
- 上下文组装 ``build_messages``：system（含档案片段 + 历史摘要）+ 历史原文；
- **档案片段与压缩互不干扰**（压缩不碰档案）；
- 消息落库前**脱敏**（明文分数/位次 → 区间）；
- 删除会话级联删消息；
- Redis 缓存不可用时**静默降级**（不抛异常）。

设计：**不依赖 MySQL / Redis** —— 用 ``InMemorySessionStore``，``cache=None``，全离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.session import (  # noqa: E402
    COMPRESS_THRESHOLD,
    RECENT_KEEP,
    InMemorySessionStore,
    SessionService,
    compress_messages,
    sanitize_text,
)


def _service() -> SessionService:
    return SessionService(InMemorySessionStore())


# ============================================================
#  基础：创建 / 追加 / 读取
# ============================================================


def test_ensure_session_idempotent():
    svc = _service()
    a = svc.ensure_session("sid-1")
    b = svc.ensure_session("sid-1")
    assert a.id == b.id
    assert a.sid == "sid-1"


def test_session_history_owner_rename_delete():
    svc = _service()
    svc.append_turn(
        "sid-owner",
        "你好",
        "你好，有什么可以帮你？",
        owner_username="student",
    )
    items = svc.list_sessions("student")
    assert [item["sid"] for item in items] == ["sid-owner"]
    assert svc.rename_session("sid-owner", "student", "新的会话名")
    assert svc.get_session("sid-owner").title == "新的会话名"
    assert not svc.rename_session("sid-owner", "other", "越权")
    assert not svc.delete_session("sid-owner", owner_username="other")
    assert svc.delete_session("sid-owner", owner_username="student")
    assert svc.get_session("sid-owner") is None


def test_append_turn_records_two_messages():
    svc = _service()
    view = svc.append_turn("sid-1", "我580分物理类", "为你推荐…", intent="recommend")
    assert len(view.messages) == 2
    assert view.messages[0].role == "user"
    assert view.messages[1].role == "assistant"
    assert view.messages[0].seq == 0
    assert view.messages[1].seq == 1


def test_title_set_from_first_question():
    svc = _service()
    svc.append_turn("sid-1", "提前批什么时候填报", "…", intent="policy")
    view = svc.get_session("sid-1")
    assert view.title == "提前批什么时候填报"


def test_title_not_overwritten_by_second_turn():
    svc = _service()
    svc.append_turn("sid-1", "第一问", "…")
    svc.append_turn("sid-1", "第二问", "…")
    view = svc.get_session("sid-1")
    assert view.title == "第一问"


def test_get_missing_session_returns_none():
    svc = _service()
    assert svc.get_session("nope") is None


# ============================================================
#  J3 · 长会话压缩
# ============================================================


def test_j3_no_compression_when_short():
    msgs = [{"role": "user", "content": f"问题{i}", "seq": i} for i in range(5)]
    summary, recent = compress_messages(msgs, keep_recent=RECENT_KEEP)
    assert summary == ""
    assert len(recent) == 5


def test_j3_compression_keeps_recent_and_summarizes_old():
    msgs: list[dict] = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"第{i}个关于志愿填报的问题", "intent": "policy" if i % 2 else "recommend"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})

    summary, recent = compress_messages(msgs, keep_recent=RECENT_KEEP)
    assert summary, "长会话应生成摘要"
    assert len(recent) == RECENT_KEEP, "应只保留最近 N 条原文"
    # 关键信息保留：摘要含意图标签
    assert "policy" in summary or "recommend" in summary
    assert "此前对话摘要" in summary


def test_j3_compression_preserves_user_topics():
    """摘要应保留用户问过的要点（首句片段）。"""
    msgs = [{"role": "user", "content": f"关于提前批的独特问题{i}", "intent": "policy"} for i in range(20)]
    summary, _ = compress_messages(msgs, keep_recent=5)
    assert "提前批" in summary, "摘要不得丢失关键主题"


def test_j3_compression_no_loss_of_recent():
    """最近消息必须**原样**保留（不压缩、不改写）。"""
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(15)]
    _, recent = compress_messages(msgs, keep_recent=10)
    assert [m["content"] for m in recent] == [f"msg{i}" for i in range(5, 15)]


def test_compress_empty_returns_empty():
    summary, recent = compress_messages([], keep_recent=10)
    assert summary == ""
    assert recent == []


# ============================================================
#  上下文组装
# ============================================================


def test_build_messages_without_profile():
    svc = _service()
    svc.append_turn("sid-1", "提前批什么时候填报", "根据…[1]", intent="policy")
    msgs = svc.build_messages("sid-1", system_prompt="你是升学助手")
    assert msgs[0]["role"] == "system"
    assert "你是升学助手" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"


def test_build_messages_includes_profile_block():
    svc = _service()
    svc.append_turn("sid-1", "问题", "回答")
    msgs = svc.build_messages(
        "sid-1", system_prompt="你是升学助手", profile_block="【考生档案】\n- 报考省份：广东"
    )
    assert "考生档案" in msgs[0]["content"]
    assert "广东" in msgs[0]["content"]


def test_build_messages_long_session_injects_summary():
    """长会话：system 段应含历史摘要，且历史只保留最近 N 条。"""
    svc = _service()
    for i in range(COMPRESS_THRESHOLD + 6):
        svc.append_turn("sid-1", f"关于志愿的第{i}个问题", f"回答{i}", intent="policy")
    msgs = svc.build_messages("sid-1", system_prompt="你是升学助手", profile_block="【考生档案】\n- 省份：广东")
    system = msgs[0]["content"]
    assert "历史摘要" in system, "长会话应注入历史摘要"
    assert "考生档案" in system, "档案片段不得因压缩丢失"
    # 历史消息数 = 最近 RECENT_KEEP 条（user/assistant 交替）
    history = [m for m in msgs[1:] if m["role"] in ("user", "assistant")]
    assert len(history) <= RECENT_KEEP


def test_build_messages_profile_survives_compression():
    """红线：压缩不得丢关键档案字段。"""
    svc = _service()
    for i in range(COMPRESS_THRESHOLD + 4):
        svc.append_turn("sid-1", f"问题{i}", f"答{i}")
    msgs = svc.build_messages(
        "sid-1", system_prompt="sys", profile_block="【考生档案】\n- 首选科目：物理\n- 分数区间：610-619"
    )
    assert "物理" in msgs[0]["content"]
    assert "610-619" in msgs[0]["content"]


# ============================================================
#  脱敏（消息落库前）
# ============================================================


def test_sanitize_score_in_text():
    assert "610-619分" in sanitize_text("我考了612分")


def test_sanitize_rank_in_text():
    out = sanitize_text("我的位次是35412")
    assert "35412" not in out
    assert "35000-35999" in out


def test_sanitize_keeps_normal_text():
    assert sanitize_text("提前批什么时候填报") == "提前批什么时候填报"


def test_append_turn_desensitizes_user_content():
    """落库的用户消息不得含明文分数。"""
    store = InMemorySessionStore()
    svc = SessionService(store)
    svc.append_turn("sid-1", "我考了612分物理类", "好的")
    stored = store.messages["sid-1"][0]["content"]
    assert "612" not in stored
    assert "610-619" in stored


def test_assistant_content_not_modified():
    """助手回复不强制脱敏（可能含引用的公开分数），但用户输入必脱敏。"""
    store = InMemorySessionStore()
    svc = SessionService(store)
    svc.append_turn("sid-1", "问题", "本科线为 539 分")
    assert "539" in store.messages["sid-1"][1]["content"]


# ============================================================
#  删除 & 缓存降级
# ============================================================


def test_delete_session_cascades():
    svc = _service()
    svc.append_turn("sid-1", "问题", "回答")
    assert svc.delete_session("sid-1") is True
    assert svc.get_session("sid-1") is None


def test_delete_missing_session_returns_false():
    svc = _service()
    assert svc.delete_session("nope") is False


def test_cache_failure_degrades_silently():
    """Redis 不可用（delete 抛异常）时不得影响业务。"""

    class _BrokenCache:
        def delete(self, key: str) -> None:  # noqa: ARG002
            raise RuntimeError("redis down")

    svc = SessionService(InMemorySessionStore(), cache=_BrokenCache())
    view = svc.append_turn("sid-1", "问题", "回答")
    assert len(view.messages) == 2


def test_session_view_to_dict():
    svc = _service()
    view = svc.append_turn("sid-1", "问题", "回答", intent="policy")
    d = view.to_dict()
    for key in ("id", "sid", "title", "summary", "message_count", "messages"):
        assert key in d


# ============================================================
#  真机 smoke（可选：MySQL 可用时）
# ============================================================


@pytest.mark.smoke
def test_real_session_smoke():
    try:
        from app.db.session import SessionLocal
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用：{exc}")
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    try:
        from app.services.session import SqlSessionStore

        svc = SessionService(SqlSessionStore(db))
        svc.append_turn("smoke-t17-sess", "我612分物理类", "为您推荐…", intent="recommend")
        view = svc.get_session("smoke-t17-sess")
        assert view is not None
        assert len(view.messages) == 2
        assert svc.delete_session("smoke-t17-sess") is True
    finally:
        db.close()
