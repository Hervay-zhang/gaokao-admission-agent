"""T13 测试 —— 政策切片（chunk）+ 检索（retrieve）。

对应任务卡「测试场景（必做）」：
    J1 相关查询能召回对应政策；
    J2 中文分词召回有效（不只整串匹配）；
    J3 MOCK_MODE 可跑通；
    J4 返回含 source_url。

补足：
- **切片不跨标题**守护（红线）；
- FULLTEXT ngram vs LIKE 的差异（任务卡「易混淆」点名要测）；
- 重叠窗口 / 关键词抽取 / 确定性伪向量（同文本同向量）；
- 检索排名单调性（keyword/向量融合不会把明显相关块排到后面）。

设计：**不依赖 MySQL / Redis / LLM** —— 用 ``InMemoryChunkRepository`` + 纯函数，
全部离线可复现。真数据断言（若有 ``policy_docs`` 落库）走可选分支。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.chunk import (  # noqa: E402
    chunk_document,
    extract_keywords,
    tokenize,
)
from app.rag.index import MOCK_EMBED_DIM, mock_embedding  # noqa: E402
from app.rag.retrieve import (  # noqa: E402
    InMemoryChunkRepository,
    Retriever,
    _cosine,
    _ngrams,
    _query_terms,
    load_embedding,
)

# ---- 测试语料（模拟真实政策结构：章节标题 + 条款）----
SAMPLE_DOC_A = """为做好我省2026年普通高校招生志愿填报工作，现就有关事项通知如下：
一、填报时间
（一）本科批次志愿填报时间为6月28日至7月4日。
（二）专科批次志愿填报时间为7月20日至7月25日。
二、录取批次
提前批录取安排在本科批次之前进行，包括军事、公安、师范、医学等类别。
普通本科批次录取在提前批结束后依次进行。
"""
SAMPLE_DOC_B = """广东省2026年普通高等学校专升本招生工作规定
一、报名条件
（一）遵守中华人民共和国宪法和法律，身体状况符合相关要求。
（二）具有广东省户籍的应届专科毕业生可以报考。
二、考试科目
考试科目为政治理论、英语和一门专业基础课。
"""
SAMPLE_DOC_C = """关于做好2026年高职院校自主招生工作的通知
一、招生院校及专业
2026年我省高职自主招生含普通高职自主招生、普通现代学徒制试点等4种招生类型。
二、报考基本条件
所有报考考生须符合广东省招生委员会规定，并参加了广东省普通高考报名。
"""


def _row(chunk_id: int, doc_id: int, seq: int, content: str, title: str, url: str, year: int, doc_type="通知"):
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "seq": seq,
        "content": content,
        "tokens": extract_keywords(content),
        "embedding": json.dumps(mock_embedding(content), ensure_ascii=False),
        "title": title,
        "source_url": url,
        "source": "广东省教育考试院",
        "year": year,
        "doc_type": doc_type,
    }


def _build_repo() -> InMemoryChunkRepository:
    rows: list[dict] = []
    cid = 1
    for doc_id, (text, title, url, year) in enumerate(
        [
            (SAMPLE_DOC_A, "关于做好2026年普通高校招生志愿填报工作的通知", "https://eea.gd.gov.cn/a1.html", 2026),
            (SAMPLE_DOC_B, "广东省2026年普通高等学校专升本招生工作规定", "https://eea.gd.gov.cn/b1.html", 2026),
            (SAMPLE_DOC_C, "关于做好2026年高职院校自主招生工作的通知", "https://eea.gd.gov.cn/c1.html", 2026),
        ],
        start=1,
    ):
        for ch in chunk_document(text):
            rows.append(_row(cid, doc_id, ch.seq, ch.content, title, url, year))
            cid += 1
    return InMemoryChunkRepository(rows)


# ============================================================
#  切片（chunk）
# ============================================================


def test_chunk_does_not_cross_heading():
    """红线：切片不跨标题 —— 同一块内不得出现两个不同章节标题。"""
    chunks = chunk_document(SAMPLE_DOC_A)
    assert chunks, "应切出至少一块"
    headings = [c.heading for c in chunks]
    # 一、 与 二、 不得出现在同一块
    for c in chunks:
        assert not ("一、填报时间" in c.content and "二、录取批次" in c.content)
    assert "一、填报时间" in headings
    assert "二、录取批次" in headings


def test_chunk_size_and_overlap():
    """长文本按 size 切分且相邻块有 overlap 字重叠。"""
    long_text = "志愿填报规则说明。" * 200  # ~1800 字，无标题 → 单段窗口切分
    chunks = chunk_document(long_text, size=500, overlap=50)
    assert len(chunks) >= 3
    assert all(len(c.content) <= 500 + 60 for c in chunks)  # 允许标题/换行冗余
    # 重叠：前块尾部 40 字应能在后块开头附近找到
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        tail = prev.content[-40:]
        assert tail[-20:] in nxt.content[:120], "相邻块应有重叠"


def test_chunk_empty_and_whitespace():
    assert chunk_document("") == []
    assert chunk_document("   \n  ") == []


def test_chunk_seq_contiguous():
    chunks = chunk_document(SAMPLE_DOC_A + SAMPLE_DOC_B)
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_chunk_short_paragraph_merged():
    """过短尾块并入上一块（同标题），避免碎块污染检索。"""
    text = "一、总则\n" + "本规定适用于所有考生。" * 10 + "\n附："
    chunks = chunk_document(text)
    assert all(len(c.content.strip()) >= 40 for c in chunks)


def test_j2_tokenize_chinese():
    """J2 基础：jieba 中文分词有效（不是整串匹配）。"""
    toks = tokenize("提前批录取安排在本科批次之前")
    assert "提前批" in toks or "提前" in toks
    assert "录取" in toks
    # 不是整串
    assert "提前批录取安排在本科批次之前" not in toks


def test_extract_keywords_dedup_and_order():
    kws = extract_keywords("志愿填报 志愿 填报 志愿 录取 录取 录取")
    words = kws.split()
    assert len(words) == len(set(words)), "关键词不应重复"
    # 高频词靠前
    assert words[0] in ("录取", "志愿", "填报")


# ============================================================
#  伪向量 & 余弦
# ============================================================


def test_j3_mock_embedding_deterministic():
    """J3 MOCK：伪向量确定性（同文本同向量）+ 单位化（模长≈1）。"""
    a = mock_embedding("提前批录取规则")
    b = mock_embedding("提前批录取规则")
    c = mock_embedding("专升本报名条件")
    assert a == b, "同文本应得同向量"
    assert a != c, "不同文本应得不同向量"
    assert len(a) == MOCK_EMBED_DIM
    assert abs(sum(x * x for x in a) ** 0.5 - 1.0) < 1e-6, "应为单位向量"
    assert _cosine(a, a) == pytest.approx(1.0, abs=1e-6)


def test_load_embedding_bad_json():
    assert load_embedding(None) == []
    assert load_embedding("not-json") == []
    assert load_embedding('{"a":1}') == []
    assert load_embedding("[0.1,0.2]") == [0.1, 0.2]


def test_ngrams():
    assert _ngrams("提前批", 2) == {"提前", "前批"}
    assert _ngrams("", 2) == set()


# ============================================================
#  检索（retrieve）
# ============================================================


def test_j1_retrieve_relevant_doc():
    """J1：相关查询能召回对应政策。"""
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("提前批录取", top_k=3)
    assert res.hits, "应有命中"
    top = res.hits[0]
    assert "提前批" in top.content or "录取" in top.content
    assert top.doc_id == 1, "应召回志愿填报那篇（doc 1）"


def test_j4_hits_contain_source_url():
    """J4：返回含 source_url（+ 来源元数据）。"""
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("专升本报名条件", top_k=3)
    assert res.hits
    for h in res.hits:
        assert h.source_url.startswith("http")
        assert h.title
        assert h.doc_id > 0
    d = res.hits[0].to_dict()
    for key in ("doc_id", "title", "source_url", "year", "score"):
        assert key in d


def test_j2_ngram_recall_not_exact_match():
    """J2：ngram 分词召回有效 —— 部分词（非整串）也能命中。

    守护语义（**不绑定具体排序**）：查询「高职自主招生」在原文档中写作
    「高职院校自主招生」，整串 LIKE 不命中，但 ngram（「高职」「自主」「招生」）
    应能召回该文档。故断言只看「doc C 是否被召回」，不看它排第几 ——
    排序由 T15 的加权/去重策略决定，本用例只守护「部分词可召回」本身。
    """
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("高职自主招生", top_k=5)
    assert res.hits, "部分词应能召回"
    doc_ids = {h.doc_id for h in res.hits}
    assert 3 in doc_ids, "ngram 应召回高职自主招生文档（doc 3）"
    # 召回到的该文档块正文应含「高职」/「自主招生」等词元
    doc_c_text = " ".join(h.content for h in res.hits if h.doc_id == 3)
    assert "高职" in doc_c_text


def test_fulltext_vs_like_difference():
    """FULLTEXT ngram ≠ LIKE：验证 ngram 能命中「词的部分」。"""
    content = "提前批次录取安排在本科批次之前进行"
    terms = _query_terms("提前录取")
    # LIKE 整串包含：'提前录取' 不在 content 中
    assert "提前录取" not in content
    # ngram 词元：'提前' / '录取' 均在 content 中 → 可命中
    assert "提前" in terms and "录取" in terms
    assert any(t in content for t in terms)


def test_retrieve_empty_query():
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    assert r.retrieve("").hits == []
    assert r.retrieve("   ").hits == []


def test_retrieve_scores_non_increasing():
    """排序单调：命中按分数降序。"""
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("录取批次 志愿填报", top_k=5)
    scores = [h.score for h in res.hits]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_hits_deduplicated_by_doc():
    """文档级去重：返回的 top-k 中每篇文档只出现一次（保留最高分块）。"""
    repo = _build_repo()
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("提前批录取 志愿填报", top_k=5)
    doc_ids = [h.doc_id for h in res.hits]
    assert len(doc_ids) == len(set(doc_ids)), "同一文档不应重复出现"


def test_retrieve_without_vector_still_works():
    """关闭向量重排（use_vector=False）仍能检索（关键词 + FULLTEXT 通路）。"""
    repo = _build_repo()
    r = Retriever(repo, embed_query=None, use_vector=False)
    res = r.retrieve("专升本考试科目", top_k=3)
    assert res.hits, "无向量也应有命中"
    assert all(h.score_vector == 0.0 for h in res.hits)


def test_retrieve_keyword_fallback_when_no_recall(monkeypatch):
    """粗召回为空时退化为全量 + 关键词打分（mode=keyword_fallback）。"""

    class _NoRecallRepo(InMemoryChunkRepository):
        def fulltext_search_docs(self, query_terms: str, limit: int):  # noqa: ANN001
            return []

    repo = _NoRecallRepo(_build_repo().rows)
    r = Retriever(repo, embed_query=mock_embedding)
    res = r.retrieve("自主招生", top_k=3)
    assert res.mode == "keyword_fallback"
    assert res.hits


def test_term_idf_rare_term_weighs_more():
    """IDF：稀有词权重高于泛词（用于加权覆盖率）。

    断言用「专升本（df=1）vs 招生（df=5）」：后者是跨文档泛词，前者只出现于专升本
    那篇，故稀有词 IDF 必然更大。**不出现词**取 IDF 上限，用「消防救援」验证。
    """
    repo = _build_repo()
    idf = repo.term_idf(["招生", "专升本"])
    assert idf["专升本"] > idf["招生"], "仅在单一文档出现的词 IDF 应更大"
    # 未出现词 df=0 → IDF 取上限（比任何实际出现的词都大）
    idf2 = repo.term_idf(["招生", "消防救援"])
    assert idf2["消防救援"] > idf2["招生"], "未出现词 IDF 应取上限（权重最大）"


# ============================================================
#  真数据（若 policy_chunks 已建则验证，否则 skip）
# ============================================================


@pytest.mark.smoke
def test_real_retrieval_if_indexed():
    """真 MySQL 冒烟：若有切片则验证「提前批」能命中带 url 的相关文档。"""
    try:
        from app.db.session import SessionLocal
        from app.rag.index import chunk_stats
        from app.rag.retrieve import get_retriever
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用：{exc}")

    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    try:
        if chunk_stats(db)["chunks"] == 0:
            pytest.skip("policy_chunks 为空（请先运行 app.rag.index）")
        r = get_retriever(db)
        res = r.retrieve("提前批录取", top_k=5)
        assert res.hits, "真数据应有命中"
        assert any("提前批" in h.content for h in res.hits)
        assert all(h.source_url.startswith("http") for h in res.hits)
    finally:
        db.close()
