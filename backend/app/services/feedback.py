"""T22 · 反馈闭环服务（点赞/点踩/纠错 → DB 优化资产）。

职责
----
1. 校验入参（kind 枚举 / down 必带原因 / correction 必带内容）；
2. **一条输出仅一次反馈**：应用层先查 + DB 唯一约束双保险（重复 → `FeedbackDuplicateError`）；
3. **快照去身份**：按 ``message_id`` 取「提问 + 回复」经 T17 ``sanitize_text`` 脱敏后落库
   （分数/位次/手机号一律掩码），**不携带任何用户身份字段**；
4. 反馈与推荐结果**解耦**：落库即止，不反向修改推荐（红线）。

可注入
------
- ``FeedbackStore``（存储）：SQL 生产 / 内存测试；
- ``MessageSource``（消息对来源）：SQL 生产 / 内存测试。
全部离线可测，不依赖 MySQL。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.exceptions import FeedbackDuplicateError, ValidationError
from app.core.logging import get_logger
from app.schemas.feedback import (
    ALLOWED_DIMENSIONS,
    ALLOWED_KINDS,
    KIND_CORRECTION,
    KIND_DOWN,
    KIND_UP,
    FeedbackView,
)
from app.services.session import sanitize_text

logger = get_logger(__name__)

MAX_SNAPSHOT_CHARS = 4000


@dataclass
class MessagePair:
    """一条 assistant 消息及其对应的上一条 user 提问。"""

    prompt: str = ""
    response: str = ""
    model: str | None = None


class MessageSource(Protocol):
    """消息对来源接口。"""

    def get_pair(self, message_id: int) -> MessagePair | None: ...


class FeedbackStore(Protocol):
    """反馈存储接口。"""

    def exists_for_message(self, message_id: int) -> bool: ...

    def save(self, record: dict[str, Any]) -> int: ...

    def recent(self, limit: int) -> list[dict[str, Any]]: ...


# ============================================================
#  实现
# ============================================================


class InMemoryMessageSource:
    """内存消息对（测试用）。"""

    def __init__(self, pairs: dict[int, MessagePair] | None = None) -> None:
        self._pairs = pairs or {}

    def add_pair(self, message_id: int, pair: MessagePair) -> None:
        self._pairs[message_id] = pair

    def get_pair(self, message_id: int) -> MessagePair | None:
        return self._pairs.get(message_id)


class SqlMessageSource:
    """SQL 消息对（生产）：message_id → (上一条 user 消息, 该 assistant 消息, model)。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    def get_pair(self, message_id: int) -> MessagePair | None:
        from sqlalchemy import select

        from app.db.models import Message

        msg = self._db.get(Message, message_id)
        if msg is None:
            return None
        # 找同会话中该消息之前最近的一条 user 消息作为提问快照
        prompt = ""
        try:
            prev = (
                self._db.execute(
                    select(Message)
                    .where(Message.session_id == msg.session_id, Message.seq < msg.seq, Message.role == "user")
                    .order_by(Message.seq.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            prompt = prev.content if prev else ""
        except Exception as exc:  # noqa: BLE001 - 提问快照缺失不影响反馈落库
            logger.warning("反馈提问快照查询失败（忽略）：%r", exc)
        return MessagePair(prompt=prompt, response=msg.content, model=msg.model)


class InMemoryFeedbackStore:
    """内存反馈存储（测试用）。"""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._by_message: dict[int, dict[str, Any]] = {}

    def exists_for_message(self, message_id: int) -> bool:
        return message_id in self._by_message

    def save(self, record: dict[str, Any]) -> int:
        self._rows.append(record)
        fid = len(self._rows)
        if record.get("message_id") is not None:
            self._by_message[record["message_id"]] = record
        return fid

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows[-limit:][::-1]]


class SqlFeedbackStore:
    """SQL 反馈存储（生产）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    def exists_for_message(self, message_id: int) -> bool:
        from sqlalchemy import select

        from app.db.models import Feedback

        return self._db.execute(select(Feedback.id).where(Feedback.message_id == message_id)).first() is not None

    def save(self, record: dict[str, Any]) -> int:
        from app.db.models import Feedback

        row = Feedback(**record)
        self._db.add(row)
        self._db.commit()
        return int(row.id)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.db.models import Feedback

        rows = (
            self._db.execute(select(Feedback).order_by(Feedback.id.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [
            {
                "id": int(r.id),
                "message_id": int(r.message_id) if r.message_id is not None else None,
                "kind": r.kind,
                "dimension": r.dimension,
                "rating": r.rating,
                "reason": r.reason,
                "content": r.content,
                "prompt_snapshot": r.prompt_snapshot,
                "response_snapshot": r.response_snapshot,
                "model": r.model,
                "created_at": r.created_at,
            }
            for r in rows
        ]


# ============================================================
#  服务
# ============================================================


class FeedbackService:
    """反馈闭环服务。"""

    def __init__(self, store: FeedbackStore, messages: MessageSource | None = None) -> None:
        self.store = store
        self.messages = messages

    def submit(
        self,
        *,
        kind: str,
        dimension: str = "answer",
        message_id: int | None = None,
        session_sid: str = "",
        reason: str = "",
        correction: str = "",
    ) -> FeedbackView:
        """提交一次反馈；重复反馈同一输出 → ``FeedbackDuplicateError``（409）。"""
        # ---- 校验 ----
        if kind not in ALLOWED_KINDS:
            raise ValidationError(f"kind 必须是 {'/'.join(sorted(ALLOWED_KINDS))}，收到 {kind!r}")
        if dimension not in ALLOWED_DIMENSIONS:
            raise ValidationError(f"dimension 必须是 {'/'.join(sorted(ALLOWED_DIMENSIONS))}，收到 {dimension!r}")
        if kind == KIND_DOWN and not reason:
            raise ValidationError("点踩必须选择原因（推荐不准/数据错误/回答跑题/其他）")
        if kind == KIND_CORRECTION and not correction.strip():
            raise ValidationError("纠错反馈必须填写纠错内容")

        # ---- 一条输出仅一次（应用层先查；DB 唯一约束兜底）----
        if message_id is not None and self.store.exists_for_message(message_id):
            logger.info("重复反馈被拦截 message_id=%s kind=%s", message_id, kind)
            raise FeedbackDuplicateError("该回答已反馈过，每条内容仅可反馈一次")

        # ---- 快照（去身份：脱敏 + 不含身份字段）----
        prompt_snap = ""
        response_snap = ""
        model = None
        if message_id is not None and self.messages is not None:
            pair = self.messages.get_pair(message_id)
            if pair is not None:
                prompt_snap = sanitize_text(pair.prompt)[:MAX_SNAPSHOT_CHARS]
                response_snap = sanitize_text(pair.response)[:MAX_SNAPSHOT_CHARS]
                model = pair.model

        record: dict[str, Any] = {
            "message_id": message_id,
            "session_sid": session_sid or None,
            "kind": kind,
            "dimension": dimension,
            "rating": {KIND_UP: 1, KIND_DOWN: -1}.get(kind),
            "reason": reason or None,
            "content": correction.strip() or None,
            "prompt_snapshot": prompt_snap or None,
            "response_snapshot": response_snap or None,
            "model": model,
        }
        try:
            feedback_id = self.store.save(record)
        except Exception as exc:  # noqa: BLE001 - DB 唯一约束兜底（并发双击）
            if _is_unique_violation(exc):
                raise FeedbackDuplicateError("该回答已反馈过，每条内容仅可反馈一次") from exc
            raise

        return FeedbackView(
            feedback_id=feedback_id,
            message_id=message_id,
            kind=kind,
            dimension=dimension,
            rating=record["rating"],
            reason=record["reason"],
            correction=record["content"],
            prompt_snapshot=record["prompt_snapshot"],
            response_snapshot=record["response_snapshot"],
            model=model,
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近反馈（管理后台查看用；快照已脱敏，无身份字段）。"""
        limit = max(1, min(int(limit), 200))
        out: list[dict[str, Any]] = []
        for row in self.store.recent(limit):
            created = row.get("created_at")
            out.append({
                "feedback_id": row.get("id"),
                "message_id": row.get("message_id"),
                "kind": row.get("kind"),
                "dimension": row.get("dimension"),
                "rating": row.get("rating"),
                "reason": row.get("reason"),
                "correction": row.get("content"),
                "prompt_snapshot": row.get("prompt_snapshot"),
                "response_snapshot": row.get("response_snapshot"),
                "model": row.get("model"),
                "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else None,
            })
        return out


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "duplicate" in text or "uq_feedback_message" in text or "1062" in text


# ============================================================
#  单例
# ============================================================

_service: FeedbackService | None = None


def get_feedback_service(db=None) -> FeedbackService:  # noqa: ANN001
    """请求级构造（同 ``get_profile_service``：不再缓存持会话的单例）。

    旧实现自建 ``SessionLocal()`` 并长期持有，连接不归还连接池，
    会被 MySQL ``wait_timeout`` 断开后让反馈写入永久 500。
    """
    if _service is not None:  # 仅测试注入时会命中
        return _service
    if db is None:
        raise ValueError("get_feedback_service 需要请求级 Session（禁止自建进程级单例会话）")
    return FeedbackService(SqlFeedbackStore(db), SqlMessageSource(db))


def set_feedback_service(service: FeedbackService | None) -> None:
    """替换全局服务（测试用）。"""
    global _service
    _service = service
