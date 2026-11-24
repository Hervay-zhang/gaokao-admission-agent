"""T24 测试 —— 院校库检索 / 详情 / 对比接口。

对应任务卡「测试场景（必做）」（后端侧）：
    J1 层次+地域筛选正确（前端进 URL 由前端保证，后端验证筛选结果）；
    J2 关键词搜索命中名称/代码；
    J3 详情返回三年专业组数据（含缺年标注）；
    J4 对比 2–3 所数据正确；超 3 所被拦。

设计：**不依赖 MySQL** —— InMemorySchoolRepository 注入，离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import schools as schools_api  # noqa: E402
from app.main import app  # noqa: E402
from app.services.schools import AdmRow, InMemorySchoolRepository, SchoolService, UniRow  # noqa: E402


def _uni(code, name, base=None, level="本科", province="北京", stype="综合", tags=None, meta="已核"):
    return UniRow(code=code, name=name, base_name=base or name, level=level,
                  province=province, school_type=stype, tags=tags or [], meta_status=meta)


def _adm(year, code, name, group, *, prov="广东", st="物理", mrank=None, mscore=None, plan=10, req=None):
    return AdmRow(year=year, province=prov, subject_type=st, university_code=code,
                  university_name=name, group_code=group, plan_count=plan, admitted_count=plan,
                  min_score=mscore, min_rank=mrank, requirement=req or {})


@pytest.fixture
def repo() -> InMemorySchoolRepository:
    unis = [
        _uni("10001", "北京大学", province="北京", tags=["985", "211", "双一流"]),
        _uni("10002", "中国人民大学", province="北京", tags=["985", "211", "双一流"]),
        _uni("10003", "清华大学", province="北京", tags=["985", "211", "双一流"]),
        _uni("10213", "哈尔滨工业大学", base="哈尔滨工业大学", province="黑龙江",
             tags=["985", "211", "双一流"], stype="理工"),
        _uni("18213", "哈尔滨工业大学（深圳）", base="哈尔滨工业大学", province="广东",
             tags=["985", "211", "双一流"], stype="理工"),
        _uni("99999", "某某学院", province="广东", tags=["普通本科"], stype="综合", meta="待补"),
    ]
    adm = [
        _adm(2024, "10001", "北京大学", 201, st="物理", mrank=32, mscore=690),
        _adm(2025, "10001", "北京大学", 201, st="物理", mrank=30, mscore=692),
        _adm(2026, "10001", "北京大学", 201, st="物理", mrank=28, mscore=694),
        _adm(2024, "10001", "北京大学", 202, st="物理", mrank=50, mscore=688),
        _adm(2025, "10001", "北京大学", 202, st="物理", mrank=48, mscore=689),
        _adm(2026, "10001", "北京大学", 202, st="物理", mrank=45, mscore=691),
        # 清华：只 2024/2025（缺 2026 → 详情应标注）
        _adm(2024, "10003", "清华大学", 101, st="物理", mrank=20, mscore=695),
        _adm(2025, "10003", "清华大学", 101, st="物理", mrank=18, mscore=696),
        # 哈工大深圳：广东
        _adm(2025, "18213", "哈尔滨工业大学（深圳）", 301, st="物理", mrank=8000, mscore=650),
        _adm(2026, "18213", "哈尔滨工业大学（深圳）", 301, st="物理", mrank=7500, mscore=655),
        # 历史类数据
        _adm(2025, "10001", "北京大学", 201, st="历史", mrank=150, mscore=648),
    ]
    return InMemorySchoolRepository(unis, adm)


@pytest.fixture
def client(monkeypatch, repo) -> TestClient:
    monkeypatch.setattr(schools_api, "get_school_service", lambda: SchoolService(repo))
    return TestClient(app)


# ==================== J1 · 层次 + 地域筛选 ====================


def test_j1_filter_by_tag_985(client) -> None:
    r = client.get("/api/schools", params={"tag": "985"})
    assert r.status_code == 200
    data = r.json()
    codes = {i["code"] for i in data["items"]}
    assert {"10001", "10002", "10003", "10213", "18213"} <= codes
    assert "99999" not in codes


def test_j1_filter_by_uni_province(client) -> None:
    r = client.get("/api/schools", params={"uni_province": "广东"})
    data = r.json()
    codes = {i["code"] for i in data["items"]}
    assert "18213" in codes and "99999" in codes
    assert "10001" not in codes


def test_j1_filter_combined(client) -> None:
    r = client.get("/api/schools", params={"uni_province": "北京", "tag": "985"})
    data = r.json()
    assert data["total"] == 3


def test_j1_pagination(client) -> None:
    r = client.get("/api/schools", params={"page": 1, "page_size": 2})
    data = r.json()
    assert data["total"] == 6
    assert len(data["items"]) == 2
    r2 = client.get("/api/schools", params={"page": 2, "page_size": 2})
    assert len(r2.json()["items"]) == 2
    # 不重叠
    assert {i["code"] for i in data["items"]}.isdisjoint({i["code"] for i in r2.json()["items"]})


# ==================== J2 · 关键词搜索 ====================


def test_j2_keyword_hits_name(client) -> None:
    r = client.get("/api/schools", params={"keyword": "哈尔滨"})
    data = r.json()
    codes = {i["code"] for i in data["items"]}
    # 同名院校（不同校区代码）都应命中——不得混用
    assert {"10213", "18213"} <= codes


def test_j2_keyword_hits_code(client) -> None:
    r = client.get("/api/schools", params={"keyword": "10001"})
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "北京大学"


def test_j2_keyword_no_match(client) -> None:
    r = client.get("/api/schools", params={"keyword": "不存在的大学"})
    assert r.json()["total"] == 0


# ==================== J3 · 详情 ====================


def test_j3_detail_groups_and_years(client) -> None:
    r = client.get("/api/schools/10001", params={"province": "广东", "subject_type": "物理"})
    assert r.status_code == 200
    data = r.json()
    assert data["code"] == "10001"
    assert data["name"] == "北京大学"
    assert data["admission_url"] == "https://www.gotopku.cn/"
    assert data["admission_url_kind"] == "school"
    # 两个专业组，各三年数据
    assert len(data["groups"]) == 2
    g201 = next(g for g in data["groups"] if g["group_code"] == 201)
    assert [ln["year"] for ln in g201["lines"]] == [2024, 2025, 2026]
    assert g201["lines"][0]["min_rank"] == 32


def test_j3_detail_missing_year_marked(client) -> None:
    """清华缺 2026 → trend_note 明确标注缺年，不推算。"""
    r = client.get("/api/schools/10003", params={"province": "广东", "subject_type": "物理"})
    data = r.json()
    assert "缺 2026" in data["trend_note"]


def test_j3_detail_not_found(client) -> None:
    r = client.get("/api/schools/00000", params={"province": "广东"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNI_NOT_FOUND"


def test_j3_detail_requirement_marked_pending(client) -> None:
    """选科要求缺失 → 文案标「待补齐」，不猜测。"""
    r = client.get("/api/schools/10001", params={"province": "广东", "subject_type": "物理"})
    data = r.json()
    assert all("待补齐" in g["requirement_text"] for g in data["groups"])
    assert all(g["require_status"] == "待补" for g in data["groups"])


def test_j3_detail_falls_back_to_chsi_when_official_unavailable(client) -> None:
    r = client.get("/api/schools/99999", params={"province": "广东", "subject_type": "物理"})
    data = r.json()
    assert data["admission_url_kind"] == "chsi"
    assert data["admission_url"].startswith("https://gaokao.chsi.com.cn/sch/search.do")


# ==================== J4 · 对比 ====================


def test_j4_compare_three_schools(client) -> None:
    r = client.get("/api/schools/compare", params={"codes": "10001,10003,18213"})
    assert r.status_code == 200
    data = r.json()
    assert data["years"] == [2024, 2025, 2026]
    assert len(data["rows"]) == 3
    by = {row["code"]: row for row in data["rows"]}
    # 北大三年位次趋势
    assert by["10001"]["rank_trend"] == [32, 30, 28]
    # 清华缺 2026 → 该年 None（不补造）
    assert by["10003"]["rank_trend"] == [20, 18, None]
    assert by["18213"]["rank_trend"] == [None, 8000, 7500]


def test_j4_compare_two_schools(client) -> None:
    r = client.get("/api/schools/compare", params={"codes": "10001,18213"})
    assert len(r.json()["rows"]) == 2


def test_j4_compare_too_many_rejected(client) -> None:
    r = client.get("/api/schools/compare", params={"codes": "10001,10002,10003,10213"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COMPARE_TOO_MANY"


def test_j4_compare_unknown_code(client) -> None:
    r = client.get("/api/schools/compare", params={"codes": "10001,00000"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNI_NOT_FOUND"


def test_j4_compare_empty(client) -> None:
    r = client.get("/api/schools/compare", params={"codes": ""})
    assert r.status_code == 422


# ==================== 附加 · meta 与口径 ====================


def test_meta_endpoint(client) -> None:
    r = client.get("/api/schools/meta")
    assert r.status_code == 200
    data = r.json()
    assert "北京" in data["uni_provinces"] and "广东" in data["uni_provinces"]
    assert data["levels"] == ["本科", "专科"]


def test_list_carries_caliber_text(client) -> None:
    r = client.get("/api/schools")
    assert "投档线≠录取线" in r.json()["caliber_text"]
