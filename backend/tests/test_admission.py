"""T03 测试 —— 院校库与投档线清洗（对应任务卡「测试场景」J1–J6）。

J1 投档线条数与原始一致（15868），唯一键不重复
J2 三年 × 两科类齐备，空投档行（官方「-」）计数与口径一致（12）
J3 院校代码唯一；投档线院校代码均可关联院校库
J4 渗字处理：数值列取最后数字；名称列无「广东省教育考试院」残留；真实校名受保护
J5 选科要求 JSON 结构合法；first 与科类一致；second 空且 status=待补
J6 base_name 提取不死循环；PDF 截断名不污染 base
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from annotate_university_meta import (  # noqa: E402
    TAG_211,
    TAG_985,
    TAG_SYL,
    match_tags,
)
from clean_admission import (  # noqa: E402
    base_name_of,
    build_subject_requirement,
    clean_seep_number,
    make_known_name_set,
    strip_leading_seep,
)

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"
ADMISSION_CSV = CLEANED / "admission_lines.csv"
UNIVERSITIES_CSV = CLEANED / "universities.csv"
SCORE_CSV = CLEANED / "score_segments.csv"

EXPECTED_YEARS = [2024, 2025, 2026]
EXPECTED_SUBJECTS = ["物理", "历史"]
EXPECTED_TOTAL = 15868
EXPECTED_EMPTY_SCORE = 12

# 「广东省教育考试院」全字集（渗字来源）
SEEP_CHARS = set("广东省教育考试院")


# ==================== 解析函数单测（J4 的基础）====================


def test_clean_seep_number_takes_last_digit_run() -> None:
    """数值列渗字：取最后一个连续数字串（与 T02 一分一段一致）。"""
    assert clean_seep_number("院10001") == 10001
    assert clean_seep_number("东509") == 509
    assert clean_seep_number("考\n20") == 20
    assert clean_seep_number("3121") == 3121
    assert clean_seep_number("") is None
    assert clean_seep_number(None) is None
    assert clean_seep_number("—") is None


def test_base_name_no_infinite_loop_on_unclosed_paren() -> None:
    """J6：PDF 截断产生不闭合括号时，base_name_of 不得死循环。"""
    # 右括号被截断
    assert base_name_of("佛山大学(原佛山科学技术学院)(地方专") == "佛山大学"
    assert base_name_of("广东外语外贸大学南国商学院(联合培养") == "广东外语外贸大学南国商学院"
    # 正常嵌套
    assert base_name_of("哈尔滨工业大学(深圳)") == "哈尔滨工业大学"
    assert base_name_of("中国人民大学(苏州校区)(中外合作办学)") == "中国人民大学"
    # 无括号
    assert base_name_of("北京大学") == "北京大学"


def test_strip_leading_seep_removes_prefix_but_protects_real_names() -> None:
    """J4：词首渗字删除；真实「东/广/省」开头校名必须保护。"""
    names = [
        "中国人民大学", "北京师范大学", "中国传媒大学", "佛山大学",
        "广东工业大学", "东北大学", "东南大学", "广州大学", "广西大学",
        "东莞理工学院", "东华大学", "广东东软学院", "桂林医学院",
        "三亚学院", "广东金融学院", "韶关学院",
    ]
    known = make_known_name_set(names)

    # 应删除（渗字）
    assert strip_leading_seep("东中国人民大学", known) == "中国人民大学"
    assert strip_leading_seep("省北京师范大学", known) == "北京师范大学"
    assert strip_leading_seep("东中国传媒大学", known) == "中国传媒大学"
    assert strip_leading_seep("省佛山大学", known) == "佛山大学"
    assert strip_leading_seep("省桂林医学院", known) == "桂林医学院"
    assert strip_leading_seep("教三亚学院", known) == "三亚学院"
    assert strip_leading_seep("考广东金融学院", known) == "广东金融学院"

    # 应保护（真实校名）
    for n in ["广东工业大学", "东北大学", "东南大学", "广州大学",
              "广西大学", "东莞理工学院", "东华大学", "广东东软学院"]:
        assert strip_leading_seep(n, known) == n, f"{n} 被误删"


def test_build_subject_requirement_structure() -> None:
    """J5：JSON 结构合法；first=科类；second 空；status=待补。"""
    for subject in EXPECTED_SUBJECTS:
        obj = json.loads(build_subject_requirement(subject))
        assert obj["mode"] == "3+1+2"
        assert obj["first"] == [subject]
        assert obj["second"] == []
        assert obj["pattern"] == "none"
        assert obj["status"] == "待补"
        assert obj["raw"] == ""


def test_match_tags_official_lists() -> None:
    """985/211 官方名单匹配（以基础校名）。"""
    assert match_tags("清华大学") == "985|211|双一流"
    assert match_tags("北京大学") == "985|211|双一流"
    assert "211" in match_tags("北京交通大学")
    assert "985" not in match_tags("北京交通大学")
    assert match_tags("某不知名学院") == ""


# ==================== 数据文件校验（J1–J3、J5）====================


@pytest.fixture(scope="module")
def admission_rows() -> list[dict[str, str]]:
    assert ADMISSION_CSV.exists(), f"缺少 {ADMISSION_CSV}，请先运行 clean_admission.py"
    with ADMISSION_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def university_rows() -> list[dict[str, str]]:
    assert UNIVERSITIES_CSV.exists(), f"缺少 {UNIVERSITIES_CSV}"
    with UNIVERSITIES_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def score_rows() -> list[dict[str, str]]:
    with SCORE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_j1_total_rows(admission_rows) -> None:
    """J1：投档线条数与原始一致，唯一键不重复。"""
    assert len(admission_rows) == EXPECTED_TOTAL, f"行数 {len(admission_rows)} ≠ {EXPECTED_TOTAL}"
    seen = set()
    for r in admission_rows:
        key = (r["year"], r["subject_type"], r["university_code"], r["group_code"])
        assert key not in seen, f"重复唯一键：{key}"
        seen.add(key)


def test_j2_years_subjects_and_empty_rows(admission_rows) -> None:
    """J2：三年两科类齐备；空投档行计数与口径一致。"""
    groups: dict[tuple[str, str], int] = defaultdict(int)
    empty = 0
    for r in admission_rows:
        groups[(r["year"], r["subject_type"])] += 1
        if not r["min_score"] or not r["min_rank"]:
            empty += 1
    for year in EXPECTED_YEARS:
        for subject in EXPECTED_SUBJECTS:
            assert (str(year), subject) in groups, f"缺少 {year} {subject}"
            assert groups[(str(year), subject)] > 1000
    assert empty == EXPECTED_EMPTY_SCORE, f"空投档行 {empty} ≠ {EXPECTED_EMPTY_SCORE}"


def test_j3_university_codes_unique_and_linked(admission_rows, university_rows) -> None:
    """J3：院校代码唯一；投档线院校代码均可关联院校库。"""
    codes = [r["university_code"] for r in university_rows]
    assert len(codes) == len(set(codes)), "院校库存在重复代码"

    uni_codes = set(codes)
    orphans = {r["university_code"] for r in admission_rows} - uni_codes
    assert not orphans, f"投档线存在孤立院校代码：{sorted(orphans)[:5]}"


def test_j3_subject_type_enum(admission_rows) -> None:
    """subject_type 只能是 物理/历史。"""
    bad = [r for r in admission_rows if r["subject_type"] not in EXPECTED_SUBJECTS]
    assert not bad, f"非法科类 {len(bad)} 行"


def test_j4_no_seep_residue_in_names(admission_rows) -> None:
    """J4：名称列无「考试院」渗字残留，且无「删首字后即真实校名」的渗字。"""
    # 直接渗字残留
    for r in admission_rows:
        n = r["university_name"]
        assert "考试院" not in n, f"残留渗字：{n}"
        assert not n.startswith(("省教育", "教育考")), f"残留渗字：{n}"

    # 词首渗字残留：首字 ∈ 渗字集 且 去掉后是另一条真实校名
    names = {r["university_name"] for r in admission_rows}
    import re
    for n in names:
        if not n or n[0] not in SEEP_CHARS:
            continue
        rest = n[1:]
        rest_base = re.split(r"[（(]", rest, maxsplit=1)[0]
        assert not (rest in names or rest_base in names), f"疑似词首渗字残留：{n}"


def test_j4_real_names_protected(admission_rows) -> None:
    """J4：真实校名受保护（不得被误删首字）。"""
    names = {r["university_name"] for r in admission_rows}
    for n in ["东北大学", "广东工业大学", "广州大学", "广西大学",
              "东南大学", "东莞理工学院", "东华大学", "广东外语外贸大学"]:
        assert n in names, f"真实校名被误删：{n}"


def test_j5_subject_requirement_in_csv(admission_rows) -> None:
    """J5：CSV 内每行选科要求结构合法、first 与科类一致。"""
    for r in admission_rows:
        raw = r.get("subject_requirement", "")
        assert raw, "选科要求缺失"
        obj = json.loads(raw)
        assert obj["mode"] == "3+1+2"
        assert obj["first"] == [r["subject_type"]], f"first 与科类不一致：{r}"
        assert obj["second"] == []
        assert obj["status"] == "待补"


def test_j5_rank_in_valid_range(admission_rows, score_rows) -> None:
    """投档排位落在该分数的合法区间（含高于表首聚合段的特例）。"""
    seg: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in score_rows:
        seg[(r["year"], r["subject_type"])][int(r["score"])] = int(r["rank"])

    for r in admission_rows:
        if not r["min_score"] or not r["min_rank"]:
            continue
        score, rank = int(r["min_score"]), int(r["min_rank"])
        table = seg[(r["year"], r["subject_type"])]
        top = max(table)
        if score > top:
            assert 1 <= rank <= table[top], f"高于表首越界：{r}"
            continue
        upper = table[score]
        higher = [table[s] for s in table if s > score]
        lower = (min(higher) + 1) if higher else 1
        assert lower <= rank <= upper, f"越界：{r['year']}/{r['subject_type']}/{r['university_code']}"


def test_j6_university_meta_valid(university_rows) -> None:
    """院校库字段枚举合法、必填完整。"""
    levels = {"本科", "专科"}
    for r in university_rows:
        assert r["university_code"] and r["university_name"] and r["base_name"]
        assert r["level"] in levels
        assert r["meta_status"] in {"已核", "待补"}
        for t in filter(None, (r["tags"] or "").split("|")):
            assert t in {"985", "211", "双一流"}, f"非法 tag: {t}"


def test_j6_985_211_counts_meet_official(university_rows) -> None:
    """985/211/双一流 计数达官方名单下限。"""
    tag_sets = [set((r["tags"] or "").split("|")) for r in university_rows]
    n985 = sum(1 for s in tag_sets if "985" in s)
    n211 = sum(1 for s in tag_sets if "211" in s)
    nsyl = sum(1 for s in tag_sets if "双一流" in s)
    assert n985 >= len(TAG_985) == 39, f"985 计数 {n985}"
    assert n211 >= len(TAG_211) >= 112, f"211 计数 {n211}"
    assert nsyl >= len(TAG_SYL) >= 147, f"双一流 计数 {nsyl}"
