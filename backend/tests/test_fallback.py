"""T11 测试 —— 推荐降级快照 / 兜底表（对应任务卡「测试场景」J1–J3 + 红线守卫）。

J1 断开 LLM（连续失败 / 全链不可用）仍能返回三档表格
J2 返回结构精简且字段完整（**不含 LLM 解释字段**）
J3 缓存命中结果一致

外加红线守卫：
- 兜底表数据必须来自库内（复用 T08 引擎 → 同一套候选过滤/分档）
- 「专家规则模式」标注必须存在（mode / mode_label / mode_note）
- 免责声明必须随结果返回
- **不得因为降级而放宽「无数据标注」**：缺年 ref_years_note / 选科 require_note 原样带出
- 入参校验与 T08 一致（缺 rank 抛错、科类非法抛错）
- 无库内数据抛 DataMissingError（不返回空壳假装成功）

设计：**不依赖 MySQL / Redis**——用 ``InMemoryRecommendRepository`` 构造确定性数据，
缓存测试用内存伪 Redis 替身（monkeypatch ``get_redis``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import DataMissingError, ValidationError  # noqa: E402
from app.schemas.fallback import MODE_EXPERT_RULE, FallbackResult  # noqa: E402
from app.services.fallback import (  # noqa: E402
    DEFAULT_FALLBACK_LIMIT,
    FallbackService,
    recommend_result_to_fallback,
)
from app.services.recommend import (  # noqa: E402
    InMemoryRecommendRepository,
    RecommendEngine,
)

PROV = "广东"
META = {
    "10001": {"tags": "985|211|双一流", "province": "北京", "school_type": "综合", "meta_status": "已核"},
    "10002": {"tags": "211|双一流", "province": "广东", "school_type": "理工", "meta_status": "已核"},
    "10003": {"tags": "", "province": "广东", "school_type": "综合", "meta_status": "已核"},
}


def _row(year, code, name, group, min_rank, plan=10, min_score=600, first=("物理",), second=(), pattern="none"):
    return {
        "year": year, "province": PROV, "subject_type": "物理",
        "university_code": code, "university_name": name, "group_code": group,
        "plan_count": plan, "admitted_count": plan, "min_score": min_score, "min_rank": min_rank,
        "first": list(first), "second": list(second), "pattern": pattern, "require_status": "待补",
    }


def _three_tier_rows() -> list[dict]:
    """三院校三年数据：A→冲 / B→稳 / C→保（R=10000 时）。"""
    return [
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


@pytest.fixture
def service() -> FallbackService:
    """无缓存（use_cache=False）的兜底服务，纯离线可复现。"""
    return FallbackService(
        InMemoryRecommendRepository(_three_tier_rows(), META),
        use_cache=False,
    )


# ============================================================
# J1 断开 LLM 仍能返回三档表格
# ============================================================


def test_j1_returns_three_tiers_without_llm(service: FallbackService) -> None:
    """服务完全不碰 LLM —— 无 mock、无 API key 也能出三档表。"""
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    assert isinstance(res, FallbackResult)
    assert [t.tier for t in res.tiers] == ["chong", "wen", "bao"]
    assert [t.label for t in res.tiers] == ["冲", "稳", "保"]
    # 三档各有内容
    assert res.total_rows >= 3
    assert res.tiers[0].rows[0].university_name == "院校A"
    assert res.tiers[1].rows[0].university_name == "院校B"
    assert res.tiers[2].rows[0].university_name == "院校C"


def test_j1_mode_marked_expert_rule(service: FallbackService) -> None:
    """必须标注「专家规则模式」（红线）。"""
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    assert res.mode == MODE_EXPERT_RULE == "expert_rule"
    assert res.mode_label == "专家规则模式"
    assert res.mode_note  # 非空：说明降级原因与已做动作
    assert "专家规则模式" in res.mode_note


def test_j1_disclaimer_present(service: FallbackService) -> None:
    """免责声明必须随结果返回（红线）。"""
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    assert res.disclaimer == "本建议仅供参考，不作为最终填报承诺"


def test_j1_no_llm_import_in_service_module() -> None:
    """静态守卫：兜底服务模块**不得**引入 LLM 客户端（否则降级路径可能触发 LLM）。"""
    src = (BACKEND_DIR / "app" / "services" / "fallback.py").read_text(encoding="utf-8")
    assert "app.core.llm" not in src
    assert "LLMClient" not in src
    assert "get_llm_client" not in src


def test_j1_llm_unavailable_scenario_simulated(service: FallbackService, monkeypatch) -> None:
    """模拟「LLM 全链不可用」：把 LLM 客户端的 complete 打成必然抛错，兜底表不受影响。"""
    import app.core.llm as llm_mod

    def _boom(*_a, **_k):
        raise RuntimeError("all models unavailable")

    # 若模块里存在 LLM 客户端入口，将其设为不可用；不存在也应当通过（说明根本没用 LLM）
    if hasattr(llm_mod, "get_llm_client"):
        monkeypatch.setattr(llm_mod, "get_llm_client", _boom)
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    assert res.total_rows >= 3


# ============================================================
# J2 结构精简且字段完整（不含 LLM 解释）
# ============================================================


def test_j2_no_explanation_fields(service: FallbackService) -> None:
    """**核心红线**：兜底结构不得含 LLM 解释字段（explanation / risk / parent_summary）。"""
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    dumped = res.model_dump()
    for forbidden in ("explanation", "risk_explanation", "parent_summary", "evidence"):
        assert forbidden not in dumped, f"兜底表不应包含解释字段 {forbidden!r}"
    # 行级同样不得含解释字段
    row = res.tiers[0].rows[0].model_dump()
    for forbidden in ("explanation", "risk_explanation", "parent_summary", "evidence", "plan_note"):
        assert forbidden not in row


def test_j2_row_has_required_fields(service: FallbackService) -> None:
    """行级字段完整：院校 / 层次 / 往年位次 / 档位。"""
    res = service.fallback(score=600, subject_type="物理", rank=10000)
    row = res.tiers[0].rows[0]
    assert row.university_code == "10001"
    assert row.university_name == "院校A"
    assert row.subject_type == "物理"
    assert row.tier == "chong"
    assert row.tier_label == "冲"
    assert row.ref_rank is not None
    assert row.latest_year == 2026
    assert row.latest_rank == 2900
    assert row.latest_score == 600
    assert row.level_tags == "985|211|双一流"
    assert row.school_province == "北京"


def test_j2_context_fields_echoed(service: FallbackService) -> None:
    """查询上下文回显，便于前端确认口径。"""
    res = service.fallback(score=612, subject_type="物理", rank=9876, current_year=2026)
    assert res.province == PROV
    assert res.current_year == 2026
    assert res.subject_type == "物理"
    assert res.score == 612
    assert res.rank == 9876
    assert res.caliber_text  # 口径文字随结果返回（防口径漂移）
    assert res.generated_at  # 生成时间非空


def test_j2_tier_limit_applies_to_fallback() -> None:
    """每档截断到指定条数（兜底表求快）。"""
    rows = []
    for i in range(25):
        code = f"2{i:04d}"
        for y in (2024, 2025, 2026):
            rows.append(_row(y, code, f"稳院校{i}", 201, 10000 + i))
    svc = FallbackService(InMemoryRecommendRepository(rows, {}), use_cache=False)
    res = svc.fallback(score=600, subject_type="物理", rank=10000, tier_limit=4)
    assert res.tiers[1].count == 4
    assert len(res.tiers[1].rows) == 4


def test_j2_default_limit_is_smaller_than_report() -> None:
    """默认每档条数应小于正常报告的 tier_limit（6 < 10），体现「精简」。"""
    assert DEFAULT_FALLBACK_LIMIT < 10


# ============================================================
# J3 缓存命中一致
# ============================================================


class _FakeRedis:
    """内存伪 Redis（仅实现 get / set），用于离线验证缓存行为。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:  # noqa: ARG002
        self.set_calls += 1
        self.store[key] = value
        return True


def _patch_redis(monkeypatch, fake: _FakeRedis) -> None:
    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis", lambda: fake)


def test_j3_cache_hit_consistent(monkeypatch) -> None:
    """第二次相同请求命中缓存，结果除 cache_hit 外完全一致。"""
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    svc = FallbackService(InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=True)
    first = svc.fallback(score=600, subject_type="物理", rank=10000)
    second = svc.fallback(score=600, subject_type="物理", rank=10000)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert fake.set_calls == 1  # 只写一次
    # 除 cache_hit / generated_at 外内容一致
    a = first.model_dump()
    b = second.model_dump()
    for k in ("cache_hit", "generated_at"):
        a.pop(k)
        b.pop(k)
    assert a == b


def test_j3_cache_key_varies_with_inputs(monkeypatch) -> None:
    """不同入参 → 不同缓存键（避免串号）。"""
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    svc = FallbackService(InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=True)

    svc.fallback(score=600, subject_type="物理", rank=10000)
    svc.fallback(score=601, subject_type="物理", rank=10000)  # 分数变
    assert fake.set_calls == 2
    assert len(fake.store) == 2


def test_j3_cache_key_varies_with_fingerprint(monkeypatch) -> None:
    """数据源指纹变化 → 缓存键变化（数据重导自然失效）。"""
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    repo = InMemoryRecommendRepository(_three_tier_rows(), META)
    s1 = FallbackService(repo, use_cache=True, dataset_fingerprint="fp-v1")
    s2 = FallbackService(repo, use_cache=True, dataset_fingerprint="fp-v2")
    s1.fallback(score=600, subject_type="物理", rank=10000)
    s2.fallback(score=600, subject_type="物理", rank=10000)
    assert len(fake.store) == 2  # 指纹不同 → 两个键


def test_j3_redis_failure_silently_degrades(monkeypatch) -> None:
    """Redis 故障 → 静默降级直算，不影响结果（缓存是优化不是依赖）。"""
    import app.core.redis as redis_mod

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_mod, "get_redis", _boom)
    svc = FallbackService(InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=True)
    res = svc.fallback(score=600, subject_type="物理", rank=10000)
    assert res.total_rows >= 3
    assert res.cache_hit is False


# ============================================================
# 红线守卫：无数据标注 / 入参校验 / 无数据报错
# ============================================================


def test_missing_year_note_carried_through() -> None:
    """缺年院校的 ref_years_note 必须原样带出（降级不得放宽无数据标注红线）。"""
    rows = [_row(2026, "10003", "仅一年院校", 203, 16000)]  # 只 2026，缺 2024/2025
    svc = FallbackService(InMemoryRecommendRepository(rows, META), use_cache=False)
    res = svc.fallback(score=600, subject_type="物理", rank=10000)
    rows_out = [r for t in res.tiers for r in t.rows]
    assert rows_out, "应有至少一条结果"
    assert "缺" in rows_out[0].ref_years_note


def test_require_note_carried_through() -> None:
    """再选科目待补 → require_note 必须原样带出。"""
    rows = [_row(2026, "10003", "待补院校", 203, 16000, second=())]
    svc = FallbackService(InMemoryRecommendRepository(rows, META), use_cache=False)
    res = svc.fallback(score=600, subject_type="物理", rank=10000)
    row = res.tiers[2].rows[0]
    assert row.require_note
    assert "待补" in row.require_note


def test_missing_rank_raises(service: FallbackService) -> None:
    """缺 rank 抛 ValidationError（禁止猜测位次，与 T08 同口径）。"""
    with pytest.raises(ValidationError):
        service.fallback(score=600, subject_type="物理", rank=None)


def test_invalid_subject_type_raises(service: FallbackService) -> None:
    with pytest.raises(ValidationError):
        service.fallback(score=600, subject_type="综合", rank=10000)


def test_invalid_tier_limit_raises(service: FallbackService) -> None:
    with pytest.raises(ValidationError):
        service.fallback(score=600, subject_type="物理", rank=10000, tier_limit=0)


def test_no_data_raises_data_missing() -> None:
    """库内无数据 → 抛 DataMissingError（不返回空壳假装成功）。"""
    svc = FallbackService(InMemoryRecommendRepository([], META), use_cache=False)
    with pytest.raises(DataMissingError):
        svc.fallback(score=600, subject_type="物理", rank=10000)


def test_future_year_ref_raises() -> None:
    """引用未来年份 → 时效红线报错（与 T08 同口径）。"""
    svc = FallbackService(InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=False)
    with pytest.raises(DataMissingError):
        svc.fallback(score=600, subject_type="物理", rank=10000, current_year=2024, ref_years=[2026])


# ============================================================
# 纯函数：投影（recommend_result_to_fallback）
# ============================================================


def test_project_from_recommend_result_drops_explanation() -> None:
    """投影函数：从 T08 结果生成兜底表时**丢弃全部解释**。"""
    eng = RecommendEngine(InMemoryRecommendRepository(_three_tier_rows(), META))
    result = eng.recommend(score=600, subject_type="物理", rank=10000)
    assert result.explanation and result.risk_explanation  # 原结果有解释
    fb = recommend_result_to_fallback(result, tier_limit=3, generated_at="2026-01-01 00:00:00")
    assert isinstance(fb, FallbackResult)
    assert fb.mode == MODE_EXPERT_RULE
    assert fb.disclaimer == result.disclaimer
    assert fb.generated_at == "2026-01-01 00:00:00"
    assert fb.caliber_text == result.caliber_text
    # 每档 ≤ tier_limit
    for t in fb.tiers:
        assert t.count <= 3


def test_project_preserves_caliber_and_years() -> None:
    eng = RecommendEngine(InMemoryRecommendRepository(_three_tier_rows(), META))
    result = eng.recommend(score=600, subject_type="物理", rank=10000, ref_years=[2024, 2025, 2026])
    fb = recommend_result_to_fallback(result, tier_limit=5)
    assert fb.ref_years_used == result.ref_years_used
    assert fb.rank == result.rank
    assert fb.score == result.score


# ============================================================
# 接口层：GET /api/recommend/fallback（T11 J1/J2 端到端）
# ============================================================


def test_api_route_registered() -> None:
    """路由已注册到应用（OpenAPI 可见）。"""
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/recommend/fallback" in paths
    assert "get" in paths["/api/recommend/fallback"]


def test_api_fallback_endpoint_returns_table(monkeypatch) -> None:
    """端到端：接口返回「专家规则模式」三档表（服务被注入内存实现，不依赖 MySQL）。"""
    from fastapi.testclient import TestClient

    import app.api.recommend as recommend_api
    from app.main import app

    svc = FallbackService(
        InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=False
    )
    monkeypatch.setattr(recommend_api, "get_fallback_service", lambda: svc)

    client = TestClient(app)
    resp = client.get(
        "/api/recommend/fallback",
        params={"score": 600, "subject_type": "物理", "rank": 10000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "expert_rule"
    assert body["mode_label"] == "专家规则模式"
    assert body["disclaimer"] == "本建议仅供参考，不作为最终填报承诺"
    assert [t["tier"] for t in body["tiers"]] == ["chong", "wen", "bao"]
    first_row = body["tiers"][0]["rows"][0]
    assert set(first_row).isdisjoint({"explanation", "risk_explanation", "parent_summary"})


def test_api_fallback_endpoint_invalid_subject(monkeypatch) -> None:
    """接口层：科类非法 → 422（ValidationError → http_status 422）。"""
    from fastapi.testclient import TestClient

    import app.api.recommend as recommend_api
    from app.main import app

    svc = FallbackService(
        InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=False
    )
    monkeypatch.setattr(recommend_api, "get_fallback_service", lambda: svc)

    client = TestClient(app)
    resp = client.get(
        "/api/recommend/fallback",
        params={"score": 600, "subject_type": "综合", "rank": 10000},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_api_rank_computed_from_score_uses_score_engine(monkeypatch) -> None:
    """接口层：只给 score 不给 rank → 用 T07 位次引擎现算（口径唯一）。"""
    from fastapi.testclient import TestClient

    import app.api.recommend as recommend_api
    from app.main import app

    svc = FallbackService(
        InMemoryRecommendRepository(_three_tier_rows(), META), use_cache=False
    )
    monkeypatch.setattr(recommend_api, "get_fallback_service", lambda: svc)
    # 把 T07 引擎现算替换为确定性返回值，验证接口确实调用了它
    monkeypatch.setattr(recommend_api, "_rank_from_score", lambda **kw: 10000)

    client = TestClient(app)
    resp = client.get(
        "/api/recommend/fallback",
        params={"score": 600, "subject_type": "物理"},
    )
    assert resp.status_code == 200
    assert resp.json()["rank"] == 10000
