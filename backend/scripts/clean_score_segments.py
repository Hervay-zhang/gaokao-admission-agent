"""T02 · 一分一段表清洗脚本（广东 2024/2025/2026 × 物理/历史）。

口径（详见 docs/升学志愿助手-data_caliber.md §2）
--------------------------------------------------
1. 位次 = 官方「**本科**（含本科加分）累计人数」列，即该分数的**位次上限**；
2. 同分并列统一取累计值（公开表不含同分内部再排序）；
3. 表首「699（含以上）」→ 记 `score=699, rank=累计值`；高出部分位次同取该值；
4. 表末到 100 分；< 100 分不虚构；
5. 逐分列出（含 0 人段），位次**非增**；出现缺口写入 `gaps.json`，**绝不补造**。

关键难点：**渗字**
    官方 PDF 左侧竖排「广东省教育考试院」会渗入单元格，形态如 `"考\\n20"`、`"育\\n5"`。
    处理：数值列取单元格内**最后一个**连续数字串（渗字恒在数字前）；
          名称/分数列取首个「数字（含以上）」形态。

幂等：可重复执行，输出确定（按 year,subject_type,score 排序）。

用法：
    cd backend && uv run python scripts/clean_score_segments.py            # 全量
    cd backend && uv run python scripts/clean_score_segments.py --year 2026 # 单年
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pdfplumber

# ---- 路径 ----
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CLEANED_DIR = PROJECT_ROOT / "data" / "cleaned"
OUT_CSV = CLEANED_DIR / "score_segments.csv"
GAPS_JSON = CLEANED_DIR / "gaps.json"

PROVINCE = "广东"
SUBJECTS = ["物理", "历史"]
YEARS = [2024, 2025, 2026]

# 本科累计人数所在列（0-based）：文化总分 | 本科段人数 | 本科累计 | 专科段人数 | 专科累计
COL_SCORE = 0
COL_BEN_KE_CUM = 2  # 「本科 累计人数」

# 分数区间下限（官方表末）
MIN_SCORE = 100

# 渗字：竖排「广东省教育考试院」残片（单字或两字）
SEEP_CHARS = set("广东省教育考试院")
_NUM_RE = re.compile(r"\d+")


def locate_pdf(year: int, subject: str) -> Path | None:
    """定位某年某科类的一分一段 PDF。

    2024/2025 在 raw/<year>/extracted/ 下；2026 直接在 raw/<year>/ 下。
    按文件名中的「（物理）/（历史）」匹配。
    """
    candidates: list[Path] = []
    base = RAW_DIR / str(year)
    if not base.exists():
        return None
    for p in base.rglob("*.pdf"):
        name = p.name
        # 仅普通类（物理/历史），排除艺术/体育类
        if "普通类" in name and f"（{subject}）" in name:
            candidates.append(p)
    if not candidates:
        return None
    # 优先 extracted 目录（更规范）
    candidates.sort(key=lambda x: (0 if "extracted" in str(x) else 1, len(str(x))))
    return candidates[0]


def strip_seep(text: str | None) -> str:
    """去掉单元格里渗入的竖排单字与换行，仅保留有效内容。"""
    if not text:
        return ""
    # 去掉换行后逐字符过滤渗字（保留数字、括号、百分号等）
    flat = text.replace("\n", "")
    return "".join(ch for ch in flat if ch not in SEEP_CHARS).strip()


def parse_score_cell(raw: str | None) -> int | None:
    """解析「文化总分」列：`699（含以上）` → 699；`698` → 698；无效 → None。"""
    s = strip_seep(raw)
    if not s:
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    return int(m.group())


def parse_rank_cell(raw: str | None) -> int | None:
    """解析「本科累计人数」列，处理渗字：取最后一个连续数字串。

    渗字恒出现在数字**之前**（如 `考\\n20`），故取最后一段数字最安全；
    若单元格本身是纯数字，结果相同。
    """
    s = strip_seep(raw)
    if not s:
        return None
    nums = _NUM_RE.findall(s)
    if not nums:
        return None
    return int(nums[-1])


def extract_year_subject(pdf_path: Path) -> dict[int, int]:
    """提取单份 PDF 的 {score: rank(本科累计)}。"""
    data: dict[int, int] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table:
                if len(row) <= COL_BEN_KE_CUM:
                    continue
                score = parse_score_cell(row[COL_SCORE])
                if score is None:
                    continue  # 表头行
                rank = parse_rank_cell(row[COL_BEN_KE_CUM])
                if rank is None:
                    continue
                # 同分只取一次（跨页表头可能重复）；出现重复以首次为准
                data.setdefault(score, rank)
    return data


def detect_gaps(scores: dict[int, int]) -> list[int]:
    """检测 [MIN_SCORE, max_score] 区间内缺失的分数。"""
    if not scores:
        return []
    top = max(scores)
    missing = [s for s in range(MIN_SCORE, top + 1) if s not in scores]
    return sorted(missing)


def check_monotonic(scores: dict[int, int]) -> list[str]:
    """校验位次随分数升高而非增；返回违规描述列表（空=通过）。"""
    problems: list[str] = []
    ordered = sorted(scores.items())  # 分数升序
    for (s1, r1), (s2, r2) in zip(ordered, ordered[1:], strict=False):
        if r2 > r1:  # 分数更高，累计人数反而更大 → 违规
            problems.append(f"分数 {s2}(rank={r2}) > 分数 {s1}(rank={r1})，位次非单调")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一分一段表清洗（广东 2024-2026）")
    parser.add_argument("--year", type=int, choices=YEARS, help="只处理指定年份")
    args = parser.parse_args(argv)

    years = [args.year] if args.year else YEARS
    all_rows: list[dict[str, object]] = []
    gaps: dict[str, list[int]] = {}
    problems: list[str] = []
    stats: list[tuple[int, str, int, int, int]] = []  # year, subject, rows, top, tail

    for year in years:
        for subject in SUBJECTS:
            pdf_path = locate_pdf(year, subject)
            if pdf_path is None:
                problems.append(f"[{year} {subject}] 未找到源 PDF")
                continue

            scores = extract_year_subject(pdf_path)
            if not scores:
                problems.append(f"[{year} {subject}] 解析为空：{pdf_path.name}")
                continue

            # 单调性
            mono = check_monotonic(scores)
            problems.extend(f"[{year} {subject}] {p}" for p in mono)

            # 缺口
            missing = detect_gaps(scores)
            if missing:
                gaps[f"{year}/{subject}"] = missing

            top, tail = max(scores), min(scores)
            for score in sorted(scores, reverse=True):
                all_rows.append({
                    "year": year,
                    "province": PROVINCE,
                    "subject_type": subject,
                    "score": score,
                    "rank": scores[score],
                })
            stats.append((year, subject, len(scores), top, tail))

    # ---- 写出 CSV（utf-8-sig，带 BOM 兼容 Excel）----
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    all_rows.sort(key=lambda r: (r["year"], r["subject_type"], -int(r["score"])))
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["year", "province", "subject_type", "score", "rank"])
        writer.writeheader()
        writer.writerows(all_rows)

    # ---- 写出 gaps.json ----
    with GAPS_JSON.open("w", encoding="utf-8") as f:
        json.dump(gaps, f, ensure_ascii=False, indent=2)

    # ---- 报告 ----
    print("=" * 62)
    print(f"一分一段表清洗完成 → {OUT_CSV.relative_to(PROJECT_ROOT)}")
    print("=" * 62)
    print(f"{'年份':<6}{'科类':<6}{'行数':<8}{'最高分':<8}{'最低分':<8}")
    for year, subject, n, top, tail in stats:
        print(f"{year:<6}{subject:<6}{n:<8}{top:<8}{tail:<8}")
    print("-" * 62)
    total = sum(s[2] for s in stats)
    print(f"合计 {total} 行；gaps 组数 {len(gaps)}；异常 {len(problems)} 条")
    if gaps:
        for k, v in gaps.items():
            print(f"  ⚠ gap {k}: 缺失 {len(v)} 个分数段（{v[:5]}{'...' if len(v) > 5 else ''}）")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("✅ 全部通过（分数单调、位次非增、无未标注缺口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
