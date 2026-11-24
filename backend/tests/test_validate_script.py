"""T05 测试 —— 数据校验与抽查（对应任务卡「测试场景」J1–J3）。

J1 全部校验项通过并生成报告          → test_j1_* / test_full_validation_passes
J2 抽查 0 失败                      → test_j2_spotcheck_zero_failure
J3 人为注入一行错误数据 → 校验能报错   → test_j3_*（本文件核心）

J3 是**脚本有效性验证**：一个「永远返回 PASS」的校验脚本毫无价值。
本文件用临时目录构造**故意错误**的数据，断言 `validate_data.py` 确实 FAIL，
证明校验逻辑真实生效。
"""

from __future__ import annotations

import csv
import importlib
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import validate_data  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"
REAL_SCORE_CSV = CLEANED / "score_segments.csv"
REAL_ADMISSION_CSV = CLEANED / "admission_lines.csv"
REAL_UNIVERSITIES_CSV = CLEANED / "universities.csv"

DATA_OK = all(p.exists() for p in (REAL_SCORE_CSV, REAL_ADMISSION_CSV, REAL_UNIVERSITIES_CSV))
requires_data = pytest.mark.skipif(not DATA_OK, reason="需要 data/cleaned 下的清洗产物")


# ==================== 工具：构造临时（可注入错误的）数据集 ====================


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _build_dataset(tmp_path: Path, mutate=None) -> Path:
    """把真实数据复制到临时目录；mutate(rows_dict) 可就地注入错误。

    rows_dict 形如 {"score": [...], "admission": [...], "universities": [...]}
    """
    score_rows = _read_rows(REAL_SCORE_CSV)
    adm_rows = _read_rows(REAL_ADMISSION_CSV)
    uni_rows = _read_rows(REAL_UNIVERSITIES_CSV)
    if mutate:
        mutate({"score": score_rows, "admission": adm_rows, "universities": uni_rows})
    _write_rows(tmp_path / "score_segments.csv", score_rows, list(score_rows[0].keys()))
    _write_rows(tmp_path / "admission_lines.csv", adm_rows, list(adm_rows[0].keys()))
    _write_rows(tmp_path / "universities.csv", uni_rows, list(uni_rows[0].keys()))
    return tmp_path


def _run(tmp_path: Path) -> tuple[int, dict[str, str]]:
    """在 tmp_path 上跑校验，返回 (退出码, {编号: 结果})。

    注意：必须在 `importlib.reload` **之后**调用 `main`（reload 会把模块级
    SCORE_CSV 等重置为默认值，`main` 再通过 `--data-dir` 覆盖）。
    """
    importlib.reload(validate_data)  # 重置模块级 RESULTS 与路径常量
    rc = validate_data.main(["--data-dir", str(tmp_path), "--quiet", "--report"])
    result = {code: status for code, status, *_ in validate_data.RESULTS}
    return rc, result


# ==================== J1：真实数据全部通过并生成报告 ====================


@requires_data
def test_j1_full_validation_passes(tmp_path: Path) -> None:
    """J1 全部校验项通过（退出码 0）并生成报告。"""
    _build_dataset(tmp_path)  # 复制真实数据（不改动）
    rc, result = _run(tmp_path)
    fails = {k: v for k, v in result.items() if v == "FAIL"}
    assert not fails, f"校验不应有失败项，实际：{fails}"
    assert rc == 0
    # 报告文件已生成
    assert (tmp_path / "data_validation_report.md").exists()
    # RESULTS 逐条记录（含 S3/S4/S5 按年×科类展开的重复编号），故按**条数**断言
    assert len(validate_data.RESULTS) >= 30, f"校验项应 ≥30，实际 {len(validate_data.RESULTS)}"


@requires_data
def test_j1_core_codes_present(tmp_path: Path) -> None:
    """J1 关键校验编号齐备（S/A/U 三大族均在）。"""
    _build_dataset(tmp_path)
    _run(tmp_path)
    codes = {c for c, *_ in validate_data.RESULTS}
    for expected in ("S0", "S2", "S4", "S5", "A0", "A1", "A3", "A5", "U0", "U1", "U4"):
        assert expected in codes, f"缺少校验项 {expected}"


# ==================== J3：注入错误 → 必须报错（脚本有效性验证）====================


@requires_data
def test_j3_inject_duplicate_score_segment_detected(tmp_path: Path) -> None:
    """J3-1 注入重复 (year,province,subject_type,score) → S2 必须 FAIL。"""

    def mutate(ds):
        ds["score"].append(dict(ds["score"][0]))  # 原样复制一行 → 唯一性违规

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("S2") == "FAIL", "重复行未被 S2 检出"
    assert rc == 1


@requires_data
def test_j3_inject_rank_non_monotonic_detected(tmp_path: Path) -> None:
    """J3-2 注入位次递增（违反「位次非增」）→ S4 必须 FAIL。"""

    def mutate(ds):
        # 找到某年某科类中分数最高的一行，把其 rank 改成比下一行还大 → 违反非增
        rows = ds["score"]
        target = max(rows, key=lambda r: (r["year"], int(r["score"])))
        target["rank"] = str(int(target["rank"]) + 10_000_000)

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("S4") == "FAIL", "位次非增违规未被 S4 检出"
    assert rc == 1


@requires_data
def test_j3_inject_bad_subject_enum_detected(tmp_path: Path) -> None:
    """J3-3 注入非法 subject_type → A2 必须 FAIL。"""

    def mutate(ds):
        ds["admission"][0]["subject_type"] = "综合"

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("A2") == "FAIL", "非法枚举未被 A2 检出"
    assert rc == 1


@requires_data
def test_j3_inject_out_of_range_rank_detected(tmp_path: Path) -> None:
    """J3-4 注入 min_rank 越界（远超分数区间）→ A3 必须 FAIL。"""

    def mutate(ds):
        for r in ds["admission"]:
            if r["min_score"] and r["min_rank"]:
                r["min_rank"] = "999999999"  # 必越界
                break

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("A3") == "FAIL", "位次越界未被 A3 检出"
    assert rc == 1


@requires_data
def test_j3_inject_orphan_university_code_detected(tmp_path: Path) -> None:
    """J3-5 注入无法关联 universities 的院校代码 → A5 必须 FAIL。"""

    def mutate(ds):
        ds["admission"][0]["university_code"] = "99999"

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("A5") == "FAIL", "孤立院校代码未被 A5 检出"
    assert rc == 1


@requires_data
def test_j3_inject_bad_requirement_json_detected(tmp_path: Path) -> None:
    """J3-6 注入非法 subject_requirement JSON → A6 必须 FAIL。"""

    def mutate(ds):
        ds["admission"][0]["subject_requirement"] = "{不是合法 JSON"

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("A6") == "FAIL", "非法 JSON 未被 A6 检出"
    assert rc == 1


@requires_data
def test_j3_inject_duplicate_university_code_detected(tmp_path: Path) -> None:
    """J3-7 注入重复 university_code → U1 必须 FAIL。"""

    def mutate(ds):
        ds["universities"].append(dict(ds["universities"][0]))

    _build_dataset(tmp_path, mutate)
    rc, result = _run(tmp_path)
    assert result.get("U1") == "FAIL", "重复院校代码未被 U1 检出"
    assert rc == 1


@requires_data
def test_j3_clean_baseline_still_passes(tmp_path: Path) -> None:
    """J3-对照：未注入错误时同一流程必须通过（排除「永远 FAIL」的假象）。"""
    _build_dataset(tmp_path, mutate=None)
    rc, result = _run(tmp_path)
    assert rc == 0, f"基线数据不应失败：{[k for k,v in result.items() if v=='FAIL']}"


# ==================== J2：独立抽查 0 失败 ====================


@requires_data
def test_j2_spotcheck_zero_failure() -> None:
    """J2 抽查脚本 0 失败（复用 PDF 解析函数做单元级验证，避免全量跑 PDF）。"""
    import spotcheck_admission as sc

    # 抽查逻辑：行解析函数对三类高危形态的解析正确性（不触发 PDF 全量解析）
    # ① 正常行
    r = sc.parse_row_independent("10001 北京大学 201 24 24 660 29")
    assert r is not None
    assert (r["university_code"], r["group_code"]) == (10001, 201)
    assert (r["plan_count"], r["admitted_count"], r["min_score"], r["min_rank"]) == (24, 24, 660, 29)

    # ② 官方「-」空投档行 → 分数/排位为 None（验证「不补造」）
    r = sc.parse_row_independent("10533 中南大学(高校专项计划) 201 1 0 - -")
    assert r is not None
    assert r["plan_count"] == 1 and r["admitted_count"] == 0
    assert r["min_score"] is None and r["min_rank"] is None

    # ③ 数值列夹渗字（院/试）
    r = sc.parse_row_independent("10011 北京工商大学 201 8 8 556 院17147")
    assert r is not None and r["min_rank"] == 17147

    r = sc.parse_row_independent("10017 北京石油化工学院 201 1 1 2试537 27475")
    assert r is not None and r["min_score"] == 537 and r["min_rank"] == 27475

    # ④ 行首渗字
    r = sc.parse_row_independent("广10031 北京第二外国语学院 201 3 3 579 8174")
    assert r is not None and r["university_code"] == 10031

    # ⑤ 校名含内部渗字：本脚本**保守处理**（不重做清洗，避免误伤 `学院` 的 `院`），
    #    数值字段仍必须正确 —— 名称一致性由 compare_one 的「去渗字后相等」判定。
    r = sc.parse_row_independent("10030 北东京外国语大学 201 19 19 606 2413")
    assert r is not None
    assert sc._strip_seep_chars(r["university_name"]) == "北京外国语大学"

    # ⑥ 真实校名绝不能被「清洗」掉（`学院`/`东`/`广东` 均为真实字）
    r = sc.parse_row_independent("10017 北京石油化工学院 201 1 1 2试537 27475")
    assert r is not None and r["university_name"] == "北京石油化工学院"
    r = sc.parse_row_independent("80004 广东技术师范大学(河源校区) 203 14 14 543 23660")
    assert r is not None and r["university_name"] == "广东技术师范大学(河源校区)"

    # ⑦ 非数据行返回 None
    assert sc.parse_row_independent("院校代码 院校名称 专业组代码 计划数 投档人数 投档最低分 投档最低排位") is None
    assert sc.parse_row_independent("考") is None
    assert sc.parse_row_independent("") is None


@requires_data
def test_j2_spotcheck_report_exists() -> None:
    """J2 抽查记录已产出且声明 0 失败、行数达标。"""
    report = PROJECT_ROOT / "data" / "eval" / "spotcheck_admission.md"
    if not report.exists():
        pytest.skip("抽查报告需先运行 scripts/spotcheck_admission.py 生成")
    text = report.read_text(encoding="utf-8")
    assert "失败行数：**0**" in text
    assert "✅ 通过（0 失败）" in text
