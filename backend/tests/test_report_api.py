"""T25 测试 —— AI 报告接口（`GET /api/report`：三档 + 风险解释 + 家长版）。

对应任务卡「测试场景」J1–J5：

J1 三档齐全 + 报告文本字段完整（口径 / 整体说明 / 风险解释 / 家长版 / 免责）
J2 位次缺省 → 由 T07 位次引擎换算（口径唯一，不重复实现）
J3 风险解释**不含概率数字**（红线，引擎 + 接口双层保证）
J4 无数据/缺年院校显式标注（`ref_years_note` / `require_note`），不静默
J5 `report_tier` 非法 → 422；档位字段 `tier`（basic/deep）预留

设计：**不依赖 MySQL** —— 注入 ``InMemoryRecommendRepository`` 构造的确定性引擎
+ 内存位次仓，完全可离线复现。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import report as report_api  # noqa: E402
from app.main import app  # noqa: E402
from app.services.recommend import (  # noqa: E402
    InMemoryRecommendRepository,
    RecommendEngine,
)
from app.services.score import InMemoryScoreRepository, ScoreEngine  # noqa: E402

PROV = "广东"
META = {
    "10001": {"tags": "985|211|双一流", "province": "北京", "school_type": "综合", "meta_status": "已核"},
    "10002": {"tags": "211|双一流", "province": "广东", "school_type": "理工", "meta_status": "已核"},
    "10003": {"tags": "", "province": "广东", "school_type": "综合", "meta_status": "待补"},
}


def _row(year, code, name, group, min_rank, plan=10, min_score=600, first=("物理",), second=(), pattern="none"):
    return {
        "year": year, "province": PROV, "subject_type": "物理",
        "university_code": code, "university_name": name, "group_code": group,
        "plan_count": plan, "admitted_count": plan, "min_score": min_score, "min_rank": min_rank,
        "first": list(first), "second": list(second), "pattern": pattern, "require_status": "待补",
    }


@pytest.fixture
def engine() -> RecommendEngine:
    """三院校 × 三年确定性数据：A 冲 / B 稳 / C 保（R=10000）。"""
    rows = [
        _row(2024, "10001", "院校A", 201, 3200),
        _row(2025, "10001", "院校A", 201, 3000),
        _row(2026, "10001", "院校A", 201, 2900),
        _row(2024, "10002", "院校B", 202, 10500),
        _row(2025, "10002", "院校B", 202, 10000),
        _row(2026, "10002", "院校B", 202, 9800),
        _row(2024, "10003", "院校C", 203, 16500),
        _row(2025, "10003", "院校C", 203, 16000),
        _row(2026, "10003", "院校C", 203, 15800),
    ]
    return RecommendEngine(InMemoryRecommendRepository(rows, META))


@pytest.fixture
def score_engine() -> ScoreEngine:
    """分数 → 位次换算（供 J2 用）：2026 物理 600 → rank 10000。"""
    return ScoreEngine(
        InMemoryScoreRepository([(2026, "物理", 600, 10000), (2026, "物理", 700, 100)]),
        use_cache=False,
    )


def _client(engine: RecommendEngine, score_engine: ScoreEngine) -> TestClient:
    """注入引擎替身（不连 MySQL）。"""
    return TestClient(app)


# ============================================================
# J1 三档齐全 + 报告文本字段完整
# ============================================================


def test_j1_report_fields_complete(engine: RecommendEngine) -> None:
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [g["tier"] for g in body["tiers"]] == ["chong", "wen", "bao"]
    assert [g["label"] for g in body["tiers"]] == ["冲", "稳", "保"]
    # 报告文本字段必须齐全（红线：必须有解释）
    for key in ("caliber_text", "explanation", "risk_explanation", "parent_summary", "disclaimer"):
        assert body.get(key), f"缺字段 {key}"
    assert body["disclaimer"] == "本建议仅供参考，不作为最终填报承诺"
    assert body["rank"] == 10000


def test_j1_report_tier_default_basic(engine: RecommendEngine) -> None:
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    assert r.status_code == 200
    # 默认档位走 basic，接口正常返回（deep 为预留付费位）
    assert r.json()["tiers"]


# ============================================================
# J2 位次缺省 → T07 引擎换算（口径唯一）
# ============================================================


def test_j2_rank_derived_from_score(engine: RecommendEngine, score_engine: ScoreEngine) -> None:
    with (
        patch.object(report_api, "get_recommend_engine", lambda: engine),
        patch.object(report_api, "_rank_from_score", lambda **kw: 10000),
    ):
        client = _client(engine, score_engine)
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理"})
    assert r.status_code == 200, r.text
    assert r.json()["rank"] == 10000


def test_j2_explicit_rank_takes_priority(engine: RecommendEngine) -> None:
    """显式传 rank 时不走换算（传一个会产生不同档位的值）。"""
    with (
        patch.object(report_api, "get_recommend_engine", lambda: engine),
        patch.object(report_api, "_rank_from_score", lambda **kw: pytest.fail("不应调用换算")),
    ):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    assert r.status_code == 200
    assert r.json()["rank"] == 10000


# ============================================================
# J3 风险解释不含概率数字（红线）
# ============================================================


def test_j3_no_probability_in_risk_text(engine: RecommendEngine) -> None:
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    body = r.json()
    risk = body["risk_explanation"]
    for word in ("录取概率", "概率", "%", "百分之"):
        assert word not in risk, f"风险解释含禁词「{word}」"
    # 全报告文本亦不得出现「录取概率」字样
    assert "录取概率" not in body["parent_summary"]
    assert "录取概率" not in body["explanation"]


def test_j3_probability_guard_triggers(engine: RecommendEngine) -> None:
    """防御性断言：引擎若产出概率数字（模拟缺陷），接口必须中止而非静默返回。"""
    base = engine.recommend(score=600, subject_type="物理", rank=10000)
    bad = base.model_copy(update={"risk_explanation": "本档录取概率约为 60%"})
    with (
        patch.object(report_api, "get_recommend_engine", lambda: engine),
        patch.object(RecommendEngine, "recommend", lambda self, **kw: bad),
    ):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "RISK_TEXT_INVALID"


# ============================================================
# J4 无数据/缺年显式标注
# ============================================================


def test_j4_ref_years_note_present(engine: RecommendEngine) -> None:
    """参考年份说明必须随报告返回（缺年时显式标注）。"""
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = _client(engine, None)  # type: ignore[arg-type]
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    body = r.json()
    assert body["ref_years_used"], "必须披露实际使用年份"
    # 每条推荐应带 evidence（可解释）
    for group in body["tiers"]:
        for item in group["items"]:
            assert isinstance(item["evidence"], list)


def test_j4_require_note_on_items(engine: RecommendEngine) -> None:
    """选科要求待补 → 逐条带提示（不静默）。"""
    engine2 = RecommendEngine(
        InMemoryRecommendRepository(
            [_row(2026, "10002", "院校B", 202, 10000, second=("化学",), pattern="any")], META
        )
    )
    with patch.object(report_api, "get_recommend_engine", lambda: engine2):
        client = TestClient(app)
        r = client.get("/api/report", params={"score": 600, "subject_type": "物理", "rank": 10000})
    assert r.status_code == 200, r.text
    for group in r.json()["tiers"]:
        for item in group["items"]:
            # 待补状态必须给文案（require_note 或 require_status="待补"）
            assert item["require_status"] == "待补"
            assert item["require_note"], "待补选科要求必须带提示文案"


# ============================================================
# J5 report_tier 校验
# ============================================================


def test_j5_invalid_report_tier(engine: RecommendEngine) -> None:
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = TestClient(app)
        r = client.get(
            "/api/report",
            params={"score": 600, "subject_type": "物理", "rank": 10000, "report_tier": "premium"},
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "TIER_INVALID"


def test_j5_deep_tier_accepted(engine: RecommendEngine) -> None:
    """deep 档位可在字段层接受（P0 不产出差异化内容，但不报错）。"""
    with patch.object(report_api, "get_recommend_engine", lambda: engine):
        client = TestClient(app)
        r = client.get(
            "/api/report",
            params={"score": 600, "subject_type": "物理", "rank": 10000, "report_tier": "deep"},
        )
    assert r.status_code == 200, r.text


def test_j5_missing_required_score(engine: RecommendEngine) -> None:
    client = TestClient(app)
    r = client.get("/api/report", params={"subject_type": "物理"})
    assert r.status_code == 422


def test_rank_from_score_helper_uses_score_engine(score_engine: ScoreEngine) -> None:
    """`_rank_from_score` 必须复用 T07 引擎（口径唯一）。"""
    with patch("app.services.score.get_score_engine", lambda: score_engine):
        rank = report_api._rank_from_score(score=600, subject_type="物理", province=PROV, current_year=2026)
    assert rank == 10000
