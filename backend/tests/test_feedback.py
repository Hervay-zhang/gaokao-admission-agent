"""T22 测试 —— 反馈闭环（点赞/点踩/纠错 → DB 资产）。

对应任务卡「测试场景（必做）」：
    J1 提交点赞/点踩/纠错落库且含 Prompt/Response 快照；
    J2 同一 message 二次反馈被拦截；
    J3 流式未结束不展示反馈控件（前端断言 —— 后端侧验证 message_id 只能来自 done）；
    J4 快照中无用户身份字段。

设计：**不依赖 MySQL** —— InMemoryFeedbackStore / InMemoryMessageSource 注入，离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import FeedbackDuplicateError, ValidationError  # noqa: E402
from app.schemas.feedback import FeedbackAck  # noqa: E402
from app.services.feedback import (  # noqa: E402
    FeedbackService,
    InMemoryFeedbackStore,
    InMemoryMessageSource,
    MessagePair,
)


@pytest.fixture
def store() -> InMemoryFeedbackStore:
    return InMemoryFeedbackStore()


@pytest.fixture
def messages() -> InMemoryMessageSource:
    return InMemoryMessageSource()


@pytest.fixture
def service(store, messages) -> FeedbackService:
    messages.add_pair(101, MessagePair(prompt="我考了632分能上什么大学", response="基于库内数据……", model="deepseek-v4-flash"))
    return FeedbackService(store, messages)


# ==================== J1 落库 + 快照 ====================


def test_j1_up_saved_with_snapshot(service, store):
    view = service.submit(kind="up", message_id=101, dimension="answer")
    assert view.feedback_id is not None
    assert view.kind == "up"
    assert view.rating == 1
    assert view.prompt_snapshot, "点赞也要带提问快照"
    assert view.response_snapshot, "点赞也要带回复快照"
    assert view.model == "deepseek-v4-flash"
    assert len(store.all()) == 1


def test_j1_down_requires_reason_and_saved(service, store):
    view = service.submit(kind="down", message_id=101, reason="数据错误")
    assert view.rating == -1
    assert view.reason == "数据错误"
    row = store.all()[0]
    assert row["reason"] == "数据错误"


def test_j1_correction_saved(service, store):
    view = service.submit(kind="correction", message_id=101, correction="该院校 2026 年已更名")
    assert view.correction
    assert view.rating is None  # 纠错不打分


def test_j1_snapshot_sanitized(service, store):
    """快照经 T17 sanitize_text 脱敏：明文分数不得出现在快照里。"""
    service.submit(kind="up", message_id=101)
    row = store.all()[0]
    assert "632分" not in (row["prompt_snapshot"] or ""), "明文分数不得进入快照"
    assert "632" not in (row["prompt_snapshot"] or ""), "数字本身也应被掩码"


# ==================== J2 二次反馈拦截 ====================


def test_j2_duplicate_rejected(service):
    service.submit(kind="up", message_id=101)
    with pytest.raises(FeedbackDuplicateError) as ei:
        service.submit(kind="down", message_id=101, reason="其他")
    assert ei.value.http_status == 409
    assert ei.value.code == "FEEDBACK_DUPLICATE"


def test_j2_duplicate_via_store_unique_violation(service):
    """应用层查询被绕过时（并发双击），DB 唯一约束兜底仍拦截（save 抛 duplicate → 409）。"""

    class _RaceStore(InMemoryFeedbackStore):
        def exists_for_message(self, message_id: int) -> bool:
            return False  # 模拟并发：两次查询都未见到对方

        def save(self, record):
            raise RuntimeError("(1062, \"Duplicate entry ... for key 'uq_feedback_message'\")")

    svc = FeedbackService(_RaceStore(), None)
    with pytest.raises(FeedbackDuplicateError):
        svc.submit(kind="up", message_id=101)


def test_j2_different_messages_both_ok(service, store):
    service.messages.add_pair(102, MessagePair(prompt="问2", response="答2"))
    service.submit(kind="up", message_id=101)
    service.submit(kind="up", message_id=102)
    assert len(store.all()) == 2


# ==================== J3 时序（message_id 来自 done）====================


def test_j3_feedback_without_message_id_allowed(service, store):
    """无会话（message_id 空）也能反馈，但**不产生快照**、不计唯一约束。"""
    view = service.submit(kind="up", dimension="recommend")
    assert view.message_id is None
    assert view.prompt_snapshot is None
    assert len(store.all()) == 1
    # 无 message_id 的反馈不占用唯一约束，可多次
    service.submit(kind="up", dimension="recommend")
    assert len(store.all()) == 2


def test_j3_message_id_only_from_done(service):
    """message_id 由前端取自 done 事件；此处验证服务对未知 message_id 的容错（快照为空不报错）。"""
    view = service.submit(kind="up", message_id=999999)
    assert view.prompt_snapshot is None, "未知消息不伪造快照"


# ==================== J4 去身份 ====================


def test_j4_no_identity_fields_in_record(service, store):
    """落库字段白名单：不含任何用户身份字段（姓名/手机号/明文分数）。"""
    service.submit(kind="correction", message_id=101, correction="更正内容", session_sid="anon-abc")
    row = store.all()[0]
    banned = {"user_name", "phone", "mobile", "id_card", "real_name", "score", "score_plain"}
    assert not (set(row.keys()) & banned), f"落库字段不得包含身份字段，实际含 {set(row.keys()) & banned}"
    # session_sid 是匿名 token，允许存在
    assert row["session_sid"] == "anon-abc"


def test_j4_sanitizer_masks_phone_like(service, store):
    service.messages.add_pair(103, MessagePair(prompt="我的手机号13800001234帮我看看", response="……"))
    service.submit(kind="down", message_id=103, reason="回答跑题")
    row = store.all()[0]
    assert "13800001234" not in (row["prompt_snapshot"] or "")


# ==================== 入参校验 ====================


def test_invalid_kind_rejected(service):
    with pytest.raises(ValidationError):
        service.submit(kind="spam")


def test_invalid_dimension_rejected(service):
    with pytest.raises(ValidationError):
        service.submit(kind="up", dimension="hacker")


def test_down_without_reason_rejected(service):
    with pytest.raises(ValidationError):
        service.submit(kind="down", message_id=101)


def test_correction_without_content_rejected(service):
    with pytest.raises(ValidationError):
        service.submit(kind="correction", message_id=101)


# ==================== 端点级 ====================


def test_endpoint_submit_and_duplicate(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import feedback as fb_api
    from app.main import app

    store = InMemoryFeedbackStore()
    messages = InMemoryMessageSource()
    messages.add_pair(55, MessagePair(prompt="问", response="答", model="m"))
    monkeypatch.setattr(
        fb_api, "get_feedback_service", lambda *args, **kwargs: FeedbackService(store, messages)
    )

    client = TestClient(app)
    # 首次提交 → 200
    r1 = client.post(
        "/api/feedback",
        json={"message_id": 55, "kind": "down", "reason": "数据错误", "session_sid": "anon-1"},
    )
    assert r1.status_code == 200
    ack = FeedbackAck(**r1.json())
    assert ack.ok and ack.feedback_id is not None

    # 二次 → 409
    r2 = client.post("/api/feedback", json={"message_id": 55, "kind": "up"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "FEEDBACK_DUPLICATE"

    # 非法 kind → 422/400（ValidationError）
    r3 = client.post("/api/feedback", json={"kind": "bad"})
    assert r3.status_code == 422
