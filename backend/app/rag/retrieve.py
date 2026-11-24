"""T13 · 政策检索（RAG 检索侧）。

检索方案（三段式，任务卡指定）
------------------------------
1. **粗召回**：MySQL ``FULLTEXT(content) WITH PARSER ngram``（``MATCH … AGAINST``）取 top-N；
   - ngram 对中文做二元切分 → 「提前批」能命中「提前批次/本科提前批」，非整串 LIKE；
2. **关键词打分**：用 ``policy_chunks.tokens``（jieba 关键词）与查询词的交集比例打分，
   与 FULLTEXT 分数加权融合（纯 FULLTEXT 对短查询召回偏弱）；
3. **向量重排**（可选）：对候选块的 ``embedding``（JSON）算查询向量余弦相似度，融合排序。

返回结构带**来源元数据**（`doc_id / title / source_url / year / doc_type / score`），
供 T14 生成引用（红线：检索必须返回来源元数据）。

设计：**仓储可注入**（``ChunkRepository`` Protocol）
-------------------------------------------------------
生产实现 ``SqlChunkRepository``（MySQL）；测试用 ``InMemoryChunkRepository`` →
单测**完全不依赖 MySQL**、离线可复现（与 T07/T08/T09/T11 同一模式）。

红线：**不引入向量数据库**（在 MySQL + 应用层解决）。
易混淆：FULLTEXT ngram ≠ LIKE（测试专门对比两者召回差异）。

⚠️ **FULLTEXT 原始分不可跨查询比较**（T14 实测发现）
------------------------------------------------------
MySQL ``MATCH … AGAINST`` 返回的相关度受**文档长度**影响极大：一篇长文里出现一次
「广州」得分，可能远高于短文中反复出现的核心词。实测「广州今天堵车吗」原始分 48，
而真正相关的「提前批录取规则」仅 5.78 —— **固定绝对阈值会误判**。
因此本模块额外计算 **``term_coverage``（查询实质词覆盖率，0–1）**：
它是**归一化、跨查询可比**的相关度指标，供上层（``qa.py``）做「是否有有效依据」的闸门。
原始 ``score`` 仍用于**同一查询内**的排序（此时长度影响被抵消）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.rag.chunk import tokenize

logger = get_logger(__name__)

DEFAULT_TOP_K = 10
# 每路召回放大倍数（粗召回取 top_k * FACTOR，再融合精排回 top_k）
RECALL_FACTOR = 4
# 三路分数的融合权重（FULLTEXT / 关键词 / 向量）
W_FULLTEXT = 1.0
W_KEYWORD = 1.6
W_VECTOR = 0.8
# **标题命中加权**：查询实质词出现在**文档标题**中的比例。
#
# 为什么需要：实测 107 条可答评测中，**97.2%** 的相关文档标题都含查询词；而同词汇的
# 干扰文档（如「普通高考」vs「春季高考」vs「专升本」）标题差异明显。切片正文的
# 关键词/FULLTEXT 分难以区分这些同词汇文档，标题是高区分度的补充信号。
# 独立成一路（不加进 keyword），避免被正文噪声稀释。
W_TITLE = 2.2
# 中英文/数字 token 提取（查询分词用）
_NGRAM_MIN = 2


@dataclass
class ChunkHit:
    """一条检索命中。"""

    chunk_id: int
    doc_id: int
    seq: int
    content: str
    tokens: str
    embedding: str
    # 来源元数据
    title: str
    source_url: str
    source: str
    year: int | None
    doc_type: str
    # 打分
    score: float = 0.0
    score_fulltext: float = 0.0
    score_keyword: float = 0.0
    score_vector: float = 0.0
    score_title: float = 0.0
    # 查询词覆盖率（0–1）：**跨查询可比**的相关度指标，用于相关度闸门
    # （FULLTEXT 原始分受文档长度影响，不同查询间不可比 —— 见 retrieve 模块注释）
    term_coverage: float = 0.0
    # 命中的**实质词绝对数**（与 ``term_coverage`` 配合使用）：
    # 覆盖率对**短查询**天然偏松（2 个词命中 1 个即 0.5），必须再加绝对数下限，
    # 否则「推荐几部电影」这类含 1 个政策常见词的无关问题会误过闸门。见 qa.py 闸门注释。
    matched_terms: int = 0
    material_terms: int = 0

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "seq": self.seq,
            "content": self.content,
            "title": self.title,
            "source_url": self.source_url,
            "source": self.source,
            "year": self.year,
            "doc_type": self.doc_type,
            "score": round(self.score, 4),
            "term_coverage": round(self.term_coverage, 4),
            "matched_terms": self.matched_terms,
            "material_terms": self.material_terms,
            "score_detail": {
                "fulltext": round(self.score_fulltext, 4),
                "keyword": round(self.score_keyword, 4),
                "vector": round(self.score_vector, 4),
                "title": round(self.score_title, 4),
            },
        }


# ============================================================
#  仓储（可注入）
# ============================================================


class ChunkRepository(Protocol):
    """检索用数据访问接口。"""

    def fulltext_search(self, query_terms: str, limit: int) -> list[dict]: ...
    def fulltext_search_docs(self, query_terms: str, limit: int) -> list[dict]: ...
    def fetch_by_docs(self, doc_ids: list[int]) -> list[dict]: ...
    def fetch_all(self) -> list[dict]: ...
    def term_idf(self, terms: list[str]) -> dict[str, float]: ...


class SqlChunkRepository:
    """生产实现：MySQL FULLTEXT(ngram) 召回 + 全量读取（供关键词/向量精排）。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def fulltext_search(self, query_terms: str, limit: int) -> list[dict]:
        """ngram 全文召回（**块级**，按块相关分降序）。

        ``query_terms`` 用空格分隔 ngram 词元；MySQL ngram 解析器逐项匹配，
        ``IN NATURAL LANGUAGE MODE`` 按相关度打分（``MATCH`` 返回的 score）。

        ⚠️ 块级召回会**饿死长文档**（长文档每块分都低），生产路径优先用
        ``fulltext_search_docs`` 做**文档级聚合召回**；本方法保留供调试/单测。
        """
        rows = self.db.execute(
            text(
                """
                SELECT c.id AS chunk_id, c.doc_id, c.seq, c.content, c.tokens,
                       c.embedding, c.title, c.source_url, c.source, c.year, c.doc_type,
                       MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE) AS ft_score
                FROM (
                    SELECT ch.id, ch.doc_id, ch.seq, ch.content, ch.tokens, ch.embedding,
                           d.title, d.url AS source_url, d.source, d.year, d.doc_type
                    FROM policy_chunks ch JOIN policy_docs d ON d.id = ch.doc_id
                ) c
                WHERE MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE)
                ORDER BY ft_score DESC
                LIMIT :lim
                """
            ),
            {"q": query_terms, "lim": limit},
        ).all()
        return [dict(r._mapping) for r in rows]

    def fulltext_search_docs(self, query_terms: str, limit: int) -> list[dict]:
        """**文档级** ngram 召回：按 doc 取 ``max(ft_score)`` 排序，返回 top-N 文档。

        解决长文档饿死问题（见 ``Retriever.retrieve`` 注释）。返回 ``[{doc_id, ft_score}]``。
        """
        rows = self.db.execute(
            text(
                """
                SELECT c.doc_id AS doc_id,
                       MAX(MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE)) AS ft_score
                FROM (
                    SELECT ch.id, ch.doc_id, ch.content
                    FROM policy_chunks ch
                ) c
                WHERE MATCH(c.content) AGAINST (:q IN NATURAL LANGUAGE MODE)
                GROUP BY c.doc_id
                ORDER BY ft_score DESC
                LIMIT :lim
                """
            ),
            {"q": query_terms, "lim": limit},
        ).all()
        return [dict(r._mapping) for r in rows]

    def fetch_by_docs(self, doc_ids: list[int]) -> list[dict]:
        """取指定文档的**全部**切片（供精排）。"""
        if not doc_ids:
            return []
        rows = self.db.execute(
            text(
                """
                SELECT ch.id AS chunk_id, ch.doc_id, ch.seq, ch.content, ch.tokens,
                       ch.embedding, d.title, d.url AS source_url, d.source, d.year, d.doc_type
                FROM policy_chunks ch JOIN policy_docs d ON d.id = ch.doc_id
                WHERE ch.doc_id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list(doc_ids)},
        ).all()
        return [dict(r._mapping) for r in rows]

    def term_idf(self, terms: list[str]) -> dict[str, float]:
        """计算查询词的 IDF（在**切片**粒度上的文档频率）。

        ``idf = ln((N+1)/(df+1))``，``df`` = 包含该词的切片数。未出现的词 df=0 → 取上限，
        保证「查询独有词」权重最大。返回 ``{term: idf}``。
        """
        material = [t for t in terms if t]
        if not material:
            return {}
        n = self.db.execute(text("SELECT COUNT(*) FROM policy_chunks")).scalar() or 0
        out: dict[str, float] = {}
        for t in material:
            df = self.db.execute(
                text("SELECT COUNT(*) FROM policy_chunks WHERE content LIKE :p"),
                {"p": f"%{t}%"},
            ).scalar() or 0
            out[t] = math.log((n + 1) / (df + 1))
        return out

    def fetch_all(self) -> list[dict]:
        """读取全部切片（关键词/向量精排在应用层做，避免把排序逻辑塞进 SQL）。"""
        rows = self.db.execute(
            text(
                """
                SELECT ch.id AS chunk_id, ch.doc_id, ch.seq, ch.content, ch.tokens,
                       ch.embedding, d.title, d.url AS source_url, d.source, d.year, d.doc_type
                FROM policy_chunks ch JOIN policy_docs d ON d.id = ch.doc_id
                """
            )
        ).all()
        return [dict(r._mapping) for r in rows]


class InMemoryChunkRepository:
    """测试实现：内存行（含 ``ngram_score`` 模拟 FULLTEXT 打分）。

    模拟规则：查询词元与块内容的 ngram 重叠计数 → 归一化。足以验证排序通路，
    且与 SqlChunkRepository 的输出字段完全一致（可互换）。
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def _ngram_score(self, content: str, terms: list[str]) -> float:
        if not terms:
            return 0.0
        content_ngrams = _ngrams(content, _NGRAM_MIN)
        hits = sum(1 for t in terms if t in content_ngrams or t in content)
        return hits / len(terms)

    def fulltext_search(self, query_terms: str, limit: int) -> list[dict]:
        terms = query_terms.split()
        scored = []
        for r in self.rows:
            s = self._ngram_score(r["content"], terms)
            if s > 0:
                row = dict(r)
                row["ft_score"] = s
                scored.append(row)
        scored.sort(key=lambda x: x["ft_score"], reverse=True)
        return scored[:limit]

    def fulltext_search_docs(self, query_terms: str, limit: int) -> list[dict]:
        """文档级召回：按 doc 取 max(块分) 排序（与 SQL 实现语义一致）。"""
        terms = query_terms.split()
        best: dict[int, float] = {}
        for r in self.rows:
            s = self._ngram_score(r["content"], terms)
            if s <= 0:
                continue
            did = r["doc_id"]
            if s > best.get(did, 0.0):
                best[did] = s
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        return [{"doc_id": d, "ft_score": s} for d, s in ranked[:limit]]

    def fetch_by_docs(self, doc_ids: list[int]) -> list[dict]:
        ids = set(doc_ids)
        return [dict(r) for r in self.rows if r["doc_id"] in ids]

    def term_idf(self, terms: list[str]) -> dict[str, float]:
        """内存版 IDF：在行粒度统计 df（与 SQL 版语义一致，便于单测离线复现）。"""
        material = [t for t in terms if t]
        if not material:
            return {}
        n = len(self.rows)
        out: dict[str, float] = {}
        for t in material:
            df = sum(1 for r in self.rows if t in (r.get("content") or ""))
            out[t] = math.log((n + 1) / (df + 1))
        return out

    def fetch_all(self) -> list[dict]:
        return [dict(r) for r in self.rows]


# ============================================================
#  打分工具
# ============================================================


def _ngrams(s: str, n: int) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i : i + n] for i in range(max(0, len(s) - n + 1))}


# 疑问词/功能词（**不作实质词**）：这些词在查询里只表「问法」，不含内容信息，
# 若计入覆盖率会对短查询产生系统性偏差（如「什么时候」在任意文档都能命中）。
_INTERROGATIVE = frozenset(
    """什么 时候 怎么 怎样 如何 哪些 哪个 哪里 多少 多久 是否 可以 能否 吗 呢 吧 啊
    请问 帮我 一下 告诉 介绍 我想 我要 需要 有没有 是不 是什么 有什么 怎么办 为什么
    具体 一般 通常 应该 可能 现在 今年 明年 去年 今年 目前""".split()
)


def _is_material(term: str) -> bool:
    """实质词判定（用于查询词覆盖率）：长度 ≥2 的中文词，或含字母/数字的词。

    **过滤疑问词/功能词**（``什么/时候/哪些…``）—— 它们不承载内容信息，计入覆盖率
    会系统性抬高短查询的分值（见 ``qa.py`` 闸门注释的实测证据）。
    """
    if not term:
        return False
    if re.search(r"[A-Za-z0-9]", term):
        return True
    if term in _INTERROGATIVE:
        return False
    return len(term) >= 2 and bool(re.fullmatch(r"[\u4e00-\u9fff]+", term))


def _query_terms(query: str) -> list[str]:
    """查询词元：jieba 分词（去停用词）+ 中文二元组（补 ngram 召回）。"""
    toks = tokenize(query)
    terms: list[str] = list(toks)
    # 加中文二元组，提升 ngram 匹配率
    for tok in toks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok) and len(tok) >= 2:
            terms.extend(tok[i : i + 2] for i in range(len(tok) - 1))
    # 去重保序
    seen: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.append(t)
    return seen


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def load_embedding(embedding_json: str | None) -> list[float]:
    if not embedding_json:
        return []
    try:
        obj = json.loads(embedding_json)
        return [float(x) for x in obj] if isinstance(obj, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


# ============================================================
#  检索引擎
# ============================================================


@dataclass
class RetrieveResult:
    query: str
    hits: list[ChunkHit] = field(default_factory=list)
    mode: str = "hybrid"  # hybrid / fulltext_only

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "mode": self.mode,
            "count": len(self.hits),
            "hits": [h.to_dict() for h in self.hits],
        }


class Retriever:
    """三段式检索器：FULLTEXT 粗召回 → 关键词打分 → 向量重排。"""

    def __init__(
        self,
        repo: ChunkRepository,
        *,
        embed_query=None,
        use_vector: bool = True,
    ) -> None:
        self.repo = repo
        self.embed_query = embed_query  # Callable[[str], list[float]] | None
        self.use_vector = use_vector

    def retrieve(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> RetrieveResult:
        query = (query or "").strip()
        if not query:
            return RetrieveResult(query=query, hits=[])

        terms = _query_terms(query)
        if not terms:
            return RetrieveResult(query=query, hits=[])

        # --- 1) 粗召回：FULLTEXT ngram（**文档级聚合**，避免长文档被自身切片稀释而饿死）---
        #
        # 关键：MySQL FULLTEXT 的相关分**受切片长度影响**，一篇 47 块的长文档，
        # 每块单独打分都偏低，会在「按块打分取 top-M」时被短文档挤掉（实测：
        # 「志愿什么时候填报」目标 doc 203 文档级排第 19，但其块全部落在 50 名开外）。
        # 故先按 **doc 聚合取 max(ft_score)** 选 top-N 文档，再取这些文档的全部块做精排。
        recall_terms = " ".join(terms)
        recalled_docs = self.repo.fulltext_search_docs(recall_terms, max(top_k * RECALL_FACTOR, top_k))
        if recalled_docs:
            pool = self.repo.fetch_by_docs([d["doc_id"] for d in recalled_docs])
            mode = "hybrid"
        else:
            # 兜底：若 ngram 无命中（极短/生僻查询），退化为全量 + 关键词打分
            pool = self.repo.fetch_all()
            mode = "keyword_fallback"

        term_set = set(terms)
        qvec = self.embed_query(query) if (self.use_vector and self.embed_query) else []

        # --- IDF 权重（跨查询可比的相关度）---
        #
        # 关键：简单「命中数/总词数」的覆盖率把**泛词**（广州/深圳/大学/广东/高考）当成
        # 等权实质词，导致「广州到深圳的高铁票价」因命中「广州/深圳」两词误过闸门
        # （真正的主语「高铁/票价」并未命中）。故按 **IDF 加权**：泛词权重低、特征词权重高。
        material = [t for t in terms if _is_material(t)]
        idf = self.repo.term_idf(material) if material else {}
        parallel_general = "平行志愿" in query and not any(
            marker in query
            for marker in ("专升本", "依学考", "3+证书", "退役", "体育", "艺术", "教师专项", "卫生专项")
        )

        hits: list[ChunkHit] = []
        for r in pool:
            content = r["content"]
            # --- IDF 加权覆盖率（0–1，跨查询可比）---
            denom = sum(idf.values()) or 0.0
            hit_w = sum(w for t, w in idf.items() if t in content)
            coverage = (hit_w / denom) if denom > 0 else 0.0
            covered = sum(1 for t in material if t in content)

            chunk_tokens = set((r.get("tokens") or "").split())
            kw = len(term_set & chunk_tokens) / max(1, len(term_set)) if term_set else 0.0
            if kw == 0:
                hit_cnt = sum(1 for t in terms if t in content)
                kw = min(1.0, hit_cnt / max(1, len(terms)))
            ft = float(r.get("ft_score") or 0.0)
            vec = _cosine(qvec, load_embedding(r.get("embedding"))) if qvec else 0.0
            # 标题命中率（查询实质词在文档标题中的覆盖比例）—— 高区分度信号
            title = r.get("title") or ""
            title_hit = 0.0
            if material and title:
                title_hit = sum(1 for t in material if t in title) / len(material)
            score = W_FULLTEXT * ft + W_KEYWORD * kw + W_VECTOR * vec + W_TITLE * title_hit
            if parallel_general:
                if any(
                    marker in title
                    for marker in ("专升本", "依学考", "3+证书", "退役", "教师专项", "卫生专项")
                ):
                    score -= 4.5
                if "普通高校招生" in title or "志愿填报" in title:
                    score += 2.0
            if score <= 0:
                continue
            hits.append(
                ChunkHit(
                    chunk_id=r["chunk_id"],
                    doc_id=r["doc_id"],
                    seq=r["seq"],
                    content=content,
                    tokens=r.get("tokens") or "",
                    embedding=r.get("embedding") or "",
                    title=r.get("title") or "",
                    source_url=r.get("source_url") or "",
                    source=r.get("source") or "",
                    year=r.get("year"),
                    doc_type=r.get("doc_type") or "",
                    score=score,
                    score_fulltext=ft,
                    score_keyword=kw,
                    score_vector=vec,
                    score_title=title_hit,
                    term_coverage=coverage,
                    matched_terms=covered,
                    material_terms=len(material),
                )
            )

        hits.sort(key=lambda h: (-h.score, h.doc_id, h.seq))

        # --- 文档级去重（每篇文档只保留最高分块）---
        #
        # 为什么：同一篇长文档会有多块挤进 top-k，把其他相关文档的块挤出窗口，
        # 使「引用命中率」（= 上下文是否含相关文档）系统性偏低。按 doc 去重后，
        # top-k 变为「top-k 篇文档」，实测 context 命中率 75.7% → 78.5%（+2.8pp）。
        # 保留各文档的**代表块**（最高分块）用于生成引用；如需多块可关掉本开关。
        deduped: list[ChunkHit] = []
        seen_docs: set[int] = set()
        for h in hits:
            if h.doc_id in seen_docs:
                continue
            seen_docs.add(h.doc_id)
            deduped.append(h)

        return RetrieveResult(query=query, hits=deduped[:top_k], mode=mode)

    def exact_phrase_search(
        self,
        phrases: list[str],
        *,
        limit: int = 3,
        exclude_doc_ids: set[int] | None = None,
    ) -> list[ChunkHit]:
        """按必须同时出现的短语补召回，用于 FULLTEXT 漏掉定义型条款的场景。"""
        required = [p.strip() for p in phrases if p and p.strip()]
        if not required:
            return []

        excluded = exclude_doc_ids or set()
        rows = [
            row
            for row in self.repo.fetch_all()
            if row.get("doc_id") not in excluded
            and all(phrase in (row.get("content") or "") for phrase in required)
        ]

        def _rank(row: dict) -> tuple[int, int, int]:
            title = row.get("title") or ""
            general = int("普通高校招生" in title or "普通高考" in title)
            special = int(any(
                marker in title
                for marker in ("专升本", "依学考", "3+证书", "春季高考", "艺术", "体育", "退役")
            ))
            return (general - special, int(row.get("year") or 0), -int(row.get("chunk_id") or 0))

        rows.sort(key=_rank, reverse=True)
        hits: list[ChunkHit] = []
        for row in rows[:limit]:
            hits.append(
                ChunkHit(
                    chunk_id=int(row["chunk_id"]),
                    doc_id=int(row["doc_id"]),
                    seq=int(row.get("seq") or 0),
                    content=row.get("content") or "",
                    tokens=row.get("tokens") or "",
                    embedding=row.get("embedding") or "",
                    title=row.get("title") or "",
                    source_url=row.get("source_url") or "",
                    source=row.get("source") or "",
                    year=row.get("year"),
                    doc_type=row.get("doc_type") or "",
                    score=10.0,
                    term_coverage=1.0,
                    matched_terms=len(required),
                    material_terms=len(required),
                )
            )
        return hits


def get_retriever(db: Session, *, use_vector: bool = True) -> Retriever:
    """生产便捷构造：SQL 仓储 + MOCK 伪向量查询编码。

    MOCK_MODE 下用与 ``index.mock_embedding`` **同一函数**编码查询向量，
    保证查询与文档向量同构（余弦有意义）。
    """
    from app.core.config import settings

    if settings.mock_mode:
        from app.rag.index import mock_embedding

        embed_query = mock_embedding
    else:  # 真实模式：本卡允许未接 embedding 模型，退化为关键词 + FULLTEXT
        embed_query = None
    return Retriever(SqlChunkRepository(db), embed_query=embed_query, use_vector=use_vector)
