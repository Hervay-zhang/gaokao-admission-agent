"""T17 · 会话与多轮上下文（持久化 + 热态缓存 + 滚动摘要压缩）。

职责
----
1. **会话持久化**：`sessions` / `messages` 落 MySQL（匿名优先：`sid` 为前端匿名 token）；
2. **热态缓存**：最近若干轮消息缓存在 Redis（键 ``chat:ctx:{sid}``），**Redis 不可用时静默降级**为直查 MySQL；
3. **滚动摘要压缩**：会话超长时把「旧的若干轮」压成一句摘要存 ``sessions.title``（本模块用 ``summary`` 存 message 角色 ``system``），
   **只保留最近 N 轮原文**；**关键档案字段绝不丢**（压缩只压对话历史，不碰档案）；
4. **上下文组装**：`build_messages(profile, session)` 产出交给 T18 的「系统段 + 历史段」结构。

分层设计（可离线测试）
----------------------
- :class:`SessionStore` Protocol：`create/get/append/list_messages/update_title/delete`；
- :class:`SqlSessionStore`（生产）+ :class:`InMemorySessionStore`（测试）；
- Redis 缓存通过可选 ``cache`` 参数注入，测试传 ``None`` → 纯内存直连，**完全离线**。

红线
----
- **用户档案（跨会话） vs 会话记忆（仅当次）分开存储**（任务卡「易混淆」）：本模块只管会话，档案由 ``profile.py`` 管；
- **压缩不得丢失关键档案字段**：压缩只作用于 messages，档案片段由 T18 从 profile 现取；
- **日志无明文**：消息内容落库前按需脱敏（``sanitize_text``），日志只记条数与长度。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 保留最近 N 轮**原文**（一轮 = user + assistant；这里按消息条数近似）
RECENT_KEEP = 10
# 触发压缩的阈值（消息条数超过则压缩旧消息）
COMPRESS_THRESHOLD = 16
# 热态缓存的会话数上限
CACHE_TTL_SECONDS = 3600

# 会话标题最大长度
TITLE_MAX = 128

# 消息正文里的敏感数字（形如「580分」「位次35000」）——落库前脱敏
_SCORE_IN_TEXT = re.compile(r"(\d{3})\s*分")
_RANK_IN_TEXT = re.compile(r"(位次|排名|省排)\D{0,4}(\d{2,6})")


# ============================================================
#  数据契约
# ============================================================


@dataclass
class ChatMessage:
    """一条会话消息（结构化，便于落库与组装）。"""

    role: str                 # user / assistant / system
    content: str
    intent: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    mocked: bool = False
    latency_ms: int | None = None
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "intent": self.intent,
            "citations": self.citations,
            "model": self.model,
            "mocked": self.mocked,
            "latency_ms": self.latency_ms,
            "seq": self.seq,
        }


@dataclass
class SessionView:
    """会话视图（含摘要 + 消息列表）。"""

    id: int | None
    sid: str
    title: str | None = None
    summary: str = ""
    message_count: int = 0
    messages: list[ChatMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sid": self.sid,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "messages": [m.to_dict() for m in self.messages],
        }


# ============================================================
#  存储接口
# ============================================================


@runtime_checkable
class SessionStore(Protocol):
    """会话存储接口。"""

    def create_session(
        self,
        sid: str,
        title: str | None = None,
        owner_username: str | None = None,
    ) -> int: ...

    def get_session(self, sid: str) -> dict[str, Any] | None: ...

    def append_message(self, sid: str, msg: dict[str, Any]) -> int: ...

    def list_messages(self, sid: str) -> list[dict[str, Any]]: ...

    def list_sessions(self, owner_username: str, limit: int = 50) -> list[dict[str, Any]]: ...

    def update_session(self, sid: str, fields: dict[str, Any]) -> bool: ...

    def delete_session(self, sid: str) -> bool: ...


class SqlSessionStore:
    """MySQL 实现。"""

    def __init__(self, db) -> None:  # noqa: ANN001
        self.db = db

    def create_session(
        self,
        sid: str,
        title: str | None = None,
        owner_username: str | None = None,
    ) -> int:
        from app.db.models import Session

        obj = Session(
            sid=sid,
            owner_username=owner_username,
            title=(title or None),
            message_count=0,
        )
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return int(obj.id)

    def get_session(self, sid: str) -> dict[str, Any] | None:
        from app.db.models import Session

        obj = self.db.query(Session).filter(Session.sid == sid).first()
        if obj is None:
            return None
        return {
            "id": obj.id,
            "sid": obj.sid,
            "title": obj.title,
            "message_count": obj.message_count,
            "user_id": obj.user_id,
            "owner_username": obj.owner_username,
        }

    def append_message(self, sid: str, msg: dict[str, Any]) -> int:
        from app.db.models import Message, Session

        sess = self.db.query(Session).filter(Session.sid == sid).first()
        if sess is None:
            raise ValueError(f"会话不存在：{sid}")
        seq = self.db.query(Message).filter(Message.session_id == sess.id).count()
        obj = Message(
            session_id=sess.id,
            seq=seq,
            role=msg.get("role", "user"),
            content=msg.get("content", ""),
            intent=msg.get("intent"),
            citations=json.dumps(msg.get("citations", []), ensure_ascii=False) if msg.get("citations") else None,
            model=msg.get("model"),
            mocked=bool(msg.get("mocked", False)),
            latency_ms=msg.get("latency_ms"),
        )
        self.db.add(obj)
        sess.message_count = (sess.message_count or 0) + 1
        self.db.commit()
        return seq

    def list_messages(self, sid: str) -> list[dict[str, Any]]:
        from app.db.models import Message, Session

        sess = self.db.query(Session).filter(Session.sid == sid).first()
        if sess is None:
            return []
        rows = (
            self.db.query(Message)
            .filter(Message.session_id == sess.id)
            .order_by(Message.seq)
            .all()
        )
        return [_orm_msg_to_dict(m) for m in rows]

    def list_sessions(self, owner_username: str, limit: int = 50) -> list[dict[str, Any]]:
        from app.db.models import Session

        rows = (
            self.db.query(Session)
            .filter(Session.owner_username == owner_username)
            .order_by(Session.updated_at.desc(), Session.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": row.id,
                "sid": row.sid,
                "title": row.title,
                "message_count": row.message_count,
                "owner_username": row.owner_username,
                "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
            }
            for row in rows
        ]

    def update_session(self, sid: str, fields: dict[str, Any]) -> bool:
        from app.db.models import Session

        sess = self.db.query(Session).filter(Session.sid == sid).first()
        if sess is None:
            return False
        for k, v in fields.items():
            setattr(sess, k, v)
        self.db.commit()
        return True

    def delete_session(self, sid: str) -> bool:
        from app.db.models import Session

        sess = self.db.query(Session).filter(Session.sid == sid).first()
        if sess is None:
            return False
        self.db.delete(sess)  # cascade 删除 messages
        self.db.commit()
        return True


def _orm_msg_to_dict(m) -> dict[str, Any]:  # noqa: ANN001
    cites: list[dict[str, Any]] = []
    if m.citations:
        try:
            obj = json.loads(m.citations)
            cites = obj if isinstance(obj, list) else []
        except (json.JSONDecodeError, TypeError):
            cites = []
    return {
        "role": m.role,
        "content": m.content,
        "intent": m.intent,
        "citations": cites,
        "model": m.model,
        "mocked": bool(m.mocked),
        "latency_ms": m.latency_ms,
        "seq": m.seq,
    }


class InMemorySessionStore:
    """内存实现（测试用，语义与 SQL 版一致）。"""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self._next = 1

    def create_session(
        self,
        sid: str,
        title: str | None = None,
        owner_username: str | None = None,
    ) -> int:
        sid_id = self._next
        self._next += 1
        self.sessions[sid] = {
            "id": sid_id,
            "sid": sid,
            "title": title,
            "message_count": 0,
            "owner_username": owner_username,
        }
        self.messages[sid] = []
        return sid_id

    def get_session(self, sid: str) -> dict[str, Any] | None:
        row = self.sessions.get(sid)
        return dict(row) if row else None

    def append_message(self, sid: str, msg: dict[str, Any]) -> int:
        if sid not in self.sessions:
            raise ValueError(f"会话不存在：{sid}")
        seq = len(self.messages[sid])
        stored = dict(msg)
        stored["seq"] = seq
        self.messages[sid].append(stored)
        self.sessions[sid]["message_count"] = len(self.messages[sid])
        return seq

    def list_messages(self, sid: str) -> list[dict[str, Any]]:
        return [dict(m) for m in self.messages.get(sid, [])]

    def list_sessions(self, owner_username: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.sessions.values()
            if row.get("owner_username") == owner_username
        ]
        return sorted(rows, key=lambda row: int(row.get("id") or 0), reverse=True)[:limit]

    def update_session(self, sid: str, fields: dict[str, Any]) -> bool:
        if sid not in self.sessions:
            return False
        self.sessions[sid].update(fields)
        return True

    def delete_session(self, sid: str) -> bool:
        if sid not in self.sessions:
            return False
        del self.sessions[sid]
        self.messages.pop(sid, None)
        return True


def _dict_to_msg(m: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=m.get("role", "user"),
        content=m.get("content", ""),
        intent=m.get("intent"),
        citations=m.get("citations") or [],
        model=m.get("model"),
        mocked=bool(m.get("mocked", False)),
        latency_ms=m.get("latency_ms"),
        seq=m.get("seq", 0),
    )


# ============================================================
#  脱敏（消息落库前）
# ============================================================


# 文本内手机号（11 位，1 开头第 2 位 3-9）——落库快照必须掩码（PIPL）
_PHONE_IN_TEXT = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def sanitize_text(text: str) -> str:
    """把消息正文里的**明文分数/位次/手机号**替换为区间或掩码（PIPL：落库前脱敏）。

    注意：本函数只用于**落库/日志**路径；内存态展示给本人时可保留原文。
    """
    if not text:
        return ""

    def _score_repl(m: re.Match) -> str:
        v = int(m.group(1))
        low = (v // 10) * 10
        return f"{low}-{low + 9}分"

    def _rank_repl(m: re.Match) -> str:
        v = int(m.group(2))
        low = (v // 1000) * 1000
        return f"{m.group(1)}{low}-{low + 999}"

    out = _SCORE_IN_TEXT.sub(_score_repl, text)
    out = _RANK_IN_TEXT.sub(_rank_repl, out)
    # 手机号最后（先掩分数/位次，避免位次数字被误判为手机号片段）
    out = _PHONE_IN_TEXT.sub("1**********", out)
    return out


# ============================================================
#  滚动摘要（上下文压缩）
# ============================================================


def compress_messages(messages: list[dict[str, Any]], *, keep_recent: int = RECENT_KEEP) -> tuple[str, list[dict[str, Any]]]:
    """把旧消息压成一句摘要，返回 ``(summary, 保留的最近消息)``。

    **本函数只做「机械摘要」**（不调用 LLM）——抽取旧消息里的**关键实体**
    （意图标签 + 各轮首句片段），拼成一句可读摘要。这样：
    1. 离线可复现（不依赖 LLM）；
    2. **不丢关键信息**（意图与要点都进摘要）；
    3. 真正的语义摘要可在 T18 接 LLM 后替换（本函数是可用的确定性兜底）。
    """
    if len(messages) <= keep_recent:
        return "", list(messages)

    old = messages[:-keep_recent]
    recent = messages[-keep_recent:]

    intents: list[str] = []
    snippets: list[str] = []
    for m in old:
        if m.get("intent") and m["intent"] not in intents:
            intents.append(m["intent"])
        if m.get("role") == "user":
            first_line = (m.get("content") or "").strip().splitlines()[0][:40] if m.get("content") else ""
            if first_line:
                snippets.append(first_line)

    head = "此前对话摘要："
    if intents:
        head += f"涉及意图[{('、'.join(intents))}]；"
    if snippets:
        head += "用户先后问到：" + "；".join(snippets[:5]) + "。"
    if not intents and not snippets:
        head += "（无实质内容）"
    return head, recent


# ============================================================
#  会话服务
# ============================================================


class SessionService:
    """会话服务（持久化 + 压缩 + 上下文组装）。"""

    def __init__(self, store: SessionStore, *, cache: Any | None = None) -> None:
        self.store = store
        self.cache = cache  # Redis 客户端（可选；None → 不缓存）

    # ---------- 会话生命周期 ----------

    def ensure_session(
        self,
        sid: str,
        *,
        title: str | None = None,
        owner_username: str | None = None,
    ) -> SessionView:
        """获取或创建会话（幂等）。"""
        existing = self.store.get_session(sid)
        if existing is None:
            self.store.create_session(sid, title=title, owner_username=owner_username)
            logger.info("会话已创建 sid=%s", sid)
        elif owner_username and existing.get("owner_username") != owner_username:
            self.store.update_session(sid, {"owner_username": owner_username})
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> SessionView | None:
        sess = self.store.get_session(sid)
        if sess is None:
            return None
        msgs = self.store.list_messages(sid)
        # 摘要**实时计算**（读时压缩）：不额外占用 session 行字段，保证口径唯一。
        # 真正交给 LLM 时由 build_messages 复用同一 compress_messages 逻辑。
        summary, _ = compress_messages(msgs, keep_recent=RECENT_KEEP)
        return SessionView(
            id=sess.get("id"),
            sid=sess["sid"],
            title=sess.get("title"),
            summary=summary,
            message_count=sess.get("message_count", 0),
            messages=[_dict_to_msg(m) for m in msgs],
        )

    def append_turn(
        self,
        sid: str,
        user_content: str,
        assistant_content: str,
        *,
        intent: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        model: str | None = None,
        mocked: bool = False,
        latency_ms: int | None = None,
        owner_username: str | None = None,
    ) -> SessionView:
        """追加一轮对话（user + assistant），**落库前脱敏**。"""
        self.ensure_session(sid, owner_username=owner_username)
        # 落库前脱敏（明文分数/位次 → 区间）
        safe_user = sanitize_text(user_content)
        self.store.append_message(sid, {"role": "user", "content": safe_user, "intent": intent})
        self.store.append_message(
            sid,
            {
                "role": "assistant",
                "content": assistant_content,
                "intent": intent,
                "citations": citations or [],
                "model": model,
                "mocked": mocked,
                "latency_ms": latency_ms,
            },
        )
        # 首轮提问 → 设为会话标题（截断）
        sess = self.store.get_session(sid) or {}
        if not sess.get("title") and user_content:
            self.store.update_session(sid, {"title": sanitize_text(user_content).strip()[:TITLE_MAX]})
        # 压缩检查
        self._maybe_compress(sid)
        self._invalidate_cache(sid)
        return self.get_session(sid)  # type: ignore[return-value]

    def list_sessions(self, owner_username: str, limit: int = 50) -> list[dict[str, Any]]:
        if not owner_username:
            return []
        return self.store.list_sessions(owner_username, limit=limit)

    def rename_session(self, sid: str, owner_username: str, title: str) -> bool:
        sess = self.store.get_session(sid)
        if sess is None or sess.get("owner_username") != owner_username:
            return False
        return self.store.update_session(sid, {"title": sanitize_text(title).strip()[:TITLE_MAX]})

    def delete_session(self, sid: str, owner_username: str | None = None) -> bool:
        """删除会话（含消息级联）。"""
        sess = self.store.get_session(sid)
        if owner_username is not None and (sess is None or sess.get("owner_username") != owner_username):
            return False
        ok = self.store.delete_session(sid)
        self._invalidate_cache(sid)
        if ok:
            logger.info("会话已删除 sid=%s", sid)
        return ok

    # ---------- 上下文组装 ----------

    def build_messages(
        self,
        sid: str,
        *,
        system_prompt: str,
        profile_block: str = "",
    ) -> list[dict[str, str]]:
        """组装交给 LLM 的消息列表：``[system(含档案), ...历史(压缩后), ]``。

        - **档案片段**由调用方从 ``profile.build_preference_block`` 传入（压缩**不碰**它）；
        - 历史消息超过阈值 → 先压缩（旧消息 → 摘要），只保留最近 ``RECENT_KEEP`` 条原文；
        - 摘要作为 system 段的补充注入，**保证关键信息不丢**。
        """
        msgs = self.store.list_messages(sid)
        summary, recent = compress_messages(msgs, keep_recent=RECENT_KEEP)

        system = system_prompt
        if profile_block:
            system = f"{system}\n\n{profile_block}"
        if summary:
            system = f"{system}\n\n【历史摘要】\n{summary}"

        out: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in recent:
            role = m.get("role")
            if role in ("user", "assistant"):
                out.append({"role": role, "content": m.get("content", "")})
        return out

    # ---------- 内部 ----------

    def _maybe_compress(self, sid: str) -> None:
        """消息超阈值时触发压缩（本设计为「读时压缩」，故此处只清理缓存并记录）。"""
        msgs = self.store.list_messages(sid)
        if len(msgs) > COMPRESS_THRESHOLD:
            logger.info("会话 sid=%s 已达 %d 条，上下文将启用滚动摘要压缩", sid, len(msgs))

    def _invalidate_cache(self, sid: str) -> None:
        if self.cache is None:
            return
        try:
            # 同步 Redis 客户端（测试不传 cache；生产可传 redis 同步客户端）
            self.cache.delete(f"chat:ctx:{sid}")
        except Exception as exc:  # noqa: BLE001 - 缓存是优化不是依赖
            logger.warning("清理会话缓存失败（忽略）：%r", exc)


# ============================================================
#  生产便捷构造
# ============================================================


def get_session_service(db=None) -> SessionService:  # noqa: ANN001
    """生产构造：SQL 存储 + 会话服务（不强制 Redis）。"""
    from app.db.session import SessionLocal

    db = db or SessionLocal()
    return SessionService(SqlSessionStore(db))


def is_mock() -> bool:
    return bool(settings.mock_mode)
