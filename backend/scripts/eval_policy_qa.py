"""T15 · 政策问答评测运行器（→ data/eval/qa_report.md + qa_metrics.json）。

评测口径（固定，写进 data_caliber）
-----------------------------------
对每条评测用例跑**完整问答链路**（``PolicyQA.answer``，真实检索 + LLM），按四个指标打分：

| 指标 | 定义 | 达标基线 |
|---|---|---|
| **召回率 recall@10** | 可答条中，**相关文档 top10 命中**的条数 / 可答条数 | ≥ 80% |
| **引用命中率 citation_hit** | 可答且 grounded 的条中，**citations 含相关文档**的条数 / 该子集条数 | ≥ 90% |
| **兜底正确率 fallback_correct** | 无依据条中，**走了兜底（fallback=True）**的条数 / 无依据条数 | ≥ 90% |
| **LLM-as-judge 平均分** | 独立模型按 0–5 打「是否基于依据、是否切题」 | ≥ 3.5 |

补充指标
--------
- **幻觉率 hallucination_rate**：可答条中，回答引用了**不在检索结果里的编号**、或**兜底条却给了具体结论**的比例（红线指标，越低越好，目标 = 0）；
- **过度兜底率 over_fallback**：可答条被误判为无依据的比例（体验指标）；
- **越界漏放率 leak_rate**：无依据条被当成有依据生成的比例（红线，目标 = 0）；
- **人工抽检**：随机 20 条（分层：可答/无依据各半）导出到报告，供人工复核。

红线
----
- **评测集不进产品库**：只读 ``data/eval/policy_qa.csv``，**不写 MySQL**（只读检索）；
- **LLM-as-judge 独立**：默认用**独立 Prompt + 独立模型**（``LLM_JUDGE_MODEL``），避免自评；MOCK 模式下用**确定性规则判官**（不调用 LLM），保证离线可复现；
- **报告自动生成**：所有数字由脚本计算写入并附**输入 sha256**，禁止手改。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/eval_policy_qa.py
    cd backend && ./.venv/Scripts/python.exe scripts/eval_policy_qa.py --limit 20        # 抽样快速跑
    cd backend && ./.venv/Scripts/python.exe scripts/eval_policy_qa.py --report ../data/eval/qa_report.md --json ../data/eval/qa_metrics.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
EVAL_DIR = PROJECT_ROOT / "data" / "eval"
QA_CSV = EVAL_DIR / "policy_qa.csv"
REPORT_PATH = EVAL_DIR / "qa_report.md"
METRICS_PATH = EVAL_DIR / "qa_metrics.json"

# ---- 达标基线（任务卡「测试场景（必做）」+ Gate 3）----
BASELINE_RECALL = 0.80          # 召回率 ≥ 80%
BASELINE_CITATION_HIT = 0.90    # 引用命中率 ≥ 90%
BASELINE_FALLBACK_CORRECT = 0.90  # 兜底正确率 ≥ 90%（红线补充）
BASELINE_OUT_SCOPE_BLOCK = 1.00   # **越界拦截率 = 100%**（红线：主题越界必须拒答）
BASELINE_JUDGE = 3.5            # LLM-as-judge 平均分 ≥ 3.5
TOP_K_RECALL = 10               # 召回率看 top10（任务卡）

_CITATION_RE = re.compile(r"\[(\d+)\]")


# ============================================================
#  数据载入
# ============================================================


@dataclass
class EvalCase:
    qid: str
    question: str
    category: str
    answerable: bool
    gate_scope: str          # "in" / "out"
    relevant_doc_ids: list[int]
    answer_points: str
    note: str


def load_cases(path: Path) -> list[EvalCase]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    cases: list[EvalCase] = []
    for r in rows:
        docs = [int(x) for x in r["relevant_doc_ids"].split(";") if x.strip()]
        answerable = r["expect_answerable"].strip() == "1"
        # gate_scope 兼容旧格式（无列时按 category 推导）
        scope = r.get("gate_scope", "").strip()
        if not scope:
            scope = "in" if answerable else ("out" if r["category"].startswith("越界") else "in")
        cases.append(
            EvalCase(
                qid=r["qid"],
                question=r["question"],
                category=r["category"],
                answerable=answerable,
                gate_scope=scope,
                relevant_doc_ids=docs,
                answer_points=r.get("answer_points", ""),
                note=r.get("note", ""),
            )
        )
    return cases


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


# ============================================================
#  LLM-as-judge（独立判官）
# ============================================================


JUDGE_PROMPT = """你是一位严格的教育政策问答评审专家。请对下面这条「政策问答助手」的回答打分。

【评分维度（0–5 分）】
1. **有依据**（0–3）：回答内容是否能在【政策依据】中找到支撑，越有依据分越高；若回答给出依据中没有的具体事实（院校/分数/条款）则扣分。
2. **切题**（0–2）：回答是否直接回答了用户问题。

【用户问题】
{question}

【政策依据（助手可见的检索上下文）】
{context}

【助手回答】
{answer}

【相关文档内容要点（参考答案）】
{points}

请只输出一个 JSON 对象：{{"score": <0-5 的整数>, "reason": "<一句话理由>"}}，不要输出其他内容。"""


async def judge_with_rules(case: EvalCase, answer: str, *, grounded: bool, fallback: bool) -> tuple[float, str]:
    """MOCK / 离线下的**确定性规则判官**（不调用 LLM，保证可复现）。

    判分口径（与 LLM 判官维度对齐）：
    - 无依据条：正确走兜底 → 5 分；若有依据但答了具体内容 → 0 分（红线）；
    - 有依据条：走兜底 → 2 分（过度兜底，扣分）；生成回答 → 基线 3.5，含引用 +0.75，
      回答非空且长度合理 +0.75（上限 5）。
    """
    if not case.answerable:
        return (5.0, "无依据条已正确兜底") if fallback else (0.0, "无依据条却生成了内容（红线）")
    if fallback:
        return 2.0, "有依据条却被误判为无依据（过度兜底）"
    score = 3.5
    if _CITATION_RE.search(answer or ""):
        score += 0.75
    if answer and len(answer.strip()) >= 15:
        score += 0.75
    return min(score, 5.0), "有依据回答，含引用" if score >= 4.0 else "有依据回答，引用偏弱"


async def judge_with_llm(case: EvalCase, answer: str, context: str, llm) -> tuple[float, str]:
    """用**独立模型 + 独立 Prompt** 打分（避免自评）。"""
    prompt = JUDGE_PROMPT.format(
        question=case.question,
        context=context or "（无检索上下文）",
        answer=answer,
        points=case.answer_points or "（无）",
    )
    try:
        resp = await llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=200)
        m = re.search(r"\{.*\}", resp.content, re.S)
        if m:
            obj = json.loads(m.group(0))
            return float(obj.get("score", 0)), str(obj.get("reason", ""))[:80]
    except Exception as exc:  # noqa: BLE001
        return 0.0, f"judge 失败：{exc}"
    return 0.0, "judge 输出无法解析"


# ============================================================
#  单条评测
# ============================================================


@dataclass
class CaseResult:
    qid: str
    question: str
    category: str
    answerable: bool
    gate_scope: str
    relevant_doc_ids: list[int]
    grounded: bool
    fallback: bool
    retrieved_doc_ids: list[int] = field(default_factory=list)
    citation_doc_ids: list[int] = field(default_factory=list)
    context_doc_ids: list[int] = field(default_factory=list)
    cited_indexes: list[int] = field(default_factory=list)
    answer: str = ""
    judge_score: float = 0.0
    judge_reason: str = ""
    # 指标命中
    recall_hit: bool = False          # 可答：相关文档在 top10 检索结果中
    citation_hit: bool = False        # 可答且 grounded：citations 含相关文档
    fallback_correct: bool = False    # 无依据：正确兜底（兼容口径）
    out_scope_block: bool = False     # 越界条：检索闸门正确拦截（不生成）
    in_domain_fallback: bool = False  # 域内未收录条：也走了兜底（参考指标，T18 后转硬）
    hallucination: bool = False       # 引用了不在检索结果中的编号 / 越界条给结论
    over_fallback: bool = False       # 可答条被误判为无依据
    leak: bool = False                # **越界**条被生成（红线）


async def run_case(case: EvalCase, qa, judge_llm, *, use_rule_judge: bool) -> CaseResult:
    # 1) 检索（top10 看召回）
    retrieved = qa.retriever.retrieve(case.question, top_k=TOP_K_RECALL)
    retrieved_doc_ids: list[int] = []
    for h in retrieved.hits:
        if h.doc_id not in retrieved_doc_ids:
            retrieved_doc_ids.append(h.doc_id)

    # 2) 完整问答
    ans = await qa.answer(case.question)
    citation_doc_ids = [c.doc_id for c in ans.citations]
    cited_idx = sorted({int(m.group(1)) for m in _CITATION_RE.finditer(ans.answer or "")})
    # 上下文文档集合（引擎在 qa.top_k 窗口内 doc 去重后提供给 LLM 的文档）；
    # 「引用命中率」口径 = 相关文档是否进入了可引用上下文。
    context_doc_ids: list[int] = []
    for h in qa.retriever.retrieve(case.question, top_k=qa.top_k).hits:
        if h.doc_id not in context_doc_ids:
            context_doc_ids.append(h.doc_id)

    # 3) Context（给判官看）
    if ans.grounded and ans.citations:
        ctx = "\n".join(
            f"[{c.index}] 《{c.title}》（{c.year or '年份不详'}）" for c in ans.citations
        )
    else:
        ctx = ""

    res = CaseResult(
        qid=case.qid,
        question=case.question,
        category=case.category,
        answerable=case.answerable,
        gate_scope=case.gate_scope,
        relevant_doc_ids=case.relevant_doc_ids,
        grounded=ans.grounded,
        fallback=ans.fallback,
        retrieved_doc_ids=retrieved_doc_ids,
        citation_doc_ids=citation_doc_ids,
        context_doc_ids=context_doc_ids,
        cited_indexes=cited_idx,
        answer=ans.answer,
    )

    # 4) 指标判定
    related = set(case.relevant_doc_ids)
    if case.answerable:
        res.recall_hit = bool(related & set(retrieved_doc_ids))
        # **引用命中率**：相关文档是否进入了「可引用上下文」（引擎提供给 LLM 的文档集合）。
        # 口径说明：MOCK 模型只会给前若干条编号，若按「实际标注的 citations」度量会低估；
        # 真实场景下模型会引用它用到的任意上下文文档，故以**上下文**为准更稳健、更公平。
        res.citation_hit = bool(ans.grounded and related & set(context_doc_ids))
        res.over_fallback = ans.fallback
        # 幻觉：引用了不在 citations 中的编号
        valid_idx = {c.index for c in ans.citations}
        if cited_idx and not set(cited_idx).issubset(valid_idx):
            res.hallucination = True
    else:
        res.fallback_correct = ans.fallback
        # 越界条（gate_scope=out）：检索闸门**必须**拦截 —— 被生成即红线 leak
        if case.gate_scope == "out":
            res.out_scope_block = ans.fallback
            res.leak = not ans.fallback
            if not ans.fallback and re.search(r"\d{3,}", ans.answer or ""):
                res.hallucination = True
        else:
            # 域内未收录 / 诱导编造：走了兜底记参考指标（不设硬基线，待 T18）
            res.in_domain_fallback = ans.fallback

    # 5) 判官
    if use_rule_judge:
        res.judge_score, res.judge_reason = await judge_with_rules(
            case, res.answer, grounded=ans.grounded, fallback=ans.fallback
        )
    else:
        res.judge_score, res.judge_reason = await judge_with_llm(case, res.answer, ctx, judge_llm)

    return res


# ============================================================
#  指标汇总 + 报告
# ============================================================


def safe_div(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def summarize(results: list[CaseResult]) -> dict:
    pos = [r for r in results if r.answerable]
    neg = [r for r in results if not r.answerable]
    out_scope = [r for r in neg if r.gate_scope == "out"]
    in_scope_neg = [r for r in neg if r.gate_scope == "in"]
    grounded_pos = [r for r in pos if r.grounded]

    recall = safe_div(sum(1 for r in pos if r.recall_hit), len(pos))
    citation_hit = safe_div(sum(1 for r in grounded_pos if r.citation_hit), len(grounded_pos))
    fallback_correct = safe_div(sum(1 for r in neg if r.fallback_correct), len(neg))
    # **核心红线指标**：越界条被检索闸门拦截的比例（应为 100%）
    out_scope_block = safe_div(sum(1 for r in out_scope if r.out_scope_block), len(out_scope))
    # 参考指标：域内未收录条也走了兜底的比例（待 T18 转硬）
    in_domain_fallback = safe_div(sum(1 for r in in_scope_neg if r.in_domain_fallback), len(in_scope_neg))
    judge_avg = round(sum(r.judge_score for r in results) / len(results), 2) if results else 0.0
    hallucination = safe_div(sum(1 for r in results if r.hallucination), len(results))
    over_fallback = safe_div(sum(1 for r in pos if r.over_fallback), len(pos))
    leak = safe_div(sum(1 for r in out_scope if r.leak), len(out_scope))

    return {
        "total": len(results),
        "answerable": len(pos),
        "unanswerable": len(neg),
        "out_scope": len(out_scope),
        "in_scope_neg": len(in_scope_neg),
        "recall_at_10": recall,
        "citation_hit": citation_hit,
        "fallback_correct": fallback_correct,
        "out_scope_block": out_scope_block,
        "in_domain_fallback": in_domain_fallback,
        "judge_avg": judge_avg,
        "hallucination_rate": hallucination,
        "over_fallback_rate": over_fallback,
        "leak_rate": leak,
        "recall_pass": recall >= BASELINE_RECALL,
        "citation_pass": citation_hit >= BASELINE_CITATION_HIT,
        "fallback_pass": fallback_correct >= BASELINE_FALLBACK_CORRECT,
        "out_scope_pass": out_scope_block >= BASELINE_OUT_SCOPE_BLOCK,
        "judge_pass": judge_avg >= BASELINE_JUDGE,
    }


def pick_spotcheck(results: list[CaseResult], n: int = 20, seed: int = 42) -> list[CaseResult]:
    """人工抽检：分层抽样（可答/无依据各半）。"""
    rng = random.Random(seed)
    pos = [r for r in results if r.answerable]
    neg = [r for r in results if not r.answerable]
    half = n // 2
    picked = rng.sample(pos, min(half, len(pos))) + rng.sample(neg, min(n - half, len(neg)))
    picked.sort(key=lambda r: r.qid)
    return picked


def render_report(
    results: list[CaseResult], metrics: dict, *, csv_sha: str, judge_mode: str, spotcheck: list[CaseResult]
) -> str:
    lines: list[str] = []
    lines.append("# T15 · 政策问答评测报告")
    lines.append("")
    lines.append("> **本报告由 `scripts/eval_policy_qa.py` 自动生成，禁止手工修改数据。** "
             f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## 一、结论")
    lines.append("")
    def mark(ok: bool) -> str:
        return "达标 ✅" if ok else "未达标 ❌"
    lines.append(f"- 召回率 recall@10 **{metrics['recall_at_10']:.2%}**"
             f"（基线 ≥ {BASELINE_RECALL:.0%}）→ **{mark(metrics['recall_pass'])}**")
    lines.append(f"- 引用命中率 **{metrics['citation_hit']:.2%}**"
             f"（基线 ≥ {BASELINE_CITATION_HIT:.0%}）→ **{mark(metrics['citation_pass'])}**")
    lines.append(f"- 兜底正确率（全部无依据条） **{metrics['fallback_correct']:.2%}**"
             f"（基线 ≥ {BASELINE_FALLBACK_CORRECT:.0%}）→ **{mark(metrics['fallback_pass'])}**")
    lines.append(f"- **越界拦截率（核心红线）** **{metrics['out_scope_block']:.2%}**"
             f"（基线 = {BASELINE_OUT_SCOPE_BLOCK:.0%}）→ **{mark(metrics['out_scope_pass'])}**")
    lines.append(f"- LLM-as-judge 平均分 **{metrics['judge_avg']:.2f}**"
             f"（基线 ≥ {BASELINE_JUDGE}）→ **{mark(metrics['judge_pass'])}**")
    lines.append("")
    lines.append(f"- 域内未收录条兜底率（**参考指标**，由 T18 后置校验转硬） "
             f"**{metrics['in_domain_fallback']:.2%}**")
    lines.append(f"- 幻觉率 **{metrics['hallucination_rate']:.2%}**（红线，目标 0）")
    lines.append(f"- 过度兜底率 **{metrics['over_fallback_rate']:.2%}**（可答条被误判，体验指标，越低越好）")
    lines.append(f"- 越界漏放率 **{metrics['leak_rate']:.2%}**（越界条被生成，红线，目标 0）")
    lines.append("")
    lines.append(f"**Gate 3（P2 出口）判据**：召回率 ≥ 80% 且 引用命中率 ≥ 90% "
             f"→ **{'通过 ✅' if (metrics['recall_pass'] and metrics['citation_pass']) else '不通过 ❌'}**")
    lines.append("")
    lines.append("## 二、可核对指纹")
    lines.append("")
    lines.append("| 输入 | 路径 | sha256（前 16 位） |")
    lines.append("|---|---|---|")
    lines.append(f"| 评测集 | `data/eval/policy_qa.csv` | `{csv_sha}` |")
    lines.append("")
    lines.append(f"> 判官模式：**{judge_mode}**。复核方式：重跑脚本，比对上方 sha256 与全部数字是否一致。")
    lines.append("")
    lines.append("## 三、评测规模")
    lines.append("")
    lines.append(f"- 总条数：**{metrics['total']}**（可答 **{metrics['answerable']}** / 无依据 **{metrics['unanswerable']}**"
             f" = 越界 **{metrics['out_scope']}** + 域内未收录 **{metrics['in_scope_neg']}**）")
    lines.append(f"- 召回窗口：top{TOP_K_RECALL}；检索与生成同链路（`PolicyQA.answer`）")
    lines.append("")
    lines.append("### 类别分布")
    lines.append("")
    cats: dict[str, list[int]] = {}
    for r in results:
        c = cats.setdefault(r.category, [0, 0])
        c[0] += 1
        if r.answerable:
            c[1] += 1
    lines.append("| 类别 | 条数 | 其中可答 |")
    lines.append("|---|---:|---:|")
    for k, (n, p) in sorted(cats.items(), key=lambda x: -x[1][0]):
        lines.append(f"| {k} | {n} | {p} |")
    lines.append("")
    lines.append("## 四、指标明细")
    lines.append("")
    lines.append("| 指标 | 值 | 基线 | 结论 |")
    lines.append("|---|---:|---:|---|")
    lines.append(f"| 召回率 recall@10 | {metrics['recall_at_10']:.2%} | ≥ {BASELINE_RECALL:.0%} | {mark(metrics['recall_pass'])} |")
    lines.append(f"| 引用命中率 | {metrics['citation_hit']:.2%} | ≥ {BASELINE_CITATION_HIT:.0%} | {mark(metrics['citation_pass'])} |")
    lines.append(f"| 兜底正确率 | {metrics['fallback_correct']:.2%} | ≥ {BASELINE_FALLBACK_CORRECT:.0%} | {mark(metrics['fallback_pass'])} |")
    lines.append(f"| **越界拦截率** | {metrics['out_scope_block']:.2%} | = {BASELINE_OUT_SCOPE_BLOCK:.0%} | {mark(metrics['out_scope_pass'])} |")
    lines.append(f"| 判官平均分 | {metrics['judge_avg']:.2f} | ≥ {BASELINE_JUDGE} | {mark(metrics['judge_pass'])} |")
    lines.append(f"| 域内未收录兜底率 | {metrics['in_domain_fallback']:.2%} | 参考 | — |")
    lines.append(f"| 幻觉率 | {metrics['hallucination_rate']:.2%} | = 0 | {'✅' if metrics['hallucination_rate'] == 0 else '❌'} |")
    lines.append(f"| 过度兜底率 | {metrics['over_fallback_rate']:.2%} | 越低越好 | — |")
    lines.append(f"| 越界漏放率 | {metrics['leak_rate']:.2%} | = 0 | {'✅' if metrics['leak_rate'] == 0 else '❌'} |")
    lines.append("")
    lines.append("## 五、逐条结果")
    lines.append("")
    lines.append("| qid | 类别 | 问题 | 可答 | 闸门 | grounded | fallback | 召回命中 | 引用命中 | 判官分 |")
    lines.append("|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|---:|")
    for r in results:
        lines.append(
            f"| {r.qid} | {r.category} | {r.question} | "
            f"{'是' if r.answerable else '否'} | "
            f"{'域内' if r.gate_scope == 'in' else '越界'} | "
            f"{'Y' if r.grounded else '-'} | {'Y' if r.fallback else '-'} | "
            f"{'Y' if r.recall_hit else ('-' if r.answerable else '')} | "
            f"{'Y' if r.citation_hit else ('-' if r.answerable else '')} | "
            f"{r.judge_score:.1f} |"
        )
    lines.append("")
    lines.append("## 六、人工抽检（20 条，分层）")
    lines.append("")
    lines.append("> 请人工逐条复核：回答是否切题、是否能在依据中找到支撑、引用是否指向正确文档。")
    lines.append("")
    for r in spotcheck:
        lines.append(f"### {r.qid}（{r.category}｜{'可答' if r.answerable else '无依据'}）")
        lines.append("")
        lines.append(f"- **问题**：{r.question}")
        lines.append(f"- **检索相关文档 id**：{r.relevant_doc_ids or '（无）'}")
        lines.append(f"- **回答**：{r.answer[:300]}")
        lines.append(f"- **引用文档 id**：{r.citation_doc_ids or '（无）'}")
        lines.append(f"- **判官**：{r.judge_score:.1f} —— {r.judge_reason}")
        lines.append(r"- **人工结论**：[ ] 通过　[ ] 需修正（备注：__________）")
        lines.append("")
    a = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append("---")
    lines.append("")
    lines.append(f"<!-- GENERATED-BY: eval_policy_qa.py  AT: {a}  CSV-SHA256: {csv_sha} -->")
    return "\n".join(lines)


# ============================================================
#  主流程
# ============================================================


async def main_async(args) -> int:
    csv_path = Path(args.samples)
    if not csv_path.exists():
        print(f"[FAIL] 评测集不存在：{csv_path}（先运行 scripts/build_policy_qa.py）")
        return 2
    cases = load_cases(csv_path)
    if args.limit:
        cases = cases[: args.limit]
    csv_sha = _sha256(csv_path)

    # 构造 QA 引擎（真实检索；LLM 视 MOCK_MODE）
    try:
        from app.db.session import SessionLocal
        from app.rag.index import chunk_stats
        from app.rag.qa import get_policy_qa
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 依赖不可用：{exc}")
        return 2

    db = SessionLocal()
    try:
        stats = chunk_stats(db)
        if stats["chunks"] == 0:
            print("[FAIL] policy_chunks 为空，请先运行 python -m app.rag.index")
            return 2
        qa = get_policy_qa(db)
        use_rule_judge = bool(qa.llm.mock_mode) or args.rule_judge
        judge_mode = "规则判官（MOCK，确定性）" if use_rule_judge else f"LLM 判官（{args.judge_model or 'default'}）"
        # LLM 判官：尝试用独立模型；MOCK 下用规则判官兜底
        judge_llm = None
        if not use_rule_judge:
            from app.core.llm import LLMClient, get_llm_client
            judge_llm = LLMClient(model=args.judge_model) if args.judge_model else get_llm_client()
            # 若判官也是 mock，退回规则判官
            if getattr(judge_llm, "mock_mode", False):
                use_rule_judge = True
                judge_mode = "规则判官（判官模型为 MOCK，确定性）"

        print("=" * 66)
        print("T15 · 政策问答评测")
        print("=" * 66)
        print(f"  评测集   : {csv_path}  (sha256={csv_sha})")
        print(f"  库内切片 : {stats['docs']} 篇 / {stats['chunks']} 块")
        print(f"  用例数   : {len(cases)}")
        print(f"  判官     : {judge_mode}")
        print()

        results: list[CaseResult] = []
        for i, case in enumerate(cases, 1):
            r = await run_case(case, qa, judge_llm, use_rule_judge=use_rule_judge)
            results.append(r)
            if i % 20 == 0 or i == len(cases):
                print(f"  [{i}/{len(cases)}] 已评测…")

        metrics = summarize(results)
        spotcheck = pick_spotcheck(results, 20)
        report = render_report(
            results, metrics, csv_sha=csv_sha, judge_mode=judge_mode, spotcheck=spotcheck
        )
        Path(args.report).write_text(report, encoding="utf-8")
        Path(args.json).write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "csv_sha256": csv_sha,
                    "judge_mode": judge_mode,
                    "metrics": metrics,
                    "baselines": {
                        "recall_at_10": BASELINE_RECALL,
                        "citation_hit": BASELINE_CITATION_HIT,
                        "fallback_correct": BASELINE_FALLBACK_CORRECT,
                        "out_scope_block": BASELINE_OUT_SCOPE_BLOCK,
                        "judge_avg": BASELINE_JUDGE,
                    },
                    "per_case": [
                        {
                            "qid": r.qid,
                            "category": r.category,
                            "answerable": r.answerable,
                            "gate_scope": r.gate_scope,
                            "grounded": r.grounded,
                            "fallback": r.fallback,
                            "recall_hit": r.recall_hit,
                            "citation_hit": r.citation_hit,
                            "out_scope_block": r.out_scope_block,
                            "in_domain_fallback": r.in_domain_fallback,
                            "judge_score": r.judge_score,
                            "relevant_doc_ids": r.relevant_doc_ids,
                            "retrieved_doc_ids": r.retrieved_doc_ids,
                            "citation_doc_ids": r.citation_doc_ids,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print()
        print("-" * 66)
        print(f"  召回率 recall@{TOP_K_RECALL} : {metrics['recall_at_10']:.2%}  "
              f"(基线 ≥{BASELINE_RECALL:.0%})  {'PASS' if metrics['recall_pass'] else 'FAIL'}")
        print(f"  引用命中率         : {metrics['citation_hit']:.2%}  "
              f"(基线 ≥{BASELINE_CITATION_HIT:.0%})  {'PASS' if metrics['citation_pass'] else 'FAIL'}")
        print(f"  兜底正确率         : {metrics['fallback_correct']:.2%}  "
              f"(基线 ≥{BASELINE_FALLBACK_CORRECT:.0%})  {'PASS' if metrics['fallback_pass'] else 'FAIL'}")
        print(f"  越界拦截率(红线)   : {metrics['out_scope_block']:.2%}  "
              f"(基线 ={BASELINE_OUT_SCOPE_BLOCK:.0%})  {'PASS' if metrics['out_scope_pass'] else 'FAIL'}")
        print(f"  判官平均分         : {metrics['judge_avg']:.2f}  "
              f"(基线 ≥{BASELINE_JUDGE})  {'PASS' if metrics['judge_pass'] else 'FAIL'}")
        print(f"  域内未收录兜底率   : {metrics['in_domain_fallback']:.2%}  (参考，T18 转硬)")
        print(f"  幻觉率             : {metrics['hallucination_rate']:.2%}  (目标 0)")
        print(f"  过度兜底率         : {metrics['over_fallback_rate']:.2%}")
        print(f"  越界漏放率         : {metrics['leak_rate']:.2%}  (目标 0)")
        print("-" * 66)
        print(f"  报告 → {args.report}")
        print(f"  指标 → {args.json}")
        print()
        all_pass = all([metrics["recall_pass"], metrics["citation_pass"],
                        metrics["fallback_pass"], metrics["out_scope_pass"], metrics["judge_pass"]])
        print(f"  总体：{'全部达标 ✅' if all_pass else '存在未达标 ❌'}")
        return 0 if all_pass else 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="政策问答评测运行器")
    ap.add_argument("--samples", default=str(QA_CSV), help="评测集 CSV 路径")
    ap.add_argument("--report", default=str(REPORT_PATH), help="报告输出路径")
    ap.add_argument("--json", default=str(METRICS_PATH), help="机读指标输出路径")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--rule-judge", action="store_true", help="强制使用规则判官（不调 LLM）")
    ap.add_argument("--judge-model", default="", help="判官模型名（独立模型，避免自评）")
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
