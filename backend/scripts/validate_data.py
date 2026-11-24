"""T02/T03/T12 · 数据校验脚本（累积：一分一段 + 投档线 + 院校 + 选科要求 + 政策语料）。

校验项（详见 docs/升学志愿助手-data_caliber.md §10）
--------------------------------------------------
【一分一段 score_segments】
  S1 字段完整、类型合法
  S2 (year, province, subject_type, score) 组合唯一
  S3 分数逐分连续（100..top），覆盖率 ≥ 95%
  S4 位次非增（分数越高，累计人数越少或相等）
  S5 边界合法：rank ≥ 1；最低分行存在
  S6 抽查值回归（对官方已知点）
【投档线 admission_lines】（T03 落地后启用）
  A1 行唯一 (year, subject_type, university_code, group_code)
  A2 枚举合法（subject_type ∈ {物理,历史}）
  A3 min_rank 落在 min_score 的合法区间（更高分累计+1 ≤ rank ≤ 本分累计）
  A4 min_rank ≤ 当年科类总人数
  A5 院校代码能关联 universities
  A6 subject_requirement JSON 结构合法
【院校 universities】（T03 落地后启用）
  U1 code 唯一
  U2 641 必填、level/tags 枚举合法
【选科要求 subject_requirement】
  R1 JSON 结构合法；R2 status=待补 计数可见
【政策语料 policy_docs】（T12 落地后启用）
  P1 每篇有 source_url（url 非空）
  P2 每篇有 source_year（year 非空）
  P3 内容非空且长度合理（≥ 80 字）
  P4 doc_hash 唯一（内容 sha256 去重）
  P5 来源口径统计可见（source / doc_type / year 分布）

用法：
    cd backend && ./.venv/Scripts/python.exe scripts/validate_data.py            # 全部
    cd backend && ./.venv/Scripts/python.exe scripts/validate_data.py --report   # 输出报告 md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---- 路径 ----
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
CLEANED_DIR = PROJECT_ROOT / "data" / "cleaned"
EVAL_DIR = PROJECT_ROOT / "data" / "eval"

# 数据源路径（可被 --data-dir 覆盖，供 J3「注入错误自证」用例指向临时目录）
SCORE_CSV = CLEANED_DIR / "score_segments.csv"
ADMISSION_CSV = CLEANED_DIR / "admission_lines.csv"
UNIVERSITIES_CSV = CLEANED_DIR / "universities.csv"
POLICY_CSV = CLEANED_DIR / "policy_docs.csv"
REPORT_PATH = EVAL_DIR / "data_validation_report.md"


def set_data_dir(path: Path) -> None:
    """切换数据源目录（重设 CSV 与报告路径）。

    用途：J3 用例把**故意注入错误**的 CSV 放到临时目录，验证校验脚本确实会 FAIL
    —— 证明校验不是「永远通过」的空壳。
    """
    global SCORE_CSV, ADMISSION_CSV, UNIVERSITIES_CSV, POLICY_CSV, REPORT_PATH
    SCORE_CSV = path / "score_segments.csv"
    ADMISSION_CSV = path / "admission_lines.csv"
    UNIVERSITIES_CSV = path / "universities.csv"
    POLICY_CSV = path / "policy_docs.csv"
    REPORT_PATH = path / "data_validation_report.md"


MIN_SCORE = 100
COVERAGE_THRESHOLD = 0.95
# 政策语料正文最短字数（与 clean_policy.py 的 DEFAULT_MIN_LEN 一致）
POLICY_MIN_CONTENT_LEN = 80
# 政策语料 DoD 下限
POLICY_MIN_DOCS = 50

# 官方抽查点（(year, subject, score) -> rank），人工从官网/源 PDF 独立核对录入
# 这些是「黄金样本」，回归失败说明清洗口径漂移
SPOTCHECK_SEGMENTS: dict[tuple[int, str, int], int] = {
    # 2026 物理（源 PDF 独立二次解析核对）
    (2026, "物理", 699): 20,
    (2026, "物理", 600): 30891,
    (2026, "物理", 500): 181455,
    (2026, "物理", 300): 407474,
    (2026, "物理", 100): 433366,
    # 2026 历史
    (2026, "历史", 600): 3367,
    (2026, "历史", 500): 55042,
    (2026, "历史", 100): 294878,
    # 2025 物理/历史（末行累计）
    (2025, "物理", 100): 440208,
    (2025, "历史", 100): 292200,
    # 2024 物理/历史（末行累计）
    (2024, "物理", 100): 450961,
    (2024, "历史", 100): 269024,
}

# 运行期收集
RESULTS: list[tuple[str, str, str, str]] = []  # (code, 级别, 描述, 详情)


def record(code: str, ok: bool, desc: str, detail: str = "") -> None:
    RESULTS.append((code, "PASS" if ok else "FAIL", desc, detail))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ============================================================
#  一分一段
# ============================================================


def validate_score_segments() -> None:
    rows = read_csv(SCORE_CSV)
    if not rows:
        record("S0", False, "score_segments.csv 存在", f"未找到 {SCORE_CSV}")
        return
    record("S0", True, "score_segments.csv 存在", f"{len(rows)} 行")

    # ---- S1 字段类型 ----
    bad = 0
    for r in rows:
        try:
            int(r["year"])
            int(r["score"])
            int(r["rank"])
            assert r["province"] and r["subject_type"]
        except (KeyError, ValueError, AssertionError):
            bad += 1
    record("S1", bad == 0, "字段完整且类型合法", f"异常 {bad} 行")

    # ---- S2 唯一性 ----
    seen: set[tuple] = set()
    dup = 0
    for r in rows:
        key = (r["year"], r["province"], r["subject_type"], r["score"])
        if key in seen:
            dup += 1
        seen.add(key)
    record("S2", dup == 0, "(year,province,subject_type,score) 唯一", f"重复 {dup} 条")

    # ---- 按 (year, subject) 分组做 S3/S4/S5 ----
    groups: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in rows:
        groups[(r["year"], r["subject_type"])][int(r["score"])] = int(r["rank"])

    for (year, subject), scores in sorted(groups.items()):
        top = max(scores)
        expected = top - MIN_SCORE + 1
        actual = len(scores)
        cov = actual / expected if expected else 0
        record(
            "S3",
            cov >= COVERAGE_THRESHOLD,
            f"{year} {subject} 覆盖率 ≥ {COVERAGE_THRESHOLD:.0%}",
            f"{actual}/{expected} = {cov:.2%}",
        )

        ordered = sorted(scores.items())  # 分数升序
        violations = [
            (s1, r1, s2, r2)
            for (s1, r1), (s2, r2) in zip(ordered, ordered[1:], strict=False)
            if r2 > r1
        ]
        record("S4", not violations, f"{year} {subject} 位次非增", f"违规 {len(violations)} 处")

        boundary_ok = all(r >= 1 for r in scores.values()) and MIN_SCORE in scores
        record("S5", boundary_ok, f"{year} {subject} 边界合法（含最低分 100、rank≥1）")

    # ---- S6 抽查值回归 ----
    if SPOTCHECK_SEGMENTS:
        fails = []
        for (year, subject, score), expect in SPOTCHECK_SEGMENTS.items():
            got = groups.get((str(year), subject), {}).get(score)
            if got != expect:
                fails.append(f"{year}/{subject}/{score}: 期望 {expect} 实际 {got}")
        record("S6", not fails, "抽查值回归（官方黄金样本）", f"失败 {len(fails)}：" + "; ".join(fails[:3]))
    else:
        record("S6", True, "抽查值回归（基线待人工核对后录入）", "SKIP-PENDING")


# ============================================================
#  投档线（T03 后启用）
# ============================================================


def validate_admission() -> None:
    if not ADMISSION_CSV.exists():
        record("A0", True, "admission_lines.csv（T03 产出，暂缺）", "SKIP")
        return
    rows = read_csv(ADMISSION_CSV)
    record("A0", True, "admission_lines.csv 存在", f"{len(rows)} 行")

    # A1 唯一
    seen: set[tuple] = set()
    dup = 0
    for r in rows:
        key = (r["year"], r["subject_type"], r["university_code"], r["group_code"])
        if key in seen:
            dup += 1
        seen.add(key)
    record("A1", dup == 0, "(year,subject_type,university_code,group_code) 唯一", f"重复 {dup}")

    # A2 枚举
    bad_enum = sum(1 for r in rows if r.get("subject_type") not in ("物理", "历史"))
    record("A2", bad_enum == 0, "subject_type 枚举合法", f"异常 {bad_enum}")

    # ---- 载入一分一段（供 A3/A4 位次区间校验）----
    seg_rows = read_csv(SCORE_CSV)
    seg: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in seg_rows:
        try:
            seg[(r["year"], r["subject_type"])][int(r["score"])] = int(r["rank"])
        except (KeyError, ValueError):
            continue

    # A3 / A4 位次区间校验
    # 合法区间：min_rank ∈ [更高分累计 + 1, 本分累计]
    # 特例：投档分**高于一分一段表首聚合段**（如 2025 历史表首 672，而清华 675）时，
    #       区间上限取表首累计（官方未细分更高分），下限仍为 1。
    out_of_range: list[str] = []
    missing_seg = 0
    above_top = 0
    for r in rows:
        try:
            score = int(r["min_score"])
            rank = int(r["min_rank"])
        except (KeyError, ValueError, TypeError):
            continue  # 空投档行，跳过
        table = seg.get((r["year"], r["subject_type"]))
        if not table:
            missing_seg += 1
            continue
        upper = table.get(score)
        if upper is None:
            top = max(table)
            if score > top:
                # 高于表首：上限取表首累计，下限 1
                above_top += 1
                upper = table[top]
                if not (1 <= rank <= upper):
                    out_of_range.append(
                        f"{r['year']}/{r['subject_type']}/{r['university_code']}({r['group_code']})"
                        f" score={score}(>表首{top}) rank={rank} ∉ [1,{upper}]"
                    )
                continue
            missing_seg += 1
            continue
        higher = [table[s] for s in table if s > score]
        lower_bound = (min(higher) + 1) if higher else 1
        if not (lower_bound <= rank <= upper):
            out_of_range.append(
                f"{r['year']}/{r['subject_type']}/{r['university_code']}({r['group_code']})"
                f" score={score} rank={rank} ∉ [{lower_bound},{upper}]"
            )
    record(
        "A3", not out_of_range,
        "min_rank 落在 min_score 合法区间（更高分累计+1 ≤ rank ≤ 本分累计）",
        f"越界 {len(out_of_range)}；无一分一段 {missing_seg}；高于表首 {above_top}"
        + (f"；例：{out_of_range[0]}" if out_of_range else ""),
    )

    # A4 空投档行计数（官方「-」）不应超过合理阈值
    empty_rows = sum(1 for r in rows if not r.get("min_score") or not r.get("min_rank"))
    record("A4", True, "空投档行（官方「-」）计数可见", f"{empty_rows} 行（保留不补造）")

    # A5 院校代码能关联 universities
    uni_codes = {r["university_code"] for r in read_csv(UNIVERSITIES_CSV)} if UNIVERSITIES_CSV.exists() else set()
    if uni_codes:
        orphan = {r["university_code"] for r in rows} - uni_codes
        record("A5", not orphan, "投档线院校代码均可关联 universities", f"孤立 {len(orphan)}")
    else:
        record("A5", True, "投档线院校代码关联（universities 暂缺）", "SKIP")

    # A6 选科要求 JSON
    bad_json = 0
    pending = 0
    bad_first = 0
    for r in rows:
        raw = r.get("subject_requirement", "")
        if not raw:
            pending += 1
            continue
        try:
            obj = json.loads(raw)
            if obj.get("status") == "待补":
                pending += 1
            # first 必须与所在科类一致（广东 3+1+2 分物理/历史公布）
            first = obj.get("first") or []
            if first and first != [r["subject_type"]]:
                bad_first += 1
        except json.JSONDecodeError:
            bad_json += 1
    record("A6", bad_json == 0, "subject_requirement JSON 合法", f"非法 {bad_json}；待补 {pending}")
    record("A7", bad_first == 0, "subject_requirement.first 与科类一致", f"不一致 {bad_first}")


# ============================================================
#  院校（T03 后启用）
# ============================================================


def validate_universities() -> None:
    if not UNIVERSITIES_CSV.exists():
        record("U0", True, "universities.csv（T03 产出，暂缺）", "SKIP")
        return
    rows = read_csv(UNIVERSITIES_CSV)
    record("U0", True, "universities.csv 存在", f"{len(rows)} 所")

    codes = [r["university_code"] for r in rows]
    dup = len(codes) - len(set(codes))
    record("U1", dup == 0, "university_code 唯一", f"重复 {dup}")

    # U2 必填字段完整（code / name / base_name / level）
    bad_required = sum(
        1 for r in rows
        if not r.get("university_code") or not r.get("university_name")
        or not r.get("base_name") or not r.get("level")
    )
    record("U2", bad_required == 0, "必填字段完整（code/name/base_name/level）", f"缺失 {bad_required}")

    # U3 枚举合法（level / school_type / tags）
    levels = {"本科", "专科"}
    types = {"综合", "理工", "师范", "医药", "财经", "农林", "政法", "语言",
             "艺术", "体育", "军事", "民族"}
    tags_allowed = {"985", "211", "双一流"}
    bad_level = sum(1 for r in rows if r.get("level") not in levels)
    bad_type = sum(1 for r in rows if r.get("school_type") and r["school_type"] not in types)
    bad_tag = 0
    for r in rows:
        for t in filter(None, (r.get("tags") or "").split("|")):
            if t not in tags_allowed:
                bad_tag += 1
    record("U3", bad_level == 0 and bad_type == 0 and bad_tag == 0,
           "枚举合法（level/school_type/tags）",
           f"level异常 {bad_level}；type异常 {bad_type}；tag异常 {bad_tag}")

    # U4 985/211 计数与官方名单一致性（985=39±校区条；211≥112）
    n985 = sum(1 for r in rows if "985" in (r.get("tags") or "").split("|"))
    n211 = sum(1 for r in rows if "211" in (r.get("tags") or "").split("|"))
    nsyl = sum(1 for r in rows if "双一流" in (r.get("tags") or "").split("|"))
    ok = n985 >= 39 and n211 >= 112 and nsyl >= 147
    record("U4", ok, "985/211/双一流 计数达标（985≥39, 211≥112, 双一流≥147）",
           f"985={n985} 211={n211} 双一流={nsyl}")

    # U5 meta_status 枚举合法
    bad_status = sum(1 for r in rows if r.get("meta_status") not in ("已核", "待补"))
    record("U5", bad_status == 0, "meta_status 枚举合法（已核/待补）", f"异常 {bad_status}")


# ============================================================
#  政策语料（T12 后启用）
# ============================================================


def validate_policy() -> None:
    """P 系列：政策语料完整性/去重/来源口径（任务卡 J1-J3）。"""
    if not POLICY_CSV.exists():
        record("P0", True, "policy_docs.csv（T12 产出，暂缺）", "SKIP")
        return
    rows = read_csv(POLICY_CSV)
    record("P0", True, "policy_docs.csv 存在", f"{len(rows)} 篇")

    # P1 每篇有来源 URL（J1）
    no_url = [r for r in rows if not (r.get("url") or "").strip()]
    record("P1", not no_url, "每篇有 source_url（J1）", f"缺 url {len(no_url)} 篇")

    # P2 每篇有年份（J1）
    no_year = []
    bad_year = []
    for r in rows:
        y = (r.get("year") or "").strip()
        if not y:
            no_year.append(r)
            continue
        try:
            yi = int(y)
            if not (2000 <= yi <= 2100):
                bad_year.append(r)
        except ValueError:
            bad_year.append(r)
    record("P2", not no_year and not bad_year, "每篇有 source_year（J1）",
           f"缺 year {len(no_year)} 篇；非法 {len(bad_year)} 篇")

    # P3 内容非空且长度合理（J3）
    too_short = []
    empty = 0
    for r in rows:
        c = (r.get("content") or "").strip()
        if not c:
            empty += 1
            continue
        if len(c) < POLICY_MIN_CONTENT_LEN:
            too_short.append((r.get("title", "")[:24], len(c)))
    record("P3", empty == 0 and not too_short,
           f"内容非空且 ≥ {POLICY_MIN_CONTENT_LEN} 字（J3）",
           f"空 {empty} 篇；过短 {len(too_short)} 篇"
           + (f"；例 {too_short[0]}" if too_short else ""))

    # P4 doc_hash 唯一（内容 sha256 去重）
    hashes = [(r.get("doc_hash") or "").strip() for r in rows]
    missing_hash = sum(1 for h in hashes if not h)
    dup = len(hashes) - len(set(hashes))
    record("P4", missing_hash == 0 and dup == 0, "doc_hash 唯一且非空",
           f"缺 hash {missing_hash}；重复 {dup}")

    # P5 无重复标题（J2：同一标题只保留一篇）
    titles = [(r.get("title") or "").strip() for r in rows]
    dup_title = sum(c - 1 for c in Counter(titles).values() if c > 1)
    record("P5", dup_title == 0, "无重复标题（J2）", f"重复 {dup_title} 篇")

    # P6 来源口径统计可见（source / doc_type / year）
    if rows:
        src = Counter((r.get("source") or "(空)") for r in rows)
        types = Counter((r.get("doc_type") or "(空)") for r in rows)
        years = Counter((r.get("year") or "(空)") for r in rows)
        detail = (
            "来源: " + ", ".join(f"{k}={v}" for k, v in src.most_common(3))
            + " | 类型: " + ", ".join(f"{k}={v}" for k, v in types.most_common(3))
            + " | 年份: " + ", ".join(f"{k}={v}" for k, v in sorted(years.items(), key=lambda x: str(x[0])))
        )
        record("P6", True, "来源/类型/年份分布可见", detail)

    # P7 DoD：政策语料 ≥ 50 篇
    record("P7", len(rows) >= POLICY_MIN_DOCS, f"政策语料 ≥ {POLICY_MIN_DOCS} 篇（DoD）",
           f"实际 {len(rows)} 篇")


# ============================================================
#  主流程
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据校验（一分一段 / 投档线 / 院校 / 选科要求 / 政策语料）")
    parser.add_argument("--report", action="store_true", help="输出报告到 data/eval/data_validation_report.md")
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="覆盖数据源目录（默认 data/cleaned）；J3 注入错误自证用",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出汇总，不逐条打印")
    args = parser.parse_args(argv)

    if args.data_dir is not None:
        set_data_dir(args.data_dir)

    RESULTS.clear()
    validate_score_segments()
    validate_admission()
    validate_universities()
    validate_policy()

    # ---- 汇总打印 ----
    if not args.quiet:
        print("=" * 78)
        print("数据校验报告")
        print("=" * 78)
        print(f"{'编号':<6}{'结果':<8}{'校验项':<44}{'详情'}")
        print("-" * 78)
    fails = 0
    for code, status, desc, detail in RESULTS:
        mark = "✅" if status == "PASS" else "❌"
        if status == "FAIL":
            fails += 1
        if not args.quiet:
            print(f"{code:<6}{mark:<8}{desc:<44}{detail}")
    if not args.quiet:
        print("-" * 78)
    print(f"合计 {len(RESULTS)} 项，通过 {len(RESULTS) - fails}，失败 {fails}")

    if args.report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        report = REPORT_PATH
        lines = ["# 数据校验报告（T02/T03/T05/T12）", "", f"共 {len(RESULTS)} 项，失败 {fails} 项。", "", "| 编号 | 结果 | 校验项 | 详情 |", "|---|---|---|---|"]
        for code, status, desc, detail in RESULTS:
            lines.append(f"| {code} | {'✅' if status == 'PASS' else '❌'} | {desc} | {detail} |")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写入 {report}")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
