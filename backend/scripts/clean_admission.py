"""T03 · 院校库与投档线清洗脚本（广东 2024/2025/2026 × 物理/历史）。

产出
----
1. `data/cleaned/admission_lines.csv`
   字段：year, province, subject_type, university_code, university_name,
        group_code, plan_count, admitted_count, min_score, min_rank, subject_requirement
2. `data/cleaned/universities.csv`
   字段：university_code, university_name, base_name, level, province,
        school_type, tags, name_variants, meta_status

口径（详见 docs/升学志愿助手-data_caliber.md §3/§4）
--------------------------------------------------
1. **投档线 ≠ 录取线**：只取专业组投档最低分 / 投档最低排位；
2. 官方「-」（空投档）行**保留**，分数/排位置空，**绝不补造**；
3. 院校代码唯一；**同名院校按代码严格区分**（哈工大本部/深圳/威海等）；
4. **选科要求（subject_requirement）**：
   - 投档公告 PDF **不含**选科要求列，无法从该来源确定；
   - 唯一可从数据**确定推导**的是：广东 3+1+2 分物理/历史公布 → 该行专业组的**首选科目**必为对应科目；
   - 再选科目要求**无来源 → 标「待补」**（绝不猜测）。
     故输出：`{"mode":"3+1+2","first":["物理"],"second":[],"pattern":"none","status":"待补","raw":""}`
     其中 first 由数据来源确定，status=待补 表示再选科目待人工补充。

关键难点：**渗字**
    同 T02，PDF 左侧竖排「广东省教育考试院」渗入单元格（如 `院10001`、`东509`）。
    数值列取**最后一个**连续数字串；名称列删除安全渗字（`省教育考试院`），
    歧义渗字（`广`/`东`）仅当删除后仍是已知变体才删（保护「广东/东北/东南/广西」等真实校名）。

幂等：可重复执行，输出确定（按 year,subject_type,university_code,group_code 排序）。

用法：
    cd backend && uv run python scripts/clean_admission.py
    cd backend && uv run python scripts/clean_admission.py --year 2026
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pdfplumber

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CLEANED_DIR = PROJECT_ROOT / "data" / "cleaned"
OUT_ADMISSION = CLEANED_DIR / "admission_lines.csv"
OUT_UNIVERSITIES = CLEANED_DIR / "universities.csv"

PROVINCE = "广东"
SUBJECTS = ["物理", "历史"]
YEARS = [2024, 2025, 2026]

# 投档表列（0-based）
C_CODE, C_NAME, C_GROUP, C_PLAN, C_ADMIT, C_SCORE, C_RANK = 0, 1, 2, 3, 4, 5, 6

NUM_RE = re.compile(r"\d+")
# 安全渗字（必删）：这些字出现在校名里几乎不可能构成真实校名的一部分
SAFE_SEEP = ["省教育考试院", "教育考试院", "考试院"]
# 歧义渗字（仅在删除后仍是已知变体时才删）
# 「广东省教育考试院」逐字拆解：广/东/省/教/育/考/试/院
# 其中 `东`/`广` 极易与真实校名冲突（东北/东南/广东/广州/广西），必须走 known-set 保护；
# `省`/`教`/`育`/`考`/`试`/`院` 几乎不可能出现在任何大学名**首位**，但仍统一走同一条保守逻辑。
AMBIG_SEEP = ["广", "东", "省", "教", "育", "考", "试", "院"]


def locate_pdfs(year: int, subject: str) -> Path | None:
    """定位某年某科类投档情况 PDF。"""
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


# 解析缓存目录（pdfplumber 解析 6 份 ~70 页 PDF 需 ~15 分钟，缓存后重跑 <1s）
CACHE_DIR = PROJECT_ROOT / "data" / ".cache"


def _pdf_signature(pdf: Path) -> str:
    """PDF 文件名+大小+修改时间 → 缓存键（内容变了自动失效）。"""
    st = pdf.stat()
    return f"{pdf.name}__{st.st_size}__{int(st.st_mtime)}"


def parse_admission_cached(pdf: Path, year: int, subject: str) -> list[dict[str, object]]:
    """带缓存的投档 PDF 解析：优先读缓存，缺失/失效才真解析。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sig = hashlib.sha1(_pdf_signature(pdf).encode("utf-8")).hexdigest()[:16]
    cache_file = CACHE_DIR / f"admission_{year}_{subject}_{sig}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            print(f"    · 命中缓存 {cache_file.name}（{len(data)} 行）")
            return data  # type: ignore[return-value]
        except (ValueError, OSError):
            pass
    rows = parse_admission_pdf(pdf, year, subject)
    cache_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"    · 解析完成并写缓存 {cache_file.name}（{len(rows)} 行）")
    return rows


def clean_seep_number(raw: str | None) -> int | None:
    """数值列：去渗字后取最后一个连续数字串。"""
    if raw is None:
        return None
    flat = str(raw).replace("\n", "")
    nums = NUM_RE.findall(flat)
    if not nums:
        return None
    return int(nums[-1])


def clean_seep_text(raw: str | None, known_variants: set[str] | None = None) -> str:
    """名称列：删除渗字，保护真实校名。

    渗字来源：PDF 左侧竖排「广东省教育考试院」逐字渗入单元格。
    分两级处理：
      1. **安全渗字**（`省教育考试院` 等连续串）—— 直接删；
      2. **歧义渗字**（`广`/`东`/`省` 单字）—— 仅当满足以下之一才删：
         a. 删除后仍是某已知变体（传入 known_variants）；
         b. 位于**词首**且后接「已知大学名特征」（如 `佛山大学`、`工业大学` 等）。
    保护目标：`广东`/`东北`/`东南`/`广西`/`广州`/`省理工` 等真实校名不被误伤。
    """
    if raw is None:
        return ""
    text = str(raw).replace("\n", "").replace(" ", "")
    for seep in SAFE_SEEP:
        text = text.replace(seep, "")
    # 歧义渗字：仅当删除后仍是某个已知变体时才删
    if known_variants:
        for ch in AMBIG_SEEP:
            if ch in text:
                stripped = text.replace(ch, "")
                if stripped in known_variants:
                    text = stripped
    return text.strip()


# 词首歧义渗字的「安全删除」白名单：
# 若删掉首字后，剩余部分以这些真实校名特征开头，则首字必为渗字。
_LEADING_SEEP_TARGETS = (
    "佛山大学", "华南理工", "华南师范", "华南农业", "暨南大学", "中山大学",
    "广东工业", "广东外语", "广东财经", "广东医科", "广东药科",
    "工业大学", "理工大学", "师范大学", "科技大学", "交通大学",
    "外语外贸", "财经大学", "医科大学", "中医药大学", "海洋大学",
    "美术学院", "音乐学院", "体育学院", "大学", "学院",
)

# 真实的「东 X 大学」白名单（这些首字 `东` 是校名的一部分，绝不能删）
_EAST_LEGIT = (
    "东北农业", "东北大学", "东北师范", "东北林业", "东北电力", "东北石油",
    "东北财经", "东华大学", "东华理工", "东南大学", "东莞城市", "东莞理工",
    "东软",  # 广东东软学院
)


def make_known_name_set(names: list[str]) -> set[str]:
    """从全量校名集合中，构建「真实校名集合」。

    只收录**真实出现过的**全名与其去括号 base（**不收录**去词首渗字的变体，
    否则会把 `广东工业大学` 的 `东工业大学` 也当成真实名，导致 `广` 被误删）。
    """
    known: set[str] = set()
    for n in names:
        if not n:
            continue
        known.add(n)
        known.add(re.split(r"[（(]", n, maxsplit=1)[0])
    return known


def strip_leading_seep(name: str, known: set[str] | None = None) -> str:
    """删除词首歧义渗字（`东佛山大学` → `佛山大学`、`省佛山大学` → `佛山大学`）。

    判别（保守，绝不清掉真实校名）：
      1. `东`/`广`/`省` 为词首候选渗字；
      2. 删除后得到的「去括号 base」**本身是已知真实校名**（在 known 集合中）→ 删；
      3. 或删首字后以 _LEADING_SEEP_TARGETS 特征开头 → 删；
      4. 保护 ``_EAST_LEGIT`` 中的真实 `东 X` 校名；
      5. 否则保留。
    """
    if not name:
        return name
    for _ in range(2):  # 最多删 2 个前导渗字
        if len(name) < 3:
            break
        head, rest = name[0], name[1:]
        if head not in AMBIG_SEEP:
            break
        # 保护真实 `东 X` 校名
        if head == "东" and any(name.startswith(p) for p in _EAST_LEGIT):
            break
        rest_base = re.split(r"[（(]", rest, maxsplit=1)[0]
        if known and (rest in known or rest_base in known):
            name = rest
            continue
        if any(rest.startswith(t) for t in _LEADING_SEEP_TARGETS):
            name = rest
            continue
        break
    return name


def parse_admission_pdf(pdf_path: Path, year: int, subject: str) -> list[dict[str, object]]:
    """解析单份投档 PDF → 行列表。"""
    rows: list[dict[str, object]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for raw in table:
                if len(raw) < 7:
                    continue
                code = clean_seep_number(raw[C_CODE])
                if code is None:
                    continue  # 表头
                name = clean_seep_text(raw[C_NAME])  # 只去安全渗字；歧义词首渗字留给 known-set 阶段
                group = clean_seep_number(raw[C_GROUP])
                if not name or group is None:
                    continue
                rows.append({
                    "year": year,
                    "province": PROVINCE,
                    "subject_type": subject,
                    "university_code": code,
                    "university_name": name,
                    "group_code": group,
                    "plan_count": clean_seep_number(raw[C_PLAN]),
                    "admitted_count": clean_seep_number(raw[C_ADMIT]),
                    "min_score": clean_seep_number(raw[C_SCORE]),
                    "min_rank": clean_seep_number(raw[C_RANK]),
                })
    return rows


def build_subject_requirement(subject: str) -> str:
    """构造选科要求 JSON。

    - first：由数据来源确定（广东分物理/历史公布 → 首选科目即对应科目）；
    - second：无来源 → 空 + status=待补（绝不猜测）。
    """
    return (
        '{"mode":"3+1+2","first":["' + subject + '"],"second":[],'
        '"pattern":"none","status":"待补","raw":""}'
    )


def base_name_of(name: str) -> str:
    """提取基础校名：去掉所有括号后缀（含嵌套）。

    防御：PDF 截断可能产生**不闭合括号**（如 `佛山大学(原佛山科学技术学院)(地方专`），
    朴素的 while 循环会永不终止 —— 这里限制最大迭代次数。
    """
    result = name
    for _ in range(8):  # 括号嵌套深度不可能超过 8
        stripped = re.sub(r"[（(][^（）()]*[）)]", "", result)
        if stripped == result:
            break
        result = stripped
    # 残余不闭合括号：截断到第一个左括号前（保底，避免污染 base_name）
    result = re.split(r"[（(]", result, maxsplit=1)[0]
    return result.strip() or name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="院校库与投档线清洗（广东 2024-2026）")
    parser.add_argument("--year", type=int, choices=YEARS, help="只处理指定年份")
    args = parser.parse_args(argv)

    years = [args.year] if args.year else YEARS
    all_rows: list[dict[str, object]] = []
    stats: list[tuple[int, str, int, int]] = []  # year, subject, rows, empty_score

    for year in years:
        for subject in SUBJECTS:
            pdf = locate_pdfs(year, subject)
            if pdf is None:
                print(f"  ✗ 未找到 {year} {subject} 投档 PDF")
                continue
            print(f"  · {year} {subject} ← {pdf.name}")
            rows = parse_admission_cached(pdf, year, subject)
            for r in rows:
                r["subject_requirement"] = build_subject_requirement(subject)
            empty = sum(1 for r in rows if r["min_score"] is None or r["min_rank"] is None)
            all_rows.extend(rows)
            stats.append((year, subject, len(rows), empty))

    # ---- 词首歧义渗字二次清洗（需要全量校名做 known-set 判别）----
    # 第一轮：收集「无词首渗字怀疑」的规范名，作为已知真实校名集合
    raw_names = [str(r["university_name"]) for r in all_rows]
    known = make_known_name_set(raw_names)
    # 第二轮：对每个名字尝试删词首渗字，删后命中 known 才生效
    fixed = 0
    for r in all_rows:
        before = str(r["university_name"])
        after = strip_leading_seep(before, known)
        if after != before:
            r["university_name"] = after
            fixed += 1
    print(f"  · 词首渗字修正 {fixed} 行")

    # ---- 去重（同唯一键只留一条；跨页重复）----
    dedup: dict[tuple, dict[str, object]] = {}
    for r in all_rows:
        key = (r["year"], r["subject_type"], r["university_code"], r["group_code"])
        dedup.setdefault(key, r)
    rows_final = list(dedup.values())
    rows_final.sort(key=lambda r: (r["year"], r["subject_type"], r["university_code"], r["group_code"]))

    # ---- 写出 admission_lines.csv ----
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "year", "province", "subject_type", "university_code", "university_name",
        "group_code", "plan_count", "admitted_count", "min_score", "min_rank",
        "subject_requirement",
    ]
    with OUT_ADMISSION.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows_final:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})

    # ---- 汇总院校库 ----
    summary = build_universities(rows_final)

    # ---- 报告 ----
    print("=" * 66)
    print(f"投档线清洗完成 → {OUT_ADMISSION.relative_to(PROJECT_ROOT)}")
    print("=" * 66)
    print(f"{'年份':<6}{'科类':<6}{'行数':<8}{'空投档(官方-)':<14}")
    for year, subject, n, empty in stats:
        print(f"{year:<6}{subject:<6}{n:<8}{empty:<14}")
    print("-" * 66)
    print(f"去重后合计 {len(rows_final)} 条（原始 {len(all_rows)}）")
    print(f"院校库 {summary['total']} 所（已核 {summary['annotated']} / 待补 {summary['pending']}）")
    print("✅ 完成")
    return 0


def build_universities(rows: list[dict[str, object]]) -> dict[str, int]:
    """从投档行汇总院校库（无官方元数据源，标签留待 annotate 脚本补全）。"""
    # 复用已有 universities.csv 的元数据（若存在），避免丢失人工核定结果
    existing: dict[int, dict[str, str]] = {}
    if OUT_UNIVERSITIES.exists():
        with OUT_UNIVERSITIES.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    existing[int(r["university_code"])] = r
                except (KeyError, ValueError):
                    continue

    agg: dict[int, dict[str, object]] = {}
    known = make_known_name_set([str(r["university_name"]) for r in rows])
    for r in rows:
        code = int(r["university_code"])
        name = strip_leading_seep(str(r["university_name"]), known)
        entry = agg.setdefault(code, {"names": Counter(), "base": base_name_of(name)})
        entry["names"][name] += 1  # type: ignore[index]

    out_rows: list[dict[str, str]] = []
    for code, info in sorted(agg.items()):
        names: Counter = info["names"]  # type: ignore[assignment]
        variants = "|".join(sorted(names.keys()))
        # 规范名：优先无括号后缀的纯名；否则取频次最高
        plain = [n for n in names if "(" not in str(n) and "（" not in str(n)]
        if plain:
            canonical = max(plain, key=lambda n: names[n])
        else:
            canonical = names.most_common(1)[0][0]
        old = existing.get(code, {})
        out_rows.append({
            "university_code": str(code),
            "university_name": canonical,
            "base_name": str(info["base"]),
            "level": old.get("level", "本科"),
            "province": old.get("province", ""),
            "school_type": old.get("school_type", ""),
            "tags": old.get("tags", ""),
            "name_variants": variants,
            "meta_status": old.get("meta_status", "待补"),
        })

    fields = [
        "university_code", "university_name", "base_name", "level", "province",
        "school_type", "tags", "name_variants", "meta_status",
    ]
    with OUT_UNIVERSITIES.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    annotated = sum(1 for r in out_rows if r["meta_status"] == "已核")
    return {"total": len(out_rows), "annotated": annotated, "pending": len(out_rows) - annotated}


if __name__ == "__main__":
    sys.exit(main())
