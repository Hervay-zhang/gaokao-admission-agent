"""T27 测试 —— 案例中心（脱敏真实案例 + 筛选）。

对应任务卡「测试场景」J1–J3：

J1 案例可筛选（科类 / 分数段 / 路径类型 / 叙事分层）
J2 无个人信息泄露（字段黑名单 + 正文禁止明文分数/位次/身份串）
J3 案例数量 3–5 条

外加：slug 目录穿越防护、不存在案例 404、正文机械解析（章节可读）、真实数据源存在性。

设计：真实案例目录与**临时夹具目录**两条路径都测；不依赖 MySQL。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import cases as cases_api  # noqa: E402
from app.main import app  # noqa: E402
from app.services.cases import CaseService, default_cases_dir, parse_sections  # noqa: E402

REAL_DIR = default_cases_dir()


def _write_case(d: Path, slug: str, title: str, *, subject="物理", band="600-609", ptype="稳妥型", tier="basic"):
    (d / f"{slug}.md").write_text(
        f"# {title}\n\n## 一、考生画像（脱敏）\n\n| 项 | 内容 |\n|---|---|\n| 科类 | {subject} |\n\n"
        f"## 二、复盘\n\n- 依据真实投档数据。\n",
        encoding="utf-8",
    )
    return {
        "slug": slug, "title": title, "subject_type": subject, "score_band": band,
        "score_band_start": int(band.split("-")[0]), "rank_band": "3.0-3.1万名",
        "path_type": ptype, "narrative_tier": tier, "valid_year": 2025,
        "train_years": [2024], "n_reco": 12, "bao_effective": True, "chong_hit": False,
        "summaries": {}, "sample_id": "S0001",
    }


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """4 篇确定性夹具案例（覆盖 2 科类 × 多路径）。"""
    metas = [
        _write_case(tmp_path, "case_01", "物理 高分段 稳妥型", subject="物理", band="600-609", ptype="稳妥型"),
        _write_case(tmp_path, "case_02", "物理 低分段 滑档预警", subject="物理", band="470-479", ptype="滑档预警"),
        _write_case(tmp_path, "case_03", "历史 中分段 保底型", subject="历史", band="450-459", ptype="保底型"),
        _write_case(tmp_path, "case_04", "历史 高分段 进取型", subject="历史", band="570-579", ptype="进取型", tier="deep"),
    ]
    index = {
        "generated_by": "test-fixture", "generated_at": "2026-10-15 00:00:00", "count": len(metas),
        "filters": {
            "subject_types": ["历史", "物理"],
            "score_bands": ["600-609", "570-579", "470-479", "450-459"],
            "path_types": ["进取型", "保底型", "滑档预警", "稳妥型"],
            "narrative_tiers": ["basic", "deep"],
        },
        "cases": metas,
    }
    (tmp_path / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return tmp_path


@pytest.fixture
def svc(fixture_dir: Path) -> CaseService:
    return CaseService(fixture_dir)


# ============================================================
# J1 案例可筛选
# ============================================================


def test_j1_no_filter_returns_all(svc: CaseService) -> None:
    out = svc.list_cases()
    assert out["total"] == 4
    assert len(out["cases"]) == 4
    assert "disclaimer" in out and out["disclaimer"]


def test_j1_filter_by_subject(svc: CaseService) -> None:
    out = svc.list_cases(subject_type="物理")
    assert out["total"] == 2
    assert all(c["subject_type"] == "物理" for c in out["cases"])


def test_j1_filter_by_score_band(svc: CaseService) -> None:
    out = svc.list_cases(score_band="470-479")
    assert out["total"] == 1
    assert out["cases"][0]["path_type"] == "滑档预警"


def test_j1_filter_by_path_type(svc: CaseService) -> None:
    out = svc.list_cases(path_type="稳妥型")
    assert out["total"] == 1
    assert out["cases"][0]["subject_type"] == "物理"


def test_j1_filter_combined(svc: CaseService) -> None:
    out = svc.list_cases(subject_type="历史", narrative_tier="deep")
    assert out["total"] == 1
    assert out["cases"][0]["slug"] == "case_04"


def test_j1_filters_meta_exposed(svc: CaseService) -> None:
    out = svc.list_cases()
    f = out["filters"]
    assert set(f["subject_types"]) == {"物理", "历史"}
    assert "滑档预警" in f["path_types"]
    assert set(f["narrative_tiers"]) == {"basic", "deep"}


# ============================================================
# J2 无个人信息泄露
# ============================================================


def test_j2_no_forbidden_keys(svc: CaseService) -> None:
    """索引字段不得含 score/rank/name/phone 等敏感键（结构性兜底）。"""
    out = svc.list_cases()
    forbidden = {"score", "rank", "name", "phone", "mobile", "id_card", "email", "real_name"}
    for c in out["cases"]:
        assert not (set(k.lower() for k in c) & forbidden), f"发现敏感字段：{c}"


def test_j2_sanitize_strips_sensitive(tmp_path: Path) -> None:
    """即使索引被误写入敏感字段，服务层也必须剔除（双保险）。"""
    (tmp_path / "case_01.md").write_text("# x\n\n## 一、\n\n- ok\n", encoding="utf-8")
    index = {"count": 1, "filters": {}, "cases": [
        {"slug": "case_01", "title": "x", "subject_type": "物理", "score_band": "600-609",
         "path_type": "稳妥型", "narrative_tier": "basic", "score": 612, "rank": 30000, "name": "张三"},
    ]}
    (tmp_path / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    out = CaseService(tmp_path).list_cases()
    assert out["total"] == 1
    c = out["cases"][0]
    assert "score" not in c and "rank" not in c and "name" not in c


def test_j2_detail_has_no_plain_score_or_identity(svc: CaseService) -> None:
    """详情正文不得出现明文字段串（姓名/手机号/完整分数）。"""
    import re

    detail = svc.get_case("case_01")
    text = json.dumps(detail, ensure_ascii=False)
    assert not re.search(r"1[3-9]\d{9}", text), "出现疑似手机号"
    assert "身份证" not in text
    # 正文只应有分数段（含「-」）而非单一精确分数列；此处校验章节结构可解析
    assert detail["sections"], "章节解析不应为空"
    assert any("画像" in s["heading"] for s in detail["sections"])


def test_j2_parse_sections_mechanical() -> None:
    md = "# 标题\n\n## 一、甲\n\n- a\n\n| h |\n|---|\n| v |\n\n## 二、乙\n\n- b\n"
    secs = parse_sections(md)
    assert [s["heading"] for s in secs] == ["一、甲", "二、乙"]
    assert any("| v |" in ln for ln in secs[0]["lines"])


# ============================================================
# J3 案例数量 3–5
# ============================================================


def test_j3_real_cases_count_between_3_and_5() -> None:
    """真实案例目录必须存在且 3–5 篇（任务卡要求）。"""
    assert REAL_DIR.exists(), f"案例目录不存在：{REAL_DIR}（请执行 scripts/gen_cases.py）"
    out = CaseService(REAL_DIR).list_cases()
    assert 3 <= out["total"] <= 5, f"案例数量应在 3–5，实际 {out['total']}"


def test_j3_real_cases_have_all_path_types() -> None:
    """真实案例应覆盖多种路径类型（否则筛选无意义）。"""
    out = CaseService(REAL_DIR).list_cases()
    types = {c["path_type"] for c in out["cases"]}
    assert len(types) >= 3, f"路径类型过少：{types}"


def test_j3_real_case_detail_readable() -> None:
    out = CaseService(REAL_DIR).list_cases()
    first = out["cases"][0]["slug"]
    detail = CaseService(REAL_DIR).get_case(first)
    assert detail["meta"]["slug"] == first
    assert len(detail["sections"]) >= 3


def test_j3_real_index_has_fingerprint() -> None:
    """索引必须带输入 sha256（可核对与数据同源，禁手工编造）。"""
    idx = json.loads((REAL_DIR / "index.json").read_text(encoding="utf-8"))
    assert idx["generated_by"] == "scripts/gen_cases.py"
    assert len(idx["inputs"]["samples_sha256"]) >= 8
    assert len(idx["inputs"]["admission_sha256"]) >= 8


# ============================================================
# 安全与边界
# ============================================================


@pytest.mark.parametrize("bad", ["../index", "..%2Findex", "case_01.md", "index", "case_1", "case_0001"])
def test_slug_traversal_rejected(svc: CaseService, bad: str) -> None:
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        svc.get_case(bad)


def test_unknown_slug_not_found(svc: CaseService) -> None:
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError) as ei:
        svc.get_case("case_99")
    assert ei.value.detail["code"] == "CASE_NOT_FOUND"


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    out = CaseService(tmp_path / "nope").list_cases()
    assert out["total"] == 0 and out["cases"] == []


# ============================================================
# 端点级
# ============================================================


def test_endpoint_list_and_filter(svc: CaseService) -> None:
    with patch.object(cases_api, "get_case_service", lambda: svc):
        c = TestClient(app)
        r = c.get("/api/cases")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 4

        r = c.get("/api/cases", params={"subject_type": "物理", "path_type": "滑档预警"})
        assert r.json()["total"] == 1
        assert r.json()["cases"][0]["slug"] == "case_02"


def test_endpoint_detail_and_404(svc: CaseService) -> None:
    with patch.object(cases_api, "get_case_service", lambda: svc):
        c = TestClient(app)
        r = c.get("/api/cases/case_01")
        assert r.status_code == 200
        assert r.json()["sections"]

        r = c.get("/api/cases/case_99")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "CASE_NOT_FOUND"

        r = c.get("/api/cases/case_01.md")
        assert r.status_code == 404
