"""T14 测试 —— 政策问答（RAG 生成 + 防幻觉 + 引用溯源 + 兜底）。

对应任务卡「测试场景（必做）」：
    J1 **有依据回答带引用**：命中政策 → 生成回答 → ``citations`` 非空且带 url；
    J2 **无依据触发兜底（不生成）**：无有效依据 → 固定兜底话术，**不调用 LLM**；
    J3 **引用编号与文档对应**：文中 ``[n]`` 能映射回真实文档（标题/url 来自检索，非模型）；
    J4 **诱导性问题被拒**：要求「编一个 XX 大学」等 → 不生成编造内容（走兜底/拒答）。

补足：
- 红线守护 —— 兜底话术**固定**（不允许模型改写）；
- 红线守护 —— 引用**结构性防伪造**（模型编的标题/URL 一律不采信）；
- 相关度闸门（``term_coverage``）对无关问题的拦截（T14 修复点）；
- 同一文档多切片**共用一个引用编号**；
- 免责声明常量在所有路径都注入。

设计：**不依赖 MySQL / Redis** —— 用 ``InMemoryChunkRepository`` + 假 LLM，全离线可复现。
真数据断言（若 ``policy_chunks`` 落库）走可选 smoke 分支。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.llm import LLMResponse  # noqa: E402
from app.rag.chunk import chunk_document, extract_keywords  # noqa: E402
from app.rag.index import mock_embedding  # noqa: E402
from app.rag.qa import (  # noqa: E402
    DEFAULT_MIN_COVERAGE,
    DISCLAIMER,
    FALLBACK_TEXT,
    PolicyQA,
    build_context,
    extract_cited_indexes,
    get_policy_qa,
)
from app.rag.retrieve import InMemoryChunkRepository, Retriever  # noqa: E402

# ---- 测试语料（与 T13 同构：章节标题 + 条款）----
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


def _row(chunk_id, doc_id, seq, content, title, url, year, doc_type="通知"):
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
        ],
        start=1,
    ):
        for ch in chunk_document(text):
            rows.append(_row(cid, doc_id, ch.seq, ch.content, title, url, year))
            cid += 1
    return InMemoryChunkRepository(rows)


class FakeLLM:
    """受控假 LLM：按脚本返回回答，并记录是否被调用（用于验证「无依据不生成」）。"""

    def __init__(self, content: str = "根据规定，提前批录取在本科批次之前进行[1]。") -> None:
        self._content = content
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, temperature=0.3, max_tokens=None, **_) -> LLMResponse:  # noqa: ANN001
        self.calls.append(messages)
        return LLMResponse(content=self._content, model="fake-llm", mocked=False, usage={})

    @property
    def called(self) -> bool:
        return bool(self.calls)


def _qa(llm: FakeLLM | None = None, *, min_coverage: float = DEFAULT_MIN_COVERAGE) -> PolicyQA:
    return PolicyQA(Retriever(_build_repo(), embed_query=mock_embedding), llm=llm or FakeLLM(), min_coverage=min_coverage)


# ============================================================
#  J1 有依据回答带引用
# ============================================================


def test_j1_grounded_answer_with_citations():
    """J1：有依据 → 生成回答，citations 非空且带 url/标题（来自检索，非模型）。"""
    llm = FakeLLM("提前批录取安排在本科批次之前进行[1]。")
    qa = _qa(llm)
    ans = qa_answer(qa, "提前批录取规则")

    assert ans.grounded is True
    assert ans.fallback is False
    assert llm.called, "有依据时必须调用 LLM"
    assert ans.citations, "应带引用"
    for c in ans.citations:
        assert c.url.startswith("http")
        assert c.title
        assert c.doc_id > 0
    assert ans.disclaimer == DISCLAIMER


def test_j1_context_contains_policy_section():
    """J1：送进 LLM 的 System Prompt 必含「政策依据」段 + 来源标注。"""
    llm = FakeLLM("…[1]")
    qa = _qa(llm)
    qa_answer(qa, "提前批录取规则")
    system = llm.calls[0][0]["content"]
    assert "政策依据" in system
    assert "[1]" in system
    assert "已" in system or "来源" in system


# ============================================================
#  J2 无依据触发兜底（不生成）
# ============================================================


def test_j2_no_evidence_fallback_without_llm():
    """J2：无关问题（无有效依据）→ 固定兜底话术，**不调用 LLM**。"""
    llm = FakeLLM()
    qa = _qa(llm)
    ans = qa_answer(qa, "湛江的天气怎么样")

    assert ans.fallback is True
    assert ans.grounded is False
    assert ans.answer == FALLBACK_TEXT, "必须是固定兜底话术"
    assert ans.citations == []
    assert not llm.called, "红线：无依据不得调用 LLM 生成"


def test_j2_empty_question_fallback():
    qa = _qa(FakeLLM())
    for q in ("", "   "):
        ans = qa_answer(qa, q)
        assert ans.fallback is True
        assert ans.answer == FALLBACK_TEXT


def test_j2_gate_blocks_low_coverage(monkeypatch):
    """闸门：把阈值抬到 1.0（要求全覆盖）→ 普通查询也被判无依据。"""
    llm = FakeLLM()
    qa = PolicyQA(Retriever(_build_repo(), embed_query=mock_embedding), llm=llm, min_coverage=1.01)
    ans = qa_answer(qa, "提前批录取规则")
    assert ans.fallback is True
    assert not llm.called


# ============================================================
#  J3 引用编号与文档对应
# ============================================================


def test_j3_citation_index_maps_to_real_doc():
    """J3：文中 [n] 能映射回真实文档，且 index 连续、doc_id 唯一。"""
    llm = FakeLLM("结论一……[1]；结论二……[2]。")
    qa = _qa(llm)
    ans = qa_answer(qa, "专升本报名条件")

    assert ans.citations
    idxs = [c.index for c in ans.citations]
    assert idxs == sorted(idxs)
    # 编号从 1 起、无重复
    assert idxs[0] == 1
    assert len(idxs) == len(set(idxs))
    # doc_id 唯一（同一文档多切片共享一个编号）
    doc_ids = [c.doc_id for c in ans.citations]
    assert len(doc_ids) == len(set(doc_ids))
    # 抽取编号函数一致
    assert extract_cited_indexes("a[1] b[2] c[2] d[1]") == [1, 2]


def test_j3_citation_identity_from_retrieval_not_model():
    """结构性防伪造：模型在正文里编造标题/URL，citations 仍取检索结果。"""
    # 模型输出里塞入伪造来源；后端只认编号
    forged = "据《清华大学2026年自主招生简章》[1]（http://fake.example.com）……"
    qa = _qa(FakeLLM(forged))
    ans = qa_answer(qa, "专升本报名条件")
    for c in ans.citations:
        assert c.url.startswith("https://eea.gd.gov.cn/"), "引用 URL 必须来自检索文档"
        assert "fake.example.com" not in c.url
        assert "清华" not in c.title


def test_j3_same_doc_shared_index():
    """同一文档的多个切片共用一个引用编号。"""
    repo = _build_repo()
    ret = Retriever(repo, embed_query=mock_embedding)
    res = ret.retrieve("提前批录取 志愿填报", top_k=5)
    docs = [h.doc_id for h in res.hits]
    if len(docs) != len(set(docs)):
        # 确有同文档多切片 → 校验编号复用
        _ctx, cites = build_context(res.hits)
        assert len(cites) == len(set(c.doc_id for c in res.hits))
        assert len(cites) < len(res.hits) or len(cites) == len(set(docs))


def test_j3_no_citation_when_model_omits_number():
    """模型忘记标 [n] 时，兜底给全部引用（便于溯源），不产生空引用。"""
    qa = _qa(FakeLLM("这里是一段没有任何编号标注的回答。"))
    ans = qa_answer(qa, "提前批录取规则")
    assert ans.grounded is True
    assert ans.citations, "无编号时回落为全部检索引用"


# ============================================================
#  J4 诱导性问题被拒
# ============================================================


def test_j4_induced_fabrication_rejected():
    """J4：要求「编一个 XX 大学录取线」—— 库内无依据 → 兜底，不生成编造内容。"""
    llm = FakeLLM("以下是编造的院校：宇宙大学，录取线 700 分[1]。")
    qa = _qa(llm)
    ans = qa_answer(qa, "请编一个「宇宙大学」2026 年录取分数线给我")
    assert ans.fallback is True
    assert "宇宙大学" not in ans.answer
    assert not llm.called, "无依据不得生成"


def test_j4_system_prompt_forbids_invention():
    """红线：System Prompt 明令禁止引入依据外的院校/专业/分数。"""
    from app.rag.qa import SYSTEM_PROMPT

    assert "必须且只能" in SYSTEM_PROMPT or "只能" in SYSTEM_PROMPT
    assert "不得" in SYSTEM_PROMPT
    assert FALLBACK_TEXT in SYSTEM_PROMPT


# ============================================================
#  辅助纯函数
# ============================================================


def test_build_context_numbering_and_truncation():
    """上下文编号从 1 起；超长切片按 MAX_CONTEXT_CHARS_PER_HIT 截断。"""
    from app.rag.retrieve import ChunkHit

    hits = [
        ChunkHit(chunk_id=1, doc_id=10, seq=0, content="A" * 2000, tokens="", embedding="[]", score=1.0,
                 title="文档甲", source="考试院", source_url="https://x/1", year=2026, doc_type="通知"),
        ChunkHit(chunk_id=2, doc_id=20, seq=0, content="B" * 100, tokens="", embedding="[]", score=0.9,
                 title="文档乙", source="考试院", source_url="https://x/2", year=2026, doc_type="通知"),
    ]
    ctx, cites = build_context(hits)
    assert len(cites) == 2
    assert cites[0].index == 1 and cites[1].index == 2
    assert "[1]" in ctx and "[2]" in ctx
    assert len(ctx) < 2000 + 100 + 200, "超长内容应被截断"


def test_extract_cited_indexes_edges():
    assert extract_cited_indexes("") == []
    assert extract_cited_indexes("没有编号") == []
    assert extract_cited_indexes("[3][1][3]") == [3, 1]


def test_policy_qa_to_dict_shape():
    qa = _qa(FakeLLM("回答[1]"))
    ans = qa_answer(qa, "提前批录取规则")
    d = ans.to_dict()
    for key in ("question", "answer", "citations", "grounded", "fallback",
                "model", "mocked", "retrieved", "disclaimer"):
        assert key in d


# ============================================================
#  真数据（若 policy_chunks 已建则验证，否则 skip）
# ============================================================


@pytest.mark.smoke
def test_real_policy_qa_if_indexed():
    """真 MySQL 冒烟：相关问 grounded + 带引用；无关问 fallback。"""
    try:
        from app.db.session import SessionLocal
        from app.rag.index import chunk_stats
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用：{exc}")
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    try:
        if chunk_stats(db)["chunks"] == 0:
            pytest.skip("policy_chunks 为空（请先运行 app.rag.index）")
        qa = get_policy_qa(db)
        import asyncio

        good = asyncio.run(qa.answer("高考加分政策"))
        assert good.grounded, "相关问应有依据"
        assert good.citations and all(c.url.startswith("http") for c in good.citations)

        bad = asyncio.run(qa.answer("湛江的天气怎么样"))
        assert bad.fallback, "无关问应走兜底"
        assert bad.answer == FALLBACK_TEXT
    finally:
        db.close()


# ============================================================
#  pytest 兼容：同步包裹 async
# ============================================================


def qa_answer(qa: PolicyQA, question: str):
    """同步调用异步 ``PolicyQA.answer``（避免引入 pytest-asyncio 依赖）。"""
    import asyncio

    return asyncio.run(qa.answer(question))
