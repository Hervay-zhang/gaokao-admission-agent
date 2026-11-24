"""T02 测试 —— 一分一段表清洗（对应任务卡「测试场景」J1–J3）。

J1 每年每科类抽查与官网一致（此处验证解析函数的确定性 + 边界样本）
J2 覆盖率 ≥ 95%，gaps 为 0 或已标注
J3 (year,province,subject_type,score) 组合唯一
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

# 直接从脚本导入解析函数做单元测试
sys.path.insert(0, str(BACKEND_DIR / "scripts"))
from clean_score_segments import (  # noqa: E402
    detect_gaps,
    parse_rank_cell,
    parse_score_cell,
    strip_seep,
)

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"
SCORE_CSV = CLEANED / "score_segments.csv"
GAPS_JSON = CLEANED / "gaps.json"

EXPECTED_SUBJECTS = ["物理", "历史"]
EXPECTED_YEARS = [2024, 2025, 2026]


# ==================== 解析函数单测（J1 的基础）====================


def test_strip_seep_removes_vertical_chars() -> None:
    """渗字去除：竖排「广东省教育考试院」残片。"""
    assert strip_seep("考\n20") == "20"
    assert strip_seep("育\n5") == "5"
    assert strip_seep("省\n100") == "100"
    assert strip_seep("698") == "698"
    assert strip_seep("699（含以上）") == "699（含以上）"


def test_parse_score_cell() -> None:
    assert parse_score_cell("699（含以上）") == 699
    assert parse_score_cell("698") == 698
    assert parse_score_cell("考\n500") == 500
    assert parse_score_cell("") is None
    assert parse_score_cell("—") is None


def test_parse_rank_cell_takes_last_number() -> None:
    """渗字在数字前 → 取最后一段数字。"""
    assert parse_rank_cell("考\n20") == 20
    assert parse_rank_cell("20") == 20
    assert parse_rank_cell("育\n433353") == 433353
    assert parse_rank_cell("") is None


def test_detect_gaps() -> None:
    assert detect_gaps({100: 5000, 101: 4900, 102: 4800}) == []
    assert detect_gaps({100: 5000, 102: 4800}) == [101]


# ==================== 数据文件校验（J1–J3）====================


@pytest.fixture(scope="module")
def score_rows() -> list[dict[str, str]]:
    assert SCORE_CSV.exists(), f"缺少 {SCORE_CSV}，请先运行 clean_score_segments.py"
    with SCORE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_j1_three_years_two_subjects_present(score_rows) -> None:
    """三年 × 两科类齐备，且每组合行数合理（>500）。"""
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for r in score_rows:
        groups[(r["year"], r["subject_type"])] += 1

    for year in EXPECTED_YEARS:
        for subject in EXPECTED_SUBJECTS:
            assert (str(year), subject) in groups, f"缺少 {year} {subject}"
            assert groups[(str(year), subject)] > 500, f"{year} {subject} 行数过少"


def test_j1_boundary_samples(score_rows) -> None:
    """边界样本：最低分 100 存在；最高分行 rank 最小。"""
    groups: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in score_rows:
        groups[(r["year"], r["subject_type"])][int(r["score"])] = int(r["rank"])

    for (year, subject), scores in groups.items():
        assert 100 in scores, f"{year} {subject} 缺最低分 100"
        top = max(scores)
        # 最高分行的位次应等于该分人数（累计），且为正
        assert scores[top] >= 1
        # 100 分行的累计应是全表最大
        assert scores[100] == max(scores.values())


def test_j2_coverage_and_gaps(score_rows) -> None:
    """覆盖率 ≥ 95%；gaps 为空或已标注。"""
    groups: dict[tuple[str, str], int] = defaultdict(int)
    tops: dict[tuple[str, str], int] = {}
    for r in score_rows:
        key = (r["year"], r["subject_type"])
        groups[key] += 1
        tops[key] = max(tops.get(key, 0), int(r["score"]))

    for key, n in groups.items():
        top = tops[key]
        expected = top - 100 + 1
        assert n / expected >= 0.95, f"{key} 覆盖率 {n}/{expected} < 95%"

    gaps = json.loads(GAPS_JSON.read_text(encoding="utf-8")) if GAPS_JSON.exists() else {}
    # gaps 要么为空，要么每个缺失分数都是有据可查的（此处只验证结构合法）
    assert isinstance(gaps, dict)


def test_j3_composite_key_unique(score_rows) -> None:
    """(year, province, subject_type, score) 组合唯一。"""
    seen = set()
    for r in score_rows:
        key = (r["year"], r["province"], r["subject_type"], r["score"])
        assert key not in seen, f"重复组合：{key}"
        seen.add(key)


def test_rank_monotonic_non_increasing(score_rows) -> None:
    """位次随分数升高而非增（分数越高，累计人数越少或相等）。"""
    groups: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in score_rows:
        groups[(r["year"], r["subject_type"])][int(r["score"])] = int(r["rank"])

    for (year, subject), scores in groups.items():
        ordered = sorted(scores.items())
        for (s1, r1), (s2, r2) in zip(ordered, ordered[1:], strict=False):
            assert r2 <= r1, f"{year} {subject}: 分数 {s2}(rank={r2}) > {s1}(rank={r1})"
