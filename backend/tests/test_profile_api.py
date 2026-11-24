"""T23 测试 —— 画像测评与档案接口（POST /api/profile + GET /api/rank）。

对应任务卡「测试场景（必做）」（后端侧）：
    J1 建档落库脱敏（回执只含区间，无明文分数/位次）；
    J2 位次直填优先于换算 / 分数换算口径唯一（T07 引擎）；
    J3 近三年等位分正确 + 缺年显式标注（score=null + note，不伪造）；
    J4 未落地数据省份 → 200 + DATA_MISSING（明确文案，不阻断为 5xx）。

设计：**不依赖 MySQL** —— InMemoryProfileStore + InMemoryScoreRepository 注入，离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import profile as profile_api  # noqa: E402
from app.main import app  # noqa: E402
from app.services.profile import InMemoryProfileStore, ProfileService  # noqa: E402
from app.services.score import InMemoryScoreRepository, ScoreEngine  # noqa: E402

# ---- 测试数据集：2026/2025 物理有表，2024 物理**故意缺失**（覆盖缺年标注）----
ROWS = [
    # (year, subject_type, score, rank=累计人数)
    (2026, "物理", 700, 100),
    (2026, "物理", 650, 1000),
    (2026, "物理", 600, 10000),
    (2026, "物理", 550, 30000),
    (2026, "物理", 500, 60000),
    (2025, "物理", 700, 90),
    (2025, "物理", 650, 900),
    (2025, "物理", 600, 9000),
    (2025, "物理", 550, 28000),
    (2025, "物理", 500, 55000),
]


@pytest.fixture
def client(monkeypatch):
    """注入内存服务与引擎的 TestClient（不碰 MySQL/Redis）。"""
    monkeypatch.setattr(
        profile_api,
        "get_profile_service",
        lambda *args, **kwargs: ProfileService(InMemoryProfileStore()),
    )
    engine = ScoreEngine(InMemoryScoreRepository(ROWS), use_cache=False)
    monkeypatch.setattr(profile_api, "get_score_engine", lambda: engine)
    return TestClient(app)


# ==================== J1 · POST /api/profile 建档 ====================


def test_j1_create_profile_returns_buckets_not_plaintext(client) -> None:
    body = {
        "province": "广东",
        "subject_type": "物理",
        "elective_subjects": ["化学", "生物"],
        "score": 612,
        "rank": 8500,
        "preferred_cities": ["广州"],
        "anon_id": "anon-test",
    }
    r = client.post("/api/profile", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["id"] is not None
    assert data["province"] == "广东"
    assert data["subject_type"] == "物理"
    assert data["elective_subjects"] == ["化学", "生物"]
    # 脱敏红线：回执只有区间桶，**没有明文 score/rank 字段**
    assert "score" not in data and "rank" not in data
    assert data["score_bucket"] == "610-619"
    assert data["rank_bucket"]
    assert data["anon_id"] == "anon-test"


def test_j1_invalid_subject_type_rejected(client) -> None:
    r = client.post(
        "/api/profile",
        json={"province": "广东", "subject_type": "玄学", "score": 600},
    )
    assert r.status_code == 422


def test_j1_score_out_of_range_rejected(client) -> None:
    r = client.post(
        "/api/profile",
        json={"province": "广东", "subject_type": "物理", "score": 900},
    )
    assert r.status_code == 422  # Pydantic ge/le 先拦


def test_j1_plaintext_never_persisted(client) -> None:
    """档案 store 里没有明文分数/位次（只有 hash + bucket）。"""
    store = InMemoryProfileStore()
    client.get("/health")  # warm-up（不影响断言）
    from app.services.profile import ProfileInput

    svc = ProfileService(store)
    svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=612, rank=8500))
    row = store.get(1)
    assert "score" not in row and "rank" not in row
    assert row["score_bucket"] == "610-619"
    assert row["score_hash"] and row["rank_hash"]


# ==================== J2 · GET /api/rank 位次口径 ====================


def test_j2_rank_computed_from_score(client) -> None:
    r = client.get("/api/rank", params={"subject_type": "物理", "score": 600})
    assert r.status_code == 200
    data = r.json()
    assert data["rank"] == 10000
    assert data["rank_source"] == "computed"
    assert data["score"] == 600
    assert data["capped"] is False


def test_j2_manual_rank_takes_priority(client) -> None:
    """位次直填优先于换算（用户实测位次更准）。"""
    r = client.get("/api/rank", params={"subject_type": "物理", "score": 600, "rank": 5000})
    assert r.status_code == 200
    data = r.json()
    assert data["rank"] == 5000
    assert data["rank_source"] == "manual"


def test_j2_requires_score_or_rank(client) -> None:
    r = client.get("/api/rank", params={"subject_type": "物理"})
    assert r.status_code == 422


def test_j2_invalid_subject_type(client) -> None:
    r = client.get("/api/rank", params={"subject_type": "综合", "score": 600})
    assert r.status_code == 422


# ==================== J3 · 近三年等位分 + 缺年标注 ====================


def test_j3_equiv_scores_across_years(client) -> None:
    r = client.get("/api/rank", params={"subject_type": "物理", "rank": 9000})
    assert r.status_code == 200
    data = r.json()
    equiv = {e["year"]: e for e in data["equiv_scores"]}
    # 2026/2025 有表 → 插值出等位分
    assert equiv[2026]["score"] is not None
    assert equiv[2025]["score"] is not None
    # 2024 表缺失 → **不伪造**：score=null + 明确标注
    assert equiv[2024]["score"] is None
    assert "待补" in equiv[2024]["note"]


def test_j3_equiv_interpolation_value(client) -> None:
    """位次 9500 落在 2026 表 (600,10000)/(650,1000) 之间 → 线性插值可复算。"""
    r = client.get("/api/rank", params={"subject_type": "物理", "rank": 9500})
    data = r.json()
    e = {x["year"]: x for x in data["equiv_scores"]}[2026]
    # score = 600 + (650-600)*(10000-9500)/(10000-1000) = 600 + 50*500/9000 ≈ 602.8
    assert abs(e["score"] - 602.8) < 0.2
    assert e["low_score"] == 600 and e["high_score"] == 650


# ==================== J4 · 未落地省份 ====================


def test_j4_unsupported_province_data_missing(client) -> None:
    """未落地数据省份 → 200 + DATA_MISSING（业务正常但无数据，不伪造位次）。"""
    r = client.get("/api/rank", params={"subject_type": "物理", "score": 600, "province": "北京"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "DATA_MISSING"


def test_j4_caliber_text_present(client) -> None:
    """口径文字必带（位次上限 / 插值 / 投档线≠录取线）。"""
    r = client.get("/api/rank", params={"subject_type": "物理", "rank": 9000})
    data = r.json()
    assert "累计人数" in data["caliber_text"]
    assert "投档线≠录取线" in data["caliber_text"]
