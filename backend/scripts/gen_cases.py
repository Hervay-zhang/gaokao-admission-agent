"""T27 · 案例中心生成器（回测样本 → 脱敏真实案例）。

**红线**（任务卡 T27）
--------------------
1. 案例**必须来自真实数据**，不得虚构：本脚本把「回测样本 + 真实投档 CSV」跑一遍 T08 引擎，
   案例里的每一条院校/位次/命中判定都由数据得出；
2. **脱敏**：只输出**分数段（10 分区间）与位次段（千位区间）**，不输出明文分数/位次；
   无姓名、手机号、就读学校等任何可识别个人信息（回测样本本身也不含这些字段）；
3. **可复现**：固定样本、固定选取规则 → 同输入同输出；报告首行打印输入 sha256。

输出
----
- `data/cases/case_XX.md`：5 篇案例（基础版 4 + 深度版 1，体现报告分层字段）；
- `data/cases/index.json`：机读索引（供 `/api/cases` 筛选）。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/gen_cases.py
    cd backend && ./.venv/Scripts/python.exe scripts/gen_cases.py --count 5 --out ../data/cases
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.run_backtest import (  # noqa: E402
    ADMISSION_CSV,
    SAMPLES_CSV,
    _is_hit_answerable,
    _load_admission_rows,
    _load_university_meta,
    check_time_isolation,
    load_actual_by_year,
    load_samples,
    sha256_of,
)

PROJECT_ROOT = BACKEND_DIR.parent
CASES_DIR = PROJECT_ROOT / "data" / "cases"

PROVINCE = "广东"
TIERS = ("chong", "wen", "bao")
TIER_LABEL = {"chong": "冲", "wen": "稳", "bao": "保"}

# 选取目标：(科类, 分数下界, 分数上界, 期望路径类型)
# 覆盖 4 类路径 + 两类科类 + 高/中/低分数段（确定性：按 sample_id 升序取首个匹配）
TARGET_BANDS: list[tuple[str, int, int, str]] = [
    ("物理", 600, 751, "进取型"),    # 冲刺成功（稀缺，优先占位）
    ("物理", 400, 520, "滑档预警"),  # 保底失效（复盘价值最高）
    ("物理", 520, 620, "稳妥型"),
    ("历史", 400, 520, "保底型"),    # 无保档推荐（位次附近缺足够弱的院校）
    ("历史", 520, 751, "稳妥型"),
]


# ============================================================
# 脱敏工具
# ============================================================


def score_band(score: int) -> str:
    """分数段（10 分区间，闭区间）。"""
    lo = score // 10 * 10
    return f"{lo}-{lo + 9}"


def rank_band(rank: int) -> str:
    """位次段（千位区间），展示为「x.x–x.y 万名」。"""
    lo = rank // 1000 * 1000
    hi = lo + 999
    return f"{lo / 10000:.1f}-{hi / 10000:.1f}万名"


def path_type(bao_rec: int, bao_hit: int, chong_hit: int) -> str:
    """路径类型（由**真实结果**机械判定，不做主观编造）。

    - **滑档预警**：有保档推荐但保档未命中（保底失效）→ 复盘价值最高；
    - **进取型**：冲档有命中（事后看确有冲刺成功）；
    - **稳妥型**：保档命中（不滑档）；
    - **保底型**：位次附近未筛出保档院校（需放宽地域/类型偏好）。
    """
    if bao_rec > 0 and bao_hit == 0:
        return "滑档预警"
    if chong_hit > 0:
        return "进取型"
    if bao_rec > 0 and bao_hit > 0:
        return "稳妥型"
    return "保底型"


# ============================================================
# 主体
# ============================================================


def compute_stats(sample: dict, res, by_group, by_school, tolerance: float) -> dict:
    """由引擎结果 + 真实投档算命中/可判定，并判定路径类型。"""
    tier_stats: dict[str, dict] = {t: {"rec": 0, "hit": 0, "unanswerable": 0, "items": []} for t in TIERS}
    for g in res.tiers:
        for it in g.items:
            st = tier_stats[g.tier]
            st["rec"] += 1
            hit, answerable = _is_hit_answerable(it, sample, by_group, by_school, tolerance)
            if answerable:
                st["hit"] += 1 if hit else 0
            else:
                st["unanswerable"] += 1
            st["items"].append((it, hit, answerable))

    bao_rec, bao_hit = tier_stats["bao"]["rec"], tier_stats["bao"]["hit"]
    chong_hit = tier_stats["chong"]["hit"]
    return {
        "tier_stats": tier_stats,
        "n_reco": sum(st["rec"] for st in tier_stats.values()),
        "bao_rec": bao_rec,
        "bao_hit": bao_hit,
        "chong_hit": chong_hit,
        "bao_effective": bao_rec > 0 and bao_hit > 0,
        "path_type": path_type(bao_rec, bao_hit, chong_hit),
    }


def select_samples(samples: list[dict], stats_by_id: dict[str, dict], count: int) -> list[dict]:
    """确定性选取：按目标 (科类, 分数段, 期望路径) 顺序取首个匹配样本；逐级放宽。"""
    ordered = sorted(samples, key=lambda s: s["sample_id"])
    picked: list[dict] = []
    used: set[str] = set()

    def add(s: dict) -> None:
        if s["sample_id"] not in used:
            picked.append(s)
            used.add(s["sample_id"])

    # 第一轮：科类 + 分数段 + 期望路径
    for subj, lo, hi, want in TARGET_BANDS:
        for s in ordered:
            st = stats_by_id.get(s["sample_id"])
            if not st or s["sample_id"] in used or s["subject_type"] != subj:
                continue
            if not (lo <= s["score"] < hi) or st["path_type"] != want:
                continue
            if st["n_reco"] <= 0:
                continue
            add(s)
            break
    # 第二轮：放宽路径，只要求科类 + 分数段
    if len(picked) < count:
        for subj, lo, hi, _want in TARGET_BANDS:
            if len(picked) >= count:
                break
            for s in ordered:
                st = stats_by_id.get(s["sample_id"])
                if not st or s["sample_id"] in used or s["subject_type"] != subj:
                    continue
                if not (lo <= s["score"] < hi) or st["n_reco"] <= 0:
                    continue
                add(s)
                break
    # 第三轮：任意有推荐样本补齐
    if len(picked) < count:
        for s in ordered:
            if len(picked) >= count:
                break
            st = stats_by_id.get(s["sample_id"])
            if st and st["n_reco"] > 0:
                add(s)
    return picked[:count]


def build_case(
    sample: dict, res, stats: dict, tier_limit: int, tolerance: float, tier: str, slug: str,
) -> tuple[str, dict]:
    """由**已算好的引擎结果与统计**生成一篇案例（md 文本 + 索引元数据）。"""
    tier_stats = stats["tier_stats"]
    ptype = stats["path_type"]
    bao_effective = stats["bao_effective"]

    title = f"{sample['subject_type']}类 · {score_band(sample['score'])} 分段 · {ptype}复盘"

    md = render_case_md(
        slug=slug, title=title, sample=sample, res=res, tier_stats=tier_stats,
        bao_effective=bao_effective, ptype=ptype, tier=tier, tolerance=tolerance,
    )
    meta = {
        "slug": slug,
        "title": title,
        "subject_type": sample["subject_type"],
        "score_band": score_band(sample["score"]),
        "score_band_start": sample["score"] // 10 * 10,
        "rank_band": rank_band(sample["rank"]),
        "path_type": ptype,
        "narrative_tier": tier,
        "valid_year": sample["valid_year"],
        "train_years": sample["train_years"],
        "n_reco": stats["n_reco"],
        "bao_effective": bao_effective,
        "chong_hit": stats["chong_hit"],
        "summaries": {
            t: {"rec": tier_stats[t]["rec"], "hit": tier_stats[t]["hit"],
                "unanswerable": tier_stats[t]["unanswerable"]}
            for t in TIERS
        },
        "sample_id": sample["sample_id"],
    }
    return md, meta


def render_case_md(
    *, slug: str, title: str, sample: dict, res, tier_stats: dict, bao_effective: bool,
    ptype: str, tier: str, tolerance: float,
) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# {title}")
    a("")
    a("> 本案例由 `scripts/gen_cases.py` 从**回测样本 + 真实投档数据**自动生成（已脱敏），")
    a("> 禁止手工修改内容或编造情节。案例编号仅供索引，不含任何可识别个人信息。")
    a("")
    a("## 一、考生画像（脱敏）")
    a("")
    a("| 项 | 内容 |")
    a("|---|---|")
    a(f"| 省份 | {PROVINCE} |")
    a(f"| 科类 | {sample['subject_type']} |")
    a(f"| 分数段 | {score_band(sample['score'])} 分 |")
    a(f"| 位次段 | {rank_band(sample['rank'])} |")
    a(f"| 报考年份 | {sample['valid_year']} |")
    a(f"| 训练窗口（引擎可用年份） | {', '.join(str(y) for y in sample['train_years'])} |")
    a("| 偏好（城市/专业/预算） | 未采集（回测样本不含偏好字段） |")
    a("")
    a(f"路径类型：**{ptype}**（由事后真实结果机械判定）")
    a("")
    a("## 二、系统给出的「冲稳保」方案（回测口径）")
    a("")
    for t in TIERS:
        st = tier_stats[t]
        a(f"### {TIER_LABEL[t]}档（{st['rec']} 条）")
        a("")
        if not st["items"]:
            a("- （该档无匹配院校）")
            a("")
            continue
        for it, _hit, _ans in st["items"][:5]:
            ref = f"参考位次 {it.ref_rank}" if it.ref_rank else "参考位次缺失"
            latest = (
                f"最新年 {it.latest_year} 投档最低 {it.latest_rank} 位"
                if it.latest_year and it.latest_rank else "最新年投档数据待补"
            )
            tags = f"（{it.level_tags}）" if it.level_tags else ""
            a(f"- {it.university_name}{tags}｜专业组 {it.group_code}｜{ref}｜{latest}")
        if len(st["items"]) > 5:
            a(f"- …另有 {len(st['items']) - 5} 条，此处仅示例前 5 条")
        a("")

    a("## 三、验证年实际结果（事后复盘）")
    a("")
    a("| 档位 | 推荐 | 可对答案 | 不可对答案 | 命中 | 命中率（可判定口径） |")
    a("|---|---:|---:|---:|---:|---:|")
    for t in TIERS:
        st = tier_stats[t]
        ans = st["rec"] - st["unanswerable"]
        rate = f"{st['hit'] / ans:.0%}" if ans else "—"
        a(f"| {TIER_LABEL[t]} | {st['rec']} | {ans} | {st['unanswerable']} | {st['hit']} | {rate} |")
    a("")
    a("> **命中判据**：该院校在验证年真实投档中，存在投档位次 **≥ 考生位次**（位次数值大 = 门槛弱 = 稳进）")
    a(f"> 且与推荐项参考位次同区间（±{tolerance:.0%}）的专业组；优先精确匹配同组号，组号重排时回退院校级同区间匹配。")
    a("> **不可对答案** = 该专业组当年停招或组号重排且同区间组消失 —— 属公开数据现状，非推荐错误（已显式披露）。")
    a("")
    a(f"- 保档有效性（样本级，红线指标）：**{'命中，未滑档 ✅' if bao_effective else ('有保档推荐但未命中 ❌' if tier_stats['bao']['rec'] else '无保档推荐')}**")
    a(f"- 冲档是否有命中（事后看确有冲刺成功）：**{'是' if tier_stats['chong']['hit'] else '否'}**")
    a("")

    if tier == "deep":
        a("## 四、深度依据（逐条参考位次来源）")
        a("")
        a("> 深度版逐条展开引擎采用的**加权年份依据**（数据驱动，非 LLM 生成）。")
        a("")
        for t in TIERS:
            st = tier_stats[t]
            if not st["items"]:
                continue
            a(f"### {TIER_LABEL[t]}档")
            a("")
            for it, hit, answerable in st["items"]:
                verdict = "命中" if (answerable and hit) else ("未命中" if answerable else "不可对答案")
                a(f"- **{it.university_name}**（专业组 {it.group_code}）— 事后判定：{verdict}")
                for ev in (it.evidence or []):
                    a(f"  - {ev}")
                if it.ref_years_missing:
                    miss = ", ".join(str(y) for y in it.ref_years_missing)
                    a(f"  - ⚠️ 参考年份不足，缺 {miss}（已按缺年保守化处理）")
                if it.require_note:
                    a(f"  - ⚠️ {it.require_note}")
            a("")

    sec_no = "五" if tier == "deep" else "四"
    a(f"## {sec_no}、复盘结论（基于数据）")
    a("")
    a(f"- 该考生位次段为 {rank_band(sample['rank'])}，引擎据训练窗口 "
      f"{'、'.join(str(y) for y in res.ref_years_used)} 的投档数据加权测算参考位次。")
    if bao_effective:
        a("- 保档在验证年确实接住了该位次考生 —— 这是「不滑档」的关键保障。")
    elif tier_stats["bao"]["rec"]:
        a("- 保档在可判定条目上未命中：说明按历史规律推的保底校在该年门槛上移（位次变小），")
        a("  这正是需要「多推几所 + 服从调剂」的原因，单一保底并不保险。")
    else:
        a("- 该组合未筛出保档院校（位次附近缺少足够弱的院校），提示需要**放宽地域/类型偏好**。")
    if tier_stats["chong"]["unanswerable"] > 0:
        a(f"- 冲档有 {tier_stats['chong']['unanswerable']} 条不可对答案（专业组停招/组号重排），已显式披露。")
    a(f"- 本案例标签：路径类型 {ptype}；报告分层 {tier}。")
    a("")
    a(f"## {'六' if tier == 'deep' else '五'}、免责")
    a("")
    a("本案例基于公开投档数据自动生成，仅用于展示测算方法与复盘思路，**不构成任何填报建议**。")
    a("")
    return "\n".join(lines)


def render_index(cases: list[dict], samples_path: Path, admission_path: Path, started_at: str) -> str:
    payload = {
        "generated_by": "scripts/gen_cases.py",
        "generated_at": started_at,
        "province": PROVINCE,
        "count": len(cases),
        "inputs": {
            "samples": str(samples_path),
            "samples_sha256": sha256_of(samples_path),
            "admission": str(admission_path),
            "admission_sha256": sha256_of(admission_path),
        },
        "filters": {
            "subject_types": sorted({c["subject_type"] for c in cases}),
            "score_bands": sorted({c["score_band"] for c in cases}, reverse=True),
            "path_types": sorted({c["path_type"] for c in cases}),
            "narrative_tiers": sorted({c["narrative_tier"] for c in cases}),
        },
        "cases": cases,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从回测样本生成脱敏案例（T27）")
    ap.add_argument("--samples", type=Path, default=SAMPLES_CSV)
    ap.add_argument("--admission", type=Path, default=ADMISSION_CSV)
    ap.add_argument("--out", type=Path, default=CASES_DIR)
    ap.add_argument("--count", type=int, default=5, help="案例数量（任务卡要求 3–5）")
    ap.add_argument("--tier-limit", type=int, default=None)
    ap.add_argument("--tolerance", type=float, default=0.15)
    args = ap.parse_args(argv)

    from app.core.config import settings
    from app.services.recommend import InMemoryRecommendRepository, RecommendEngine

    if not args.samples.exists():
        print(f"[失败] 样本不存在：{args.samples}", file=sys.stderr)
        return 2
    if not 3 <= args.count <= 5:
        print(f"[失败] count 需在 3–5（任务卡要求），收到 {args.count}", file=sys.stderr)
        return 2

    samples = load_samples(args.samples)
    leaks = check_time_isolation(samples)
    if leaks:
        print("[失败] 时间泄漏，中止（红线）：", file=sys.stderr)
        for line in leaks[:10]:
            print(f"  - {line}", file=sys.stderr)
        return 1

    tier_limit = args.tier_limit or settings.tier_limit
    rows = _load_admission_rows(args.admission)
    engine = RecommendEngine(InMemoryRecommendRepository(rows, _load_university_meta()))
    by_group, by_school = load_actual_by_year(args.admission)

    # 单次跑全部样本，缓存 引擎结果 + 命中统计（供选取与生成复用，避免重复计算）
    results: dict[str, object] = {}
    stats_by_id: dict[str, dict] = {}
    for s in samples:
        res = engine.recommend(
            score=s["score"], subject_type=s["subject_type"], province=PROVINCE,
            rank=s["rank"], current_year=s["valid_year"], ref_years=s["train_years"],
            tier_limit=tier_limit,
        )
        results[s["sample_id"]] = res
        stats_by_id[s["sample_id"]] = compute_stats(s, res, by_group, by_school, args.tolerance)

    picked = select_samples(samples, stats_by_id, args.count)
    if len(picked) < 3:
        print(f"[失败] 仅选出 {len(picked)} 个样本（需 ≥3），请检查样本文件", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    # 清掉旧的 case_*.md（保持与本次生成一致，避免残留陈旧案例）
    for old in args.out.glob("case_*.md"):
        old.unlink()

    metas: list[dict] = []
    for idx, s in enumerate(picked):
        tier = "deep" if idx == len(picked) - 1 else "basic"  # 末篇为深度版（体现报告分层）
        slug = f"case_{idx + 1:02d}"
        md, meta = build_case(
            s, results[s["sample_id"]], stats_by_id[s["sample_id"]], tier_limit, args.tolerance, tier, slug
        )
        (args.out / f"{meta['slug']}.md").write_text(md, encoding="utf-8")
        metas.append(meta)

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (args.out / "index.json").write_text(
        render_index(metas, args.samples, args.admission, started_at), encoding="utf-8"
    )

    print(f"[完成] 生成 {len(metas)} 篇案例 → {args.out}")
    for m in metas:
        print(f"  - {m['slug']}: {m['title']}｜保档{'✓' if m['bao_effective'] else '✗'}｜{m['narrative_tier']}")
    print(f"[输入指纹] samples={sha256_of(args.samples)} admission={sha256_of(args.admission)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
