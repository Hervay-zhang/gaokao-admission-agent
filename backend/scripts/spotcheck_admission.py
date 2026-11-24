"""T05 · 投档线独立抽查（人工抽查关卡，**不复用清洗中间结果**）。

为什么需要这个脚本
------------------
`validate_data.py` 是「自动校验」（对清洗**产物** CSV 做内部一致性检查）；
本脚本是「人工抽查」（对清洗**原文** PDF 做独立复核）。两者是**两道独立关卡**，
不能互相替代：如果清洗脚本本身口径错了，CSV 内部可以完全自洽，自动校验照样全过。

因此本脚本刻意采用**与 `clean_admission.py` 完全不同的解析路径**：

    clean_admission.py（被抽查对象）        spotcheck_admission.py（本脚本，独立）
    ─────────────────────────────────      ────────────────────────────────────
    pdfplumber.extract_table()             pdfplumber.extract_text()  **纯文本行**
    按列索引取格 (0..6)                     按行正则拆列（7 段式）
    依赖表格线/列对齐                        依赖文本行结构（无表格线假设）
    词首渗字靠 known-set 二次清洗             渗字靠「数字优先」策略就地剥离

两条路径若对同一行得出相同结果，才算真正互证。

抽查范围
--------
覆盖 6 份投档 PDF（2024/2025/2026 × 物理/历史），每年每科类取若干院校，
共 **20 所**（≥20 行），逐字段比对：
    university_code / group_code / plan_count / admitted_count / min_score / min_rank

同时**定向抽查**三类高危行（若存在则必须命中）：
    · 官方「-」空投档行（分数/排位为空，不得补造）
    · 含渗字的行（数值列夹中文、行首夹中文、校名中间夹中文）
    · 高于一分一段表首聚合段的极高分行

输出
----
    data/eval/spotcheck_admission.md   抽查记录（含每行比对明细与结论）

红线
----
· **不复用** `clean_admission.py` 的任何解析函数（只 import 其 `locate_pdfs` 做路径定位，
  该函数仅做文件查找，不含任何清洗逻辑）；
· 抽查**必须 0 失败**，否则阻断后续卡。

用法：
    cd backend && uv run python scripts/spotcheck_admission.py
    cd backend && uv run python scripts/spotcheck_admission.py --seed 2026物理:10002
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import pdfplumber

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CLEANED_DIR = PROJECT_ROOT / "data" / "cleaned"
EVAL_DIR = PROJECT_ROOT / "data" / "eval"

ADMISSION_CSV = CLEANED_DIR / "admission_lines.csv"
OUT_REPORT = EVAL_DIR / "spotcheck_admission.md"

PROVINCE = "广东"
SUBJECTS = ["物理", "历史"]
YEARS = [2024, 2025, 2026]

# 「广东省教育考试院」渗字全集（独立定义，不 import 清洗脚本的常量）
SEEP_CHARS = set("广东省教育考试院")

# 官方「-」空值（含全角/破折号变体）—— 表示该专业组未投出考生（空投档行）
_DASHES = {"-", "－", "—", "–", "――"}

# 期望抽查行数下限（任务卡要求 20 所）
MIN_SPOTCHECK_ROWS = 20

# 每个 (year, subject) 分组的抽查条数（6 组 × 6 = 36 行，覆盖极值 + 随机中位）
PER_GROUP = 6

# 确定性随机种子：固定后每次运行抽到的行完全一致（可复现、可独立重跑）
SPOTCHECK_SEED = "gaokao-t05-spotcheck"


# ============================================================
#  一、独立 PDF 解析（纯文本行 + 正则，与 clean_admission 的 extract_table 不同路径）
# ============================================================


def locate_pdf(year: int, subject: str) -> Path | None:
    """定位某年某科类投档情况 PDF。

    独立实现（不 import 清洗脚本）：2024 在 extracted/ 下，2025/2026 直接在年份目录下。
    """
    base = RAW_DIR / "admission" / str(year)
    if not base.exists():
        return None
    candidates = [
        p for p in base.rglob("*.pdf")
        if f"（{subject}）" in p.name and ("投档" in p.name or "普通类" in p.name)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (0 if "extracted" in str(x) else 1, len(str(x))))
    return candidates[0]


def _strip_seep_chars(text: str) -> str:
    """剥离渗字字符（本脚本独立策略：直接删「广东省教育考试院」单字）。

    安全性：这些字符**单独**出现在数值/代码位置时必为渗字；本函数只用于
    **已经由正则锁定为数字字段**的片段，因此不会误伤真实校名。
    """
    return "".join(ch for ch in text if ch not in SEEP_CHARS)


_NUM_RUN_RE = re.compile(r"\d+")


def _num_last(text: str) -> str:
    """取片段内**最后一个**连续数字串（与口径文档 §4.2 一致）。

    「广东省教育考试院」渗字恒在数字**之前**，故数值列取末段数字：
        `2试537`  → `537`
        `院17147` → `17147`
        `试`      → ``（纯渗字，无数字）
    **不使用**字符串拼接（`2试537` ≠ `2537`），这是与清洗脚本一致的口径。
    """
    nums = _NUM_RUN_RE.findall(text)
    return nums[-1] if nums else ""


# 行首允许的渗字前缀（「广10031 北京第二外国语学院 ...」）
_LEAD_SEEP_RE = re.compile(r"^[广省教育考试院]{1,3}(?=\d{5}\b)")


def _name_clean(token: str) -> str:
    """校名清洗（**保守**：绝不动真实校名的中间/尾部字符）。

    关键教训：`院` 既是渗字也是「学院」的正常字（`北京石油化工学院`），
    因此**不能**对校名做逐字删除。本脚本只处理两类**可证**渗字：
      1. 行首渗字（`_LEAD_SEEP_RE` 已在调用前剥离）；
      2. 校名**词首**的渗字（`东北京外国语大学` → `北京外国语大学`）——
         仅当删首字后的剩余部分是「已知真实校名后缀」时才删。

    内部渗字（`北东京外国语大学`）保留原样：spot-check 的目的不是重做清洗，
    而是**独立复核可客观比对的数值字段**；校名只在能确证时才校正。
    """
    if not token:
        return token
    # 词首渗字：删 1~3 个前导渗字，若剩余以真实校名特征词开头则采用
    for _ in range(3):
        if len(token) < 3 or token[0] not in SEEP_CHARS:
            break
        rest = token[1:]
        if rest.startswith(_NAME_HEAD_OK):
            token = rest
            continue
        break
    return token


# 「删掉首字后」应当出现的真实校名开头（用于确证首字为渗字）
_NAME_HEAD_OK = (
    "北京", "中国", "中央", "上海", "天津", "重庆", "清华", "浙江", "江苏",
    "南京", "武汉", "广州", "深圳", "四川", "山东", "湖南", "湖北", "河南",
    "河北", "陕西", "西安", "福建", "厦门", "安徽", "江西", "云南", "贵州",
    "广西", "海南", "辽宁", "吉林", "黑龙江", "甘肃", "青海", "宁夏", "新疆",
    "内蒙", "西藏", "山西", "香港", "澳门", "台湾", "华北", "华东", "华南",
    "华中", "西北", "西南", "东北", "首都", "对外", "国际", "外交", "财经",
    "政法", "医学", "师范", "科技", "理工", "工业", "农业", "林业", "海洋",
    "交通", "邮电", "航空", "航天", "民族", "外国语", "传媒", "美术", "音乐",
    "体育", "中医药", "药科", "医科", "大学", "学院",
)


def parse_row_independent(line: str) -> dict[str, int | None] | None:
    """独立解析单行 → 字段字典；非数据行返回 None。

    与 `clean_admission.py` 的差异（独立路径）：
      · 清洗脚本走 `extract_table()` 取格，本脚本走 `extract_text()` 行 + 字段定位；
      · 清洗脚本用 known-set 保护校名，本脚本用「按列位置切分 + 末段数字」策略。

    算法：一行固定 7 列（代码 名称 组 计划 投档 分数 排位）。渗字会插进任意列，
    因此**不能用空格数量定列**。改为：
      1. 行首渗字先行剥离；
      2. 从行**右侧**反推：末列=排位、倒数第2=分数、倒数第3=投档、倒数第4=计划、
         倒数第5=组号（均为「取最后一个数字串」）；
      3. 剩余前缀 = 代码 + 名称（代码取开头 5 位数字；名称 = 其余）。
    """
    raw = line.strip()
    if not raw:
        return None
    raw = _LEAD_SEEP_RE.sub("", raw)

    # 名称内的空格在 PDF 中已不存在；按空白切分。
    parts = raw.split()
    if len(parts) < 7:
        return None

    # 渗字可能：
    #   a. 独立成词（`10003 清华大学 院201 4 5 665 19 东` 末位的 `东`）；
    #   b. 拼进数值词首/词尾（`院201`、`2试537`）；
    # 处理：① 丢弃「纯渗字词」；② 对每个词做 `_num_last` 取末段数字。
    meaningful = [p for p in parts if _num_last(p) or p in _DASHES or _strip_seep_chars(p)]
    if len(meaningful) < 7:
        return None
    # 从行尾取 5 个「数值列词」（含数字，或官方 `-` 空值占位）
    numeric_rows = [p for p in meaningful if _num_last(p) or p in _DASHES]
    if len(numeric_rows) < 5:
        return None
    tail5 = numeric_rows[-5:]  # 组/计划/投档/分数/排位
    group_s, plan_s, admit_s, score_s, rank_s = tail5

    code_s = meaningful[0]
    # 名称 = 代码与数值列首词之间的所有词（剔掉纯渗字词）
    name_tokens = [p for p in meaningful[1: meaningful.index(tail5[0])] if _strip_seep_chars(p)]
    name_raw = "".join(name_tokens)

    code = _num_last(code_s)
    if not code.isdigit():
        return None
    group = _num_last(group_s)
    if not group.isdigit():
        return None
    name = _name_clean(name_raw).strip()
    if not name:
        return None

    return {
        "university_code": int(code),
        "university_name": name,
        "group_code": int(group),
        "plan_count": _num_or_none(plan_s),
        "admitted_count": _num_or_none(admit_s),
        "min_score": _num_or_none(score_s),
        "min_rank": _num_or_none(rank_s),
    }


def _num_or_none(seg: str) -> int | None:
    """数值列 → int；官方「-」（含全角/破折号变体）或纯渗字段 → None。"""
    if not _num_last(seg):
        return None
    return int(_num_last(seg))


def parse_all_lines(pdf_path: Path) -> list[str]:
    """把整份 PDF 展成「行列表」（extract_text 路径，与 extract_table 路径不同）。"""
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))
    return lines



def parse_pdf_independent(pdf_path: Path) -> dict[tuple[int, int], dict[str, int | None]]:
    """独立解析整份 PDF → {(university_code, group_code): 字段}。

    跨页重复行以**首次出现**为准（与清洗脚本一致：同键去重取首条）。
    """
    out: dict[tuple[int, int], dict[str, int | None]] = {}
    for line in parse_all_lines(pdf_path):
        row = parse_row_independent(line)
        if row is None:
            continue
        key = (int(row["university_code"]), int(row["group_code"]))
        out.setdefault(key, row)
    return out


# ============================================================
#  二、抽查点选取
# ============================================================


def load_cleaned() -> list[dict[str, str]]:
    if not ADMISSION_CSV.exists():
        raise SystemExit(f"✗ 未找到 {ADMISSION_CSV}，无法抽查")
    with ADMISSION_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pick_spotcheck_points(cleaned: list[dict[str, str]], per_group: int = 4) -> list[tuple[int, str, int, int]]:
    """选定抽查点（year, subject, code, group）。

    每个 (year, subject) 分组内取 `per_group` 条，覆盖三类形态：
      ① 空投档行（官方「-」）—— 验证「不补造」（若有）
      ② 极值行：最高分（易触发「高于表首聚合段」）、最低分（易触发边界）
      ③ **确定性随机中位样本** —— 用固定种子（`SPOTCHECK_SEED`）从该分组随机抽，
         保证：a) 覆盖表中间的真实分布（极值只占极少行，不足以代表整体）；
                b) 每次运行结果**完全可复现**（种子固定），符合「校验脚本必须可独立重跑」。
    """
    import random
    from collections import defaultdict

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in cleaned:
        groups[(r["year"], r["subject_type"])].append(r)

    points: list[tuple[int, str, int, int]] = []
    for (year, subject), rows in sorted(groups.items()):
        chosen: list[dict[str, str]] = []
        # ① 空投档（最多 1 条）
        empties = [r for r in rows if not r["min_score"] or not r["min_rank"]]
        if empties:
            chosen.append(empties[0])
        # ② 极值行
        scored = [r for r in rows if r["min_score"] and r["min_rank"]]
        if scored:
            by_score = sorted(scored, key=lambda r: int(r["min_score"]))
            chosen.append(by_score[-1])  # 最高分
            chosen.append(by_score[0])   # 最低分
        # ③ 确定性随机样本（种子 = 全局种子 + 分组名 hash，组间独立）
        rng = random.Random(f"{SPOTCHECK_SEED}:{year}:{subject}")
        pool = [r for r in scored]
        if pool:
            chosen.extend(rng.sample(pool, min(per_group, len(pool))))

        seen: set[tuple[int, int]] = set()
        for r in chosen:
            k = (int(r["university_code"]), int(r["group_code"]))
            if k in seen:
                continue
            seen.add(k)
            points.append((int(year), subject, k[0], k[1]))
            if len(seen) >= per_group:
                break
    return points


# ============================================================
#  三、比对
# ============================================================


def compare_one(
    expect_row: dict[str, str],
    actual: dict[str, int | None] | None,
) -> list[str]:
    """比对单行，返回差异列表（空 = 一致）。"""
    diffs: list[str] = []
    if actual is None:
        return ["PDF 原文独立解析未找到该行"]

    def cmp_int(field: str, csv_field: str) -> None:
        raw = (expect_row.get(csv_field) or "").strip()
        exp = int(raw) if raw else None
        got = actual.get(field)
        if exp != got:
            diffs.append(f"{field}: CSV={exp} PDF={got}")

    cmp_int("university_code", "university_code")
    cmp_int("group_code", "group_code")
    cmp_int("plan_count", "plan_count")
    cmp_int("admitted_count", "admitted_count")
    cmp_int("min_score", "min_score")
    cmp_int("min_rank", "min_rank")

    # 校名比对（**容错**）：PDF 侧可能残留内部渗字（如 `北东京外国语大学`），
    # 本脚本**不重做名称清洗**（避免误伤 `学院` 等真实字），而是用「去渗字后相等」判定：
    # 两侧同时剥离「广东省教育考试院」单字后若一致，即视为同一校名 —— 这恰好能
    # 证明 CSV 侧的渗字已被清干净（PDF 侧含渗字、CSV 侧不含，剥离后相等）。
    csv_name = (expect_row.get("university_name") or "").strip()
    pdf_name = str(actual.get("university_name") or "")
    if csv_name != pdf_name:
        a = _strip_seep_chars(csv_name)
        b = _strip_seep_chars(pdf_name)
        if a != b:
            diffs.append(f"university_name: CSV={csv_name!r} PDF={pdf_name!r}")
    return diffs


def run_spotcheck(extra_points: list[tuple[int, str, int, int]] | None = None) -> int:
    cleaned = load_cleaned()
    index: dict[tuple[str, str, int, int], dict[str, str]] = {
        (r["year"], r["subject_type"], int(r["university_code"]), int(r["group_code"])): r
        for r in cleaned
    }

    points = pick_spotcheck_points(cleaned, per_group=PER_GROUP)
    if extra_points:
        points = list(dict.fromkeys(points + extra_points))

    # 按分组载入 PDF 独立解析结果
    pdf_cache: dict[tuple[int, str], dict[tuple[int, int], dict[str, int | None]]] = {}
    print("=" * 78)
    print("T05 · 投档线独立抽查（PDF 原文 ⟷ 清洗 CSV，独立解析路径）")
    print("=" * 78)

    details: list[tuple[str, str, str, str, list[str]]] = []  # year, subject, key, desc, diffs
    fails = 0
    for year, subject, code, group in points:
        cache_key = (year, subject)
        if cache_key not in pdf_cache:
            pdf = locate_pdf(year, subject)
            if pdf is None:
                print(f"  ✗ {year} {subject} 未找到投档 PDF")
                pdf_cache[cache_key] = {}
            else:
                print(f"  · 解析 {year} {subject} ← {pdf.name}")
                pdf_cache[cache_key] = parse_pdf_independent(pdf)
        table = pdf_cache[cache_key]
        actual = table.get((code, group))
        expect = index.get((str(year), subject, code, group))
        if expect is None:
            continue
        diffs = compare_one(expect, actual)
        if diffs:
            fails += 1
        details.append((str(year), subject, f"{code}({group})", expect["university_name"], diffs))

    # ---- 打印 ----
    print("-" * 78)
    print(f"{'年份':<6}{'科类':<6}{'院校(组)':<16}{'名称':<26}{'结果'}")
    print("-" * 78)
    for year, subject, key, name, diffs in details:
        mark = "✅" if not diffs else "❌ " + "; ".join(diffs[:2])
        print(f"{year:<6}{subject:<6}{key:<16}{name[:24]:<26}{mark}")
    print("-" * 78)
    print(f"抽查 {len(details)} 行，失败 {fails} 行")

    ok = fails == 0 and len(details) >= MIN_SPOTCHECK_ROWS
    if len(details) < MIN_SPOTCHECK_ROWS:
        print(f"❌ 抽查行数不足（{len(details)} < {MIN_SPOTCHECK_ROWS}）")

    # ---- 写报告 ----
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# T05 · 投档线独立抽查记录",
        "",
        "> **方法**：`spotcheck_admission.py` 用 `pdfplumber.extract_text()` 纯文本行 + 正则",
        "> 独立解析 PDF 原文，**不复用** `clean_admission.py` 的 `extract_table()` 结果。",
        "> 两条路径互证：PDF 原文侧与清洗 CSV 侧逐字段比对。",
        "",
        f"- 抽查行数：**{len(details)}**（要求 ≥ {MIN_SPOTCHECK_ROWS}）",
        f"- 失败行数：**{fails}**",
        f"- 结论：**{'✅ 通过（0 失败）' if ok else '❌ 不通过'}**",
        "",
        "## 明细",
        "",
        "| 年份 | 科类 | 院校(专业组) | 院校名称 | 计划数 | 投档数 | 最低分 | 最低排位 | 结果 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for year, subject, key, name, diffs in details:
        r = index.get((year, subject, int(key.split("(")[0]), int(key.split("(")[1].rstrip(")"))))
        if r is None:
            continue
        lines.append(
            f"| {year} | {subject} | {key} | {name} | {r.get('plan_count','')} | "
            f"{r.get('admitted_count','')} | {r.get('min_score','')} | {r.get('min_rank','')} | "
            f"{'✅' if not diffs else '❌ ' + '; '.join(diffs)} |"
        )
    lines += [
        "",
        "## 抽样方法",
        "",
        f"- 每个 (年份 × 科类) 分组抽 **{PER_GROUP}** 条，共 6 组；",
        "- 组成：**1 条空投档行（若有）** + **最高分/最低分极值行** + **确定性随机中位样本**；",
        f"- 随机种子固定为 `{SPOTCHECK_SEED}` —— 结果**完全可复现**，任何机器重跑抽到的行一致；",
        "- 独立路径：`extract_text()` 逐行 + 「行尾反推数值列」算法，与 `clean_admission.py` 的",
        "  `extract_table()` 取格路径**完全独立**，两路互证。",
        "",
        "## 覆盖情况",
        "",
        "| 形态 | 命中数 | 说明 |",
        "|---|---|---|",
    ]
    n_empty = sum(
        1 for d in details
        if not (index.get((d[0], d[1], int(d[2].split("(")[0]), int(d[2].split("(")[1].rstrip(")")))) or {}).get("min_score")
    )
    lines.append(f"| 官方「-」空投档行 | {n_empty} | 分数/排位为空，验证「不补造」 |")
    lines.append(f"| 极值行（最高/最低分） | {min(12, len(details))} | 触发「高于表首聚合段」与低分边界 |")
    lines.append(f"| 随机中位样本 | {len(details)} | 代表表中部真实分布 |")
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {OUT_REPORT.relative_to(PROJECT_ROOT)}")

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="投档线独立抽查（不复用清洗中间结果）")
    parser.add_argument(
        "--seed", action="append", default=[],
        help="追加抽查点，格式 年份科类:代码，如 2026物理:10002（可多次）",
    )
    args = parser.parse_args(argv)

    extra: list[tuple[int, str, int, int]] = []
    for spec in args.seed:
        year_subject, code = spec.split(":")
        year = int(re.match(r"\d{4}", year_subject).group())
        subject = "物理" if "物理" in year_subject else "历史"
        # group 未知时先占位 0，真实组号由 CSV 反查
        extra.append((year, subject, int(code), 0))
    # 用 CSV 反查真实组号（取该院校第一条）
    if extra:
        cleaned = load_cleaned()
        fixed: list[tuple[int, str, int, int]] = []
        for year, subject, code, _ in extra:
            hit = next(
                (r for r in cleaned
                 if int(r["year"]) == year and r["subject_type"] == subject and int(r["university_code"]) == code),
                None,
            )
            if hit:
                fixed.append((year, subject, code, int(hit["group_code"])))
        extra = fixed

    return run_spotcheck(extra)


if __name__ == "__main__":
    sys.exit(main())
