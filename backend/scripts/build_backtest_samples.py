"""T10 · 回测样本生成（200 个虚拟考生）。

设计要点
--------
**1. 按真实分布分层抽样（不是均匀随机）**

直接 `random.score` 均匀采样会得到大量「无对应院校」的极端分数（如 200 分），
评测集失去区分度。改为：先算每年科类的**位次分位点**，在分位空间上分层抽样，
再反查分数 —— 这样样本按「考生实际分布密度」铺开，且**优先落在有院校覆盖的位次区间**。

**2. 时间隔离（红线：评测集与训练数据严格时间隔离）**

- **训练窗口** = 样本的「考试年」**之前**的年份（`train_years`）—— 引擎只能用这些年测算；
- **验证年** = 样本的「考试年」（`valid_year`）—— 用该年**实际投档结果**对答案，**绝不进训练**；
- 本脚本按 `valid_year ∈ {2025, 2026}` 生成：
  - `valid_year=2025` → `train_years=[2024]`（1 年）
  - `valid_year=2026` → `train_years=[2024, 2025]`（2 年）
- **只生成「训练年份 ≥ 1」的样本**；`valid_year=2024` 无任何历史年可训练，**不生成**（否则就是拿答案当输入）。

**3. 可复现**

固定 `--seed`（默认 `gaokao-t10-backtest`），同种子同参数 → **字节级一致**的 CSV。
抽样用 `random.Random(seed)` 独立实例，**不污染全局随机态**。

**4. 科类均衡**

物理 / 历史各占一半（200 → 每科类 100），避免「物理占七成」导致历史类指标被淹没。

用法
----
    cd backend && ./.venv/Scripts/python.exe scripts/build_backtest_samples.py
    cd backend && ./.venv/Scripts/python.exe scripts/build_backtest_samples.py --n 200 --seed gaokao-t10-backtest --force

输出：`data/eval/backtest_samples.csv`
字段：`sample_id, subject_type, valid_year, train_years, score, rank, strata`
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
CLEANED_DIR = PROJECT_ROOT / "data" / "cleaned"
EVAL_DIR = PROJECT_ROOT / "data" / "eval"

SCORE_CSV = CLEANED_DIR / "score_segments.csv"
SAMPLES_CSV = EVAL_DIR / "backtest_samples.csv"

DEFAULT_SEED = "gaokao-t10-backtest"
DEFAULT_N = 200
# 生成样本的「考试年」候选（必须有 ≥1 个训练年，故不含最早年 2024）
DEFAULT_VALID_YEARS = (2025, 2026)
# 每个 (valid_year, subject) 内的位次分层数（分位空间等分）
STRATA_PER_GROUP = 10
# 抽样位次下界：跳过「顶尖极少数」区间（投档线样本极少，指标噪声大）
MIN_RANK_FLOOR = 20


# ============================================================
# 载入
# ============================================================


def load_segments(path: Path) -> dict[tuple[int, str], list[tuple[int, int]]]:
    """读一分一段表，返回 ``{(year, subject): [(score, rank), ...]}``（按分数降序）。"""
    groups: dict[tuple[int, str], list[tuple[int, int]]] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for rec in csv.DictReader(f):
            key = (int(rec["year"]), rec["subject_type"])
            groups.setdefault(key, []).append((int(rec["score"]), int(rec["rank"])))
    for k in groups:
        groups[k].sort(key=lambda t: t[0], reverse=True)
    return groups


def available_train_years(valid_year: int, all_years: list[int]) -> list[int]:
    """训练窗口 = 所有**严格早于** ``valid_year`` 的年份（从旧到新）。"""
    return sorted(y for y in all_years if y < valid_year)


# ============================================================
# 位次 → 分数（独立实现，不复用 T07，避免「用被测代码造测试数据」）
# ============================================================


def score_at_rank(segs: list[tuple[int, int]], rank: int) -> int:
    """在一分一段表内取 ``rank`` 落在的分数（取**区间上沿**，即更高分那一端）。

    刻意与 T07 的插值实现**不同**（T07 是线性插值）：样本生成只须给出一个
    「该位次对应的可用分数」，用上沿即可，且能独立自证。
    """
    hi_score, hi_rank = segs[0]
    if rank <= hi_rank:
        return hi_score
    for i in range(len(segs) - 1):
        s_hi, r_hi = segs[i]
        s_lo, r_lo = segs[i + 1]
        if r_hi <= rank <= r_lo:
            return s_hi
    return segs[-1][0]


def rank_quantiles(segs: list[tuple[int, int]], n: int) -> list[int]:
    """在 ``[MIN_RANK_FLOOR, max_rank]`` 上等分取 ``n+1`` 个位次分位点（升序）。"""
    max_rank = segs[-1][1]
    lo = min(MIN_RANK_FLOOR, max_rank)
    if max_rank <= lo:
        return [lo]
    step = (max_rank - lo) / n
    return [int(lo + step * i) for i in range(n + 1)]


# ============================================================
# 主流程
# ============================================================


def build_samples(
    segments: dict[tuple[int, str], list[tuple[int, int]]],
    *,
    n: int = DEFAULT_N,
    seed: str = DEFAULT_SEED,
    valid_years: tuple[int, ...] = DEFAULT_VALID_YEARS,
) -> list[dict]:
    """生成回测样本（确定性：同 seed + 同数据 → 同结果）。"""
    all_years = sorted({y for y, _ in segments})
    subjects = ("物理", "历史")
    per_subject = n // len(subjects)

    rng = random.Random(seed)

    # 先按 (valid_year, subject) 分组摊样本数；按 valid_year 从小到大分配余数
    combos: list[tuple[int, str]] = []
    for vy in valid_years:
        for s in subjects:
            if (vy, s) not in segments:
                continue
            if not available_train_years(vy, all_years):
                continue
            combos.append((vy, s))
    if not combos:
        raise SystemExit(
            "无可用 (valid_year, subject) 组合：需要至少一个「早于 valid_year」的年份作为训练窗口。"
            f"现有年份 {all_years}，请求 valid_years={list(valid_years)}"
        )

    per_combo = per_subject // len(valid_years) if len(valid_years) else per_subject
    # 剩余名额按 combos 顺序补齐（每科类分别补齐到 per_subject）
    quota: dict[tuple[int, str], int] = {c: per_combo for c in combos}
    for s in subjects:
        cur = sum(v for c, v in quota.items() if c[1] == s)
        i = 0
        while cur < per_subject:
            c = next(c for c in combos if c[1] == s)
            quota[c] += 1
            cur += 1
            i += 1
            if i > 100:  # pragma: no cover - 防御
                break

    samples: list[dict] = []
    sid = 0
    for (vy, subject), count in sorted(quota.items()):
        segs = segments[(vy, subject)]
        qs = rank_quantiles(segs, STRATA_PER_GROUP)
        # 每层抽 count/STRATA 个（余数补给前几层）
        base = count // STRATA_PER_GROUP
        extra = count % STRATA_PER_GROUP
        for si in range(STRATA_PER_GROUP):
            k = base + (1 if si < extra else 0)
            lo_r, hi_r = qs[si], qs[si + 1] if si + 1 < len(qs) else segs[-1][1]
            if hi_r <= lo_r:
                hi_r = lo_r + 1
            for _ in range(k):
                sid += 1
                rank = rng.randint(lo_r, min(hi_r, segs[-1][1]))
                score = score_at_rank(segs, rank)
                samples.append({
                    "sample_id": f"S{sid:04d}",
                    "subject_type": subject,
                    "valid_year": vy,
                    "train_years": ",".join(str(y) for y in available_train_years(vy, all_years)),
                    "score": score,
                    "rank": rank,
                    "strata": f"{vy}-{subject}-L{si + 1}",
                })

    samples.sort(key=lambda s: s["sample_id"])
    return samples


def write_csv(samples: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["sample_id", "subject_type", "valid_year", "train_years", "score", "rank", "strata"]
    # newline="" + utf-8-sig：与 data/cleaned 口径一致，Excel 可直接打开
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(samples)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T10 回测样本生成（200 虚拟考生）")
    ap.add_argument("--n", type=int, default=DEFAULT_N, help=f"样本总数（默认 {DEFAULT_N}）")
    ap.add_argument("--seed", default=DEFAULT_SEED, help=f"随机种子（默认 {DEFAULT_SEED}）")
    ap.add_argument("--score-csv", type=Path, default=SCORE_CSV, help="一分一段 CSV 路径")
    ap.add_argument("--out", type=Path, default=SAMPLES_CSV, help="输出 CSV 路径")
    ap.add_argument("--force", action="store_true", help="覆盖已有文件（默认拒绝覆盖，防误擦）")
    args = ap.parse_args(argv)

    if args.out.exists() and not args.force:
        print(f"[拒绝] {args.out} 已存在。确认可覆盖请加 --force", file=sys.stderr)
        return 2

    segments = load_segments(args.score_csv)
    samples = build_samples(segments, n=args.n, seed=args.seed)
    write_csv(samples, args.out)

    # ---- 摘要（供人工核对分布合理性）----
    print(f"已生成 {len(samples)} 条样本 → {args.out}")
    print(f"  种子: {args.seed}  分层: {STRATA_PER_GROUP} 层/组")
    by_combo: dict[tuple[int, str], int] = {}
    for s in samples:
        by_combo[(s["valid_year"], s["subject_type"])] = by_combo.get((s["valid_year"], s["subject_type"]), 0) + 1
    print("  分组分布：")
    for (vy, sub), cnt in sorted(by_combo.items()):
        tks = sorted({int(x) for x in next(s for s in samples if s["valid_year"] == vy and s["subject_type"] == sub)["train_years"].split(",")})
        print(f"    valid_year={vy} {sub}: {cnt:>3} 条   训练窗口={tks}")
    ranks = [s["rank"] for s in samples]
    print(f"  位次范围：{min(ranks)} – {max(ranks)}（中位约 {sorted(ranks)[len(ranks) // 2]}）")
    scores = [s["score"] for s in samples]
    print(f"  分数范围：{min(scores)} – {max(scores)}")
    # 时间隔离自检（红线）
    bad = [s for s in samples if any(int(y) >= s["valid_year"] for y in s["train_years"].split(","))]
    if bad:
        print(f"[失败] {len(bad)} 条样本存在时间泄漏（训练年 ≥ 验证年）", file=sys.stderr)
        return 1
    print("  ✓ 时间隔离自检通过：全部样本的 train_years 均严格早于 valid_year")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
