"""T18 · 输出后置校验（免责兜底 + 越界检测 + 疑似编造标记）。

第二道防线
----------
前置约束（`app/agent/prompt.py`）告诉模型「该怎么做」；后置校验（本模块）检查「实际做了什么」。
**两道防线都要有**（任务卡「易混淆」）。

三项校验
--------
1. **免责声明**：回答末尾是否含 ``DISCLAIMER``；**缺失即补**（后置兜底，红线）；
2. **越界内容**：回答是否流露出无关领域（本模块只做**轻量检测**，真越界已由 T16 路由前置拦截）；
3. **疑似编造**：回答里出现的**院校名**是否在库内可查 —— 不在库的**标记** ``suspected_fabrication``
   （**只标记不改写**：后置校验不得改变事实内容，交由上层决定是否降级/回落）。

设计原则
--------
- **只校验/补注入，不再生成**（任务卡「易混淆」）：本模块**绝不调用 LLM**，**绝不改写事实**；
- **可注入院校源**：`UniversityLookup` Protocol（生产=MySQL，测试=内存集合），单测离线可复现；
- **确定性**：同输入同输出，便于回归。

红线
----
- 免责声明**缺失即补**，且**不因补注入而改写原有正文**（只在末尾追加）；
- 后置校验**不得改变事实内容**（编造只标记，不删除、不替换）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.rag.qa import DISCLAIMER

logger = get_logger(__name__)

# 越界领域词（轻量检测；真越界已由 T16 路由拦，这里是「模型跑偏」的最后一道网）
_GUARD_OUT_SCOPE_TERMS: tuple[str, ...] = (
    "天气预报", "今天天气", "股票", "基金走势", "彩票", "笑话", "写代码", "Python", "Java",
    "菜谱", "减肥", "看电影", "电视剧", "游戏攻略", "比特币",
)

# 疑似院校名模式：以「大学 / 学院 / 职业技术学院」结尾的中文串
_UNIVERSITY_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:大学|学院|职业技术学院|职业技术大学)")


# ============================================================
#  院校查找接口
# ============================================================


@runtime_checkable
class UniversityLookup(Protocol):
    """院校名查找接口（用于疑似编造检测）。"""

    def exists(self, name: str) -> bool: ...

    def known_names(self) -> set[str]: ...


class InMemoryUniversityLookup:
    """内存实现（测试用）。"""

    def __init__(self, names: set[str] | None = None) -> None:
        self._names = set(names or set())

    def exists(self, name: str) -> bool:
        return name in self._names

    def known_names(self) -> set[str]:
        return set(self._names)


class SqlUniversityLookup:
    """MySQL 实现（``universities`` 表）。

    :param owns_db: 是否拥有 ``db`` 生命周期（自建时 True；外部注入时 False，关闭由调用方负责）。
        ``close()`` 只关闭自建会话，避免与 FastAPI 依赖注入的 ``get_db`` 双重释放。
    """

    def __init__(self, db, *, owns_db: bool = False) -> None:  # noqa: ANN001
        self.db = db
        self._owns_db = owns_db
        self._cache: set[str] | None = None

    def _load(self) -> set[str]:
        from app.db.models import University

        rows = self.db.query(University.name).all()
        return {r[0] for r in rows if r[0]}

    def known_names(self) -> set[str]:
        if self._cache is None:
            self._cache = self._load()
        return self._cache

    def exists(self, name: str) -> bool:
        # 先精确匹配；再对「简称」做包含匹配（如「中山大学」vs「中山大学(珠海)」）
        names = self.known_names()
        if name in names:
            return True
        return any(name in n or n in name for n in names if n)

    def close(self) -> None:
        """释放自建 DB 会话（外部注入的 db 不关闭）。"""
        if self._owns_db and self.db is not None:
            self.db.close()
            self.db = None


# ============================================================
#  校验结果
# ============================================================


@dataclass
class GuardResult:
    """后置校验结果。"""

    answer: str                                   # 可能被**补注入免责**后的回答
    disclaimer_added: bool = False                # 是否补注入过免责
    has_disclaimer: bool = True                   # 最终是否含免责
    out_of_scope: bool = False                    # 是否检测到跑偏内容
    out_of_scope_hits: list[str] = field(default_factory=list)
    suspected_fabrication: list[str] = field(default_factory=list)   # 疑似编造的院校名
    rewritten: bool = False                       # 是否改写过正文（应恒为 False，除非仅追加免责）
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclaimer_added": self.disclaimer_added,
            "has_disclaimer": self.has_disclaimer,
            "out_of_scope": self.out_of_scope,
            "out_of_scope_hits": self.out_of_scope_hits,
            "suspected_fabrication": self.suspected_fabrication,
            "rewritten": self.rewritten,
            "notes": self.notes,
        }


# ============================================================
#  校验逻辑
# ============================================================


def has_disclaimer(answer: str) -> bool:
    """回答是否已含免责声明（正文包含即可）。"""
    return DISCLAIMER in (answer or "")


def enforce_disclaimer(answer: str, *, disclaimer: str = DISCLAIMER) -> tuple[str, bool]:
    """确保回答末尾含免责声明。**缺失即补**（只在末尾追加，不改写原有正文）。

    :return: ``(最终回答, 是否补注入过)``
    """
    text = answer or ""
    if disclaimer in text:
        return text, False
    # 末尾追加（保证与正文有空行分隔，可读）
    sep = "\n\n" if text.strip() and not text.rstrip().endswith("\n") else ""
    return f"{text.rstrip()}{sep}{disclaimer}", True


def detect_out_of_scope(answer: str) -> list[str]:
    """轻量检测回答是否跑偏到无关领域（返回命中的词）。"""
    text = answer or ""
    return [term for term in _GUARD_OUT_SCOPE_TERMS if term in text]


def detect_fabricated_universities(answer: str, lookup: UniversityLookup | None) -> list[str]:
    """检测回答中出现的**疑似编造院校名**（不在库内）。

    **只标记，不改写**：后置校验不得改变事实内容（任务卡红线）。
    ``lookup=None`` 时跳过检测（无院校源 → 不误报）。
    """
    if lookup is None:
        return []
    text = answer or ""
    candidates = set(_UNIVERSITY_RE.findall(text))
    fabricated: list[str] = []
    for name in candidates:
        if not lookup.exists(name):
            fabricated.append(name)
    return fabricated


def post_validate(
    answer: str,
    *,
    lookup: UniversityLookup | None = None,
    disclaimer: str = DISCLAIMER,
) -> GuardResult:
    """对模型输出做后置校验（免责补注入 + 越界检测 + 编造标记）。

    **纯函数、不调 LLM、不改写事实**；仅「补注入免责」会改变文本（且只在末尾追加）。
    """
    original = answer or ""

    # 1) 免责：缺失即补
    final_answer, added = enforce_disclaimer(original, disclaimer=disclaimer)

    # 2) 越界检测（轻量）
    oos_hits = detect_out_of_scope(final_answer)

    # 3) 疑似编造院校名（只标记）
    fabricated = detect_fabricated_universities(final_answer, lookup)

    notes: list[str] = []
    if added:
        notes.append("模型漏掉免责声明，已后置补注入")
    if oos_hits:
        notes.append(f"检测到疑似跑偏内容：{'、'.join(oos_hits)}")
    if fabricated:
        notes.append(f"疑似编造院校名（库内不可查）：{'、'.join(fabricated)}")

    if added or oos_hits or fabricated:
        logger.info(
            "后置校验：免责补注入=%s 跑偏=%s 疑似编造=%s",
            added, bool(oos_hits), len(fabricated),
        )

    return GuardResult(
        answer=final_answer,
        disclaimer_added=added,
        has_disclaimer=has_disclaimer(final_answer),
        out_of_scope=bool(oos_hits),
        out_of_scope_hits=oos_hits,
        suspected_fabrication=fabricated,
        # 仅当「补免责」时正文末尾被追加；正文自身**零改写**
        rewritten=False,
        notes=notes,
    )


# ============================================================
#  生产便捷构造
# ============================================================


def get_university_lookup(db=None) -> UniversityLookup | None:  # noqa: ANN001
    """生产构造：SQL 院校查找（DB 不可用时返回 None，检测自动跳过）。

    自建会话（``db=None``）由返回对象的 ``close()`` 负责释放；外部注入的 ``db``
    仍由调用方管理生命周期（如 FastAPI 依赖注入的 ``get_db``）。
    """
    try:
        from app.db.session import SessionLocal

        owns_db = db is None
        db = db or SessionLocal()
        return SqlUniversityLookup(db, owns_db=owns_db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("院校查找源不可用，跳过编造检测：%r", exc)
        return None
