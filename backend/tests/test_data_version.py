"""T09 测试 —— 数据时效与年份切换/对比（对应任务卡「测试场景」J1–J4 + 红线守卫）。

J1 切换 ``ref_years`` 后推荐结果随之变化且可解释
J2 使用未来年份数据被拦截
J3 趋势对比返回三年序列且方向正确
J4 切换动作落 ``audit_log``

外加红线守卫：
- 回滚可恢复切换前配置，且回滚本身也落审计
- 口径文字必须随结果返回，且与生效年份一致（防口径漂移）
- 配置自洽性（数量一致 / 和为 1 / 升序 / 无重复）逐条拒绝，不静默修正
- ``rollback`` 无存档时显式报错（不静默成功）
- 趋势对缺年**占位不插值**，并显式给出 ``missing_years``

设计：**不依赖 MySQL / Redis**——用 ``InMemoryDataVersionRepository`` +
``InMemoryRecommendRepository`` 构造确定性数据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import DataMissingError, ValidationError  # noqa: E402
from app.services.data_version import (  # noqa: E402
    DataVersionConfig,
    DataVersionService,
    InMemoryDataVersionRepository,
    StaticDataVersionProvider,
    _overall_direction,
)
from app.services.recommend import (  # noqa: E402
    InMemoryRecommendRepository,
    RecommendEngine,
)

PROV = "广东"

# ============================================================
# 夹具
# ============================================================


def _cfg(current_year: int = 2026, ref_years=(2024, 2025, 2026), weights=(0.2, 0.3, 0.5)) -> DataVersionConfig:
    return DataVersionConfig(
        current_year=current_year,
        ref_years=tuple(ref_years),
        year_weights=tuple(weights),
        source="db",
        row_id=1,
    )


def _admission_row(year, code, name, group, min_rank, plan=10, min_score=600) -> dict:
    return {
        "year": year, "province": PROV, "subject_type": "物理",
        "university_code": code, "university_name": name, "group_code": group,
        "plan_count": plan, "admitted_count": plan, "min_score": min_score, "min_rank": min_rank,
        "first": ["物理"], "second": [], "pattern": "none", "require_status": "待补",
    }


# 三个院校，位次随年份**明显漂移**，用于验证「换窗口 → 结果变化」
#   院校A 位次：2024=6000 / 2025=3000 / 2026=1000  → 越近年越难（位次越小）
#   院校B 位次：2024=20000 / 2025=18000 / 2026=17000 → 越近年越难
# 考生位次 R=10000，冲档阈值 = 8500、稳档上限 = 12000
DRIFT_ROWS = [
    _admission_row(2024, "10001", "院校A", 201, 6000),
    _admission_row(2025, "10001", "院校A", 201, 3000),
    _admission_row(2026, "10001", "院校A", 201, 1000),
    _admission_row(2024, "10002", "院校B", 202, 20000),
    _admission_row(2025, "10002", "院校B", 202, 18000),
    _admission_row(2026, "10002", "院校B", 202, 17000),
]


@pytest.fixture
def repo() -> InMemoryDataVersionRepository:
    return InMemoryDataVersionRepository(active=_cfg(), admission_rows=DRIFT_ROWS)


@pytest.fixture
def service(repo: InMemoryDataVersionRepository) -> DataVersionService:
    return DataVersionService(repo)


# ============================================================
# J1 切换 ref_years 后推荐结果随之变化且可解释
# ============================================================


def test_j1_switch_window_changes_recommendation(repo: InMemoryDataVersionRepository) -> None:
    """只改参考窗口（不改算法系数）→ 参考位次随权重变化，档位随之变化。

    院校A：
      窗口 2024+2025+2026（0.2/0.3/0.5）→ L = 6000*0.2 + 3000*0.3 + 1000*0.5 = 2600 → 冲（≤8500）
      窗口 2024+2025      （0.2/0.3→归一化 0.4/0.6）→ L = 6000*0.4 + 3000*0.6 = 4200 → 冲（≤8500）
    两窗口档位相同但 L 不同；用院校B 验证「档位也会变」：
      窗口全量 → L = 20000*0.2+18000*0.3+17000*0.5 = 17900 → 保（12000<L≤20000）
      仅 2026  → L = 17000 → 保
    故另造一所位次横跨档位边界的院校 C 来验证档位迁移。
    """
    rows = [
        *DRIFT_ROWS,
        # 院校C：2025 位次 20000（保），2026 位次 3000（冲）→ 换窗口会跨档
        _admission_row(2025, "10003", "院校C", 203, 20000),
        _admission_row(2026, "10003", "院校C", 203, 3000),
    ]
    repo = InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows)
    svc = DataVersionService(repo)
    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))

    # ---- 窗口一：全量三年 ----
    cfg1 = svc.get_active()
    assert cfg1.ref_years == (2024, 2025, 2026)
    res1 = eng.recommend(
        score=600, subject_type="物理", rank=10000,
        current_year=cfg1.current_year, ref_years=list(cfg1.ref_years),
    )
    tier_c1 = _tier_of(res1, "院校C")
    # L = 0.3/0.8*20000 + 0.5/0.8*3000 = 7500 + 1875 = 9375 → 稳（8500 < L ≤ 12000）
    assert tier_c1 == "wen", f"预期稳档，实际 {tier_c1}"

    # ---- 切换到「只用 2026」----
    svc.switch(ref_years=[2026], year_weights=[1.0], actor="tester")
    cfg2 = svc.get_active()
    assert cfg2.ref_years == (2026,)
    res2 = eng.recommend(
        score=600, subject_type="物理", rank=10000,
        current_year=cfg2.current_year, ref_years=list(cfg2.ref_years),
    )
    tier_c2 = _tier_of(res2, "院校C")
    # L = 3000 → 冲（≤8500）
    assert tier_c2 == "chong", f"预期冲档，实际 {tier_c2}"

    # ---- 变化可解释：结果自带口径文字与参与年份 ----
    assert res1.ref_years_used == [2024, 2025, 2026]
    assert res2.ref_years_used == [2026]
    assert "2024–2026" in res1.caliber_text
    assert "2026" in res2.caliber_text
    assert res1.caliber_text != res2.caliber_text
    # 参考位次确实变了且方向符合预期（窗口收窄到最近年 → 院校C 更难）
    it_c1 = _item_of(res1, "院校C")
    it_c2 = _item_of(res2, "院校C")
    assert it_c2.ref_rank < it_c1.ref_rank


def test_j1_caliber_text_follows_active_config(repo: InMemoryDataVersionRepository) -> None:
    """口径文字必须与「当前生效配置」一致（防口径漂移）。"""
    svc = DataVersionService(repo)
    before = svc.caliber_text()
    assert "2024–2026" in before
    svc.switch(ref_years=[2025, 2026], year_weights=[0.4, 0.6], actor="tester")
    after = svc.caliber_text()
    assert "2025–2026" in after
    assert "2025:0.4" in after and "2026:0.6" in after
    assert before != after


def test_j1_recommend_result_carries_caliber_text() -> None:
    """推荐结果必须携带口径文字（红线：口径文字随结果返回）。"""
    eng = RecommendEngine(InMemoryRecommendRepository(DRIFT_ROWS, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    assert res.caliber_text
    assert "2024" in res.caliber_text
    assert "加权测算" in res.caliber_text


# ============================================================
# J2 使用未来年份数据被拦截
# ============================================================


def test_j2_switch_to_future_ref_year_rejected(repo: InMemoryDataVersionRepository) -> None:
    """ref_years 含 > current_year 的年份 → 拒绝切换，且**不落库**。"""
    svc = DataVersionService(repo)
    with pytest.raises(ValidationError) as ei:
        svc.switch(ref_years=[2026, 2027], year_weights=[0.5, 0.5], actor="tester")
    assert "时效红线" in str(ei.value) or "晚于" in str(ei.value)
    # 配置未被改动
    assert svc.get_active().ref_years == (2024, 2025, 2026)
    # 未写审计（拒绝的动作不留痕，避免噪声）
    assert repo.audits == []


def test_j2_future_current_year_with_old_window_ok(repo: InMemoryDataVersionRepository) -> None:
    """current_year 前进（滚动到 2027）后，2027 即可作为参考年份（不违反红线）。"""
    svc = DataVersionService(repo)
    saved = svc.switch(current_year=2027, ref_years=[2025, 2026, 2027], year_weights=[0.2, 0.3, 0.5],
                       actor="tester")
    assert saved.current_year == 2027
    assert 2027 in saved.ref_years


def test_j2_config_validate_blocks_future() -> None:
    """配置自身的时效守卫（不经过 service 也必须拦）。"""
    with pytest.raises(ValidationError):
        _cfg(current_year=2026, ref_years=(2024, 2027), weights=(0.5, 0.5)).validate()


def test_j2_trend_rejects_future_year(repo: InMemoryDataVersionRepository) -> None:
    """趋势对比同样禁止查询未来年份。"""
    svc = DataVersionService(repo)
    with pytest.raises(ValidationError) as ei:
        svc.trend(university_code="10001", group_code=201, subject_type="物理", years=[2026, 2027])
    assert "未来" in str(ei.value)


def test_j2_recommend_engine_blocks_all_future_ref_years() -> None:
    """推荐引擎侧守卫：ref_years 全为未来年份 → 显式报错（不返回空结果糊弄）。"""
    eng = RecommendEngine(InMemoryRecommendRepository(DRIFT_ROWS, {}))
    with pytest.raises(DataMissingError) as ei:
        eng.recommend(score=600, subject_type="物理", rank=10000,
                      current_year=2025, ref_years=[2026, 2027])
    assert ei.value.detail["code"] == "FUTURE_YEAR"


def test_j2_recommend_engine_filters_future_keep_valid() -> None:
    """部分未来年份 → 剔除未来年份，其余照常参与（结果只反映合法年份）。"""
    eng = RecommendEngine(InMemoryRecommendRepository(DRIFT_ROWS, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2025, ref_years=[2024, 2025, 2026])
    assert res.ref_years_used == [2024, 2025]
    assert 2026 not in res.ref_years_used


# ============================================================
# J3 趋势对比返回三年序列且方向正确
# ============================================================


def test_j3_trend_returns_three_year_series(repo: InMemoryDataVersionRepository) -> None:
    svc = DataVersionService(repo)
    t = svc.trend(university_code="10001", group_code=201, subject_type="物理")
    assert t.years == [2024, 2025, 2026]
    assert [p.min_rank for p in t.points] == [6000, 3000, 1000]
    assert t.university_name == "院校A"
    assert t.missing_years == []


def test_j3_trend_direction_up_when_rank_shrinks(repo: InMemoryDataVersionRepository) -> None:
    """位次逐年变小 = 越来越难 → direction 全为 up，整体 up。"""
    svc = DataVersionService(repo)
    t = svc.trend(university_code="10001", group_code=201, subject_type="物理")
    # 首年无上年可比 → flat；之后两年均 up
    assert [p.direction for p in t.points] == ["flat", "up", "up"]
    assert t.points[1].rank_delta == -3000
    assert t.points[2].rank_delta == -2000
    assert t.direction == "up"
    assert t.direction_text == "整体难度上升"


def test_j3_trend_direction_down_when_rank_grows() -> None:
    """位次逐年变大 = 越来越易进 → direction 全为 down。"""
    rows = [
        _admission_row(2024, "90001", "易进院校", 301, 5000),
        _admission_row(2025, "90001", "易进院校", 301, 9000),
        _admission_row(2026, "90001", "易进院校", 301, 15000),
    ]
    svc = DataVersionService(InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows))
    t = svc.trend(university_code="90001", group_code=301, subject_type="物理")
    assert [p.direction for p in t.points] == ["flat", "down", "down"]
    assert t.direction == "down"
    assert "下降" in t.direction_text


def test_j3_trend_flat_when_unchanged() -> None:
    rows = [
        _admission_row(2024, "90002", "稳定院校", 302, 8000),
        _admission_row(2025, "90002", "稳定院校", 302, 8000),
        _admission_row(2026, "90002", "稳定院校", 302, 8000),
    ]
    svc = DataVersionService(InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows))
    t = svc.trend(university_code="90002", group_code=302, subject_type="物理")
    assert [p.direction for p in t.points] == ["flat", "flat", "flat"]
    assert t.direction == "flat"


def test_j3_trend_missing_year_placeheld_not_interpolated() -> None:
    """缺年 → 序列中占位、标注 missing_years、**不插值不猜测**。"""
    rows = [
        _admission_row(2024, "90003", "缺年院校", 303, 7000),
        # 2025 缺失
        _admission_row(2026, "90003", "缺年院校", 303, 6000),
    ]
    svc = DataVersionService(InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows))
    t = svc.trend(university_code="90003", group_code=303, subject_type="物理")
    assert t.years == [2024, 2025, 2026]
    assert [p.min_rank for p in t.points] == [7000, None, 6000]
    assert t.points[1].direction == "unavailable"
    assert t.missing_years == [2025]
    assert "2025" in t.note and "未做插值" in t.note
    # 2026 的对比基准取「上一个有数据年份」2024 → 位次 7000→6000 = 变难
    assert t.points[2].rank_delta == -1000
    assert t.points[2].direction == "up"


def test_j3_trend_unknown_group_returns_empty_series(repo: InMemoryDataVersionRepository) -> None:
    """查无此专业组：返回占位序列 + 全 unavailable，**不抛错也不编造**。"""
    svc = DataVersionService(repo)
    t = svc.trend(university_code="99999", group_code=999, subject_type="物理")
    assert [p.min_rank for p in t.points] == [None, None, None]
    assert t.direction == "unavailable"
    assert t.missing_years == [2024, 2025, 2026]
    assert t.university_name == ""


def test_j3_trend_uses_active_window_by_default(repo: InMemoryDataVersionRepository) -> None:
    """不传 years 时按「当前生效窗口」出序列；切换窗口后序列随之变化。"""
    rows = [
        _admission_row(2024, "10001", "院校A", 201, 6000),
        _admission_row(2025, "10001", "院校A", 201, 3000),
        _admission_row(2026, "10001", "院校A", 201, 1000),
    ]
    svc = DataVersionService(InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows))
    assert svc.trend(university_code="10001", group_code=201, subject_type="物理").years == [2024, 2025, 2026]
    svc.switch(ref_years=[2025, 2026], year_weights=[0.4, 0.6], actor="tester")
    assert svc.trend(university_code="10001", group_code=201, subject_type="物理").years == [2025, 2026]


def test_j3_overall_direction_needs_two_valid_points(repo: InMemoryDataVersionRepository) -> None:
    """仅 1 个有效年份 → 无法判断整体方向（不硬编「持平」糊弄）。"""
    rows = [_admission_row(2025, "90004", "单点院校", 304, 5000)]
    svc = DataVersionService(InMemoryDataVersionRepository(active=_cfg(), admission_rows=rows))
    t = svc.trend(university_code="90004", group_code=304, subject_type="物理")
    assert t.direction == "unavailable"
    assert "无法判断" in t.direction_text


# ============================================================
# J4 切换动作落 audit_log
# ============================================================


def test_j4_switch_writes_audit(repo: InMemoryDataVersionRepository) -> None:
    svc = DataVersionService(repo)
    svc.switch(ref_years=[2025, 2026], year_weights=[0.4, 0.6], actor="admin-x", note="2026 数据入库")
    assert len(repo.audits) == 1
    a = repo.audits[0]
    assert a["action"] == "switch_year"
    assert a["actor"] == "admin-x"
    detail = json.loads(a["detail"])
    assert detail["before"]["ref_years"] == [2024, 2025, 2026]
    assert detail["after"]["ref_years"] == [2025, 2026]
    assert detail["note"] == "2026 数据入库"
    assert detail["snapshot_id"] == 1


def test_j4_rollback_writes_audit_and_restores(repo: InMemoryDataVersionRepository) -> None:
    svc = DataVersionService(repo)
    svc.switch(ref_years=[2026], year_weights=[1.0], actor="admin-x")
    assert svc.get_active().ref_years == (2026,)

    restored = svc.rollback(actor="admin-y")
    assert restored.ref_years == (2024, 2025, 2026)
    assert svc.caliber_text().startswith("本方案基于 2024–2026")

    assert [a["action"] for a in repo.audits] == ["switch_year", "rollback_year"]
    rb = json.loads(repo.audits[1]["detail"])
    assert rb["snapshot_id"] == 1
    assert rb["after"]["ref_years"] == [2024, 2025, 2026]
    assert repo.audits[1]["actor"] == "admin-y"


def test_j4_rollback_without_snapshot_raises(repo: InMemoryDataVersionRepository) -> None:
    """无存档时回滚必须显式报错（不静默成功）。"""
    svc = DataVersionService(repo)
    with pytest.raises(DataMissingError) as ei:
        svc.rollback()
    assert ei.value.detail["code"] == "NO_SNAPSHOT"
    assert repo.audits == []


def test_j4_rollback_specific_snapshot(repo: InMemoryDataVersionRepository) -> None:
    """多次切换后可指定回滚到任意历史存档。"""
    svc = DataVersionService(repo)
    svc.switch(ref_years=[2024, 2025], year_weights=[0.4, 0.6], actor="a1")   # snapshot 1
    svc.switch(ref_years=[2026], year_weights=[1.0], actor="a2")             # snapshot 2
    assert svc.get_active().ref_years == (2026,)

    restored = svc.rollback(1, actor="a3")   # 回到「只用 2024+2025」之前 → 原始三年
    assert restored.ref_years == (2024, 2025, 2026)


def test_j4_dry_run_does_not_persist_or_audit(repo: InMemoryDataVersionRepository) -> None:
    """dry_run 只校验、不落库、不写审计（供后台「预览影响面」）。"""
    svc = DataVersionService(repo)
    preview = svc.switch(ref_years=[2026], year_weights=[1.0], actor="admin", dry_run=True)
    assert preview.ref_years == (2026,)
    assert svc.get_active().ref_years == (2024, 2025, 2026)   # 未变
    assert repo.audits == []


# ============================================================
# 配置自洽性（逐条拒绝，不静默修正）
# ============================================================


@pytest.mark.parametrize(
    ("kwargs", "expect"),
    [
        ({"ref_years": (2024, 2025, 2026), "year_weights": (0.5, 0.5)}, "数量不一致"),
        ({"ref_years": (2024, 2025), "year_weights": (0.2, 0.2)}, "之和应为 1"),
        ({"ref_years": (2025, 2024), "year_weights": (0.5, 0.5)}, "从旧到新"),
        ({"ref_years": (2024, 2024), "year_weights": (0.5, 0.5)}, "重复"),
        ({"ref_years": (2024, 2025), "year_weights": (-0.5, 1.5)}, "不得为负"),
    ],
)
def test_config_validate_rejects_inconsistent(kwargs: dict, expect: str) -> None:
    cfg = DataVersionConfig(current_year=2026, **kwargs)
    with pytest.raises(ValidationError) as ei:
        cfg.validate()
    assert expect in str(ei.value)


def test_empty_ref_years_rejected() -> None:
    with pytest.raises(ValidationError):
        DataVersionConfig(current_year=2026, ref_years=(), year_weights=()).validate()


def test_config_from_strings_roundtrip() -> None:
    cfg = DataVersionConfig.from_strings(
        current_year=2026, ref_years="2024,2025,2026", year_weights="0.2,0.3,0.5",
    )
    cfg.validate()
    assert cfg.ref_years == (2024, 2025, 2026)
    assert cfg.weight_map == {2024: 0.2, 2025: 0.3, 2026: 0.5}
    assert cfg.span_text == "2024–2026"
    assert cfg.weights_text == "2024:0.2 / 2025:0.3 / 2026:0.5"


def test_config_fingerprint_is_stable_and_sensitive() -> None:
    a = _cfg()
    b = _cfg()
    c = _cfg(ref_years=(2024, 2025), weights=(0.4, 0.6))
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_get_active_falls_back_to_env_when_repo_empty() -> None:
    """``data_version`` 表为空 → 用 .env 默认口径兜底（首次部署不至于不可用）。"""
    svc = DataVersionService(InMemoryDataVersionRepository(active=None))
    cfg = svc.get_active()
    assert cfg.source == "env"
    assert cfg.current_year > 0
    assert len(cfg.ref_years) == len(cfg.year_weights)


def test_switch_inherits_unspecified_fields(repo: InMemoryDataVersionRepository) -> None:
    """只传 current_year → ref_years / year_weights 沿用当前值。"""
    svc = DataVersionService(repo)
    saved = svc.switch(current_year=2027, actor="tester")
    assert saved.current_year == 2027
    assert saved.ref_years == (2024, 2025, 2026)
    assert saved.year_weights == (0.2, 0.3, 0.5)


def test_switch_auto_balances_weights_on_count_change(repo: InMemoryDataVersionRepository) -> None:
    """只改 ref_years 数量、未给 weights → 自动等权重（避免直接报错，仍显式可追溯）。"""
    svc = DataVersionService(repo)
    saved = svc.switch(ref_years=[2025, 2026], actor="tester")
    assert saved.ref_years == (2025, 2026)
    assert saved.year_weights == (0.5, 0.5)


def test_switch_note_persisted(repo: InMemoryDataVersionRepository) -> None:
    svc = DataVersionService(repo)
    saved = svc.switch(ref_years=[2026], year_weights=[1.0], note="2026 投档线已入库", actor="tester")
    assert saved.note == "2026 投档线已入库"


# ============================================================
# 单例重置 // 提供者
# ============================================================


def test_static_provider_returns_fixed_config() -> None:
    cfg = _cfg()
    p = StaticDataVersionProvider(cfg)
    assert p.get_config() is cfg


def test_overall_direction_helper() -> None:
    from app.services.data_version import TrendPoint

    pts = [
        TrendPoint(year=2024, min_rank=None, min_score=None, plan_count=None),
        TrendPoint(year=2025, min_rank=None, min_score=None, plan_count=None),
    ]
    assert _overall_direction(pts) == "unavailable"


# ============================================================
# MySQL 端到端（无 MySQL 时自动跳过；验证 SQL 仓库可用）
# ============================================================


def _mysql_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import SessionLocal

        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_mysql = pytest.mark.skipif(not _mysql_available(), reason="本机无可用 MySQL，跳过端到端用例")


@requires_mysql
def test_sql_repo_roundtrip_data_version() -> None:
    """SQL 仓库：切换 → 读回；旧行 is_active 必须置 0（唯一有效行）。"""
    from sqlalchemy import select

    from app.db.models import DataVersion
    from app.db.session import SessionLocal
    from app.services.data_version import SqlDataVersionRepository

    repo = SqlDataVersionRepository()
    original = repo.get_active()
    svc = DataVersionService(repo)
    try:
        saved = svc.switch(ref_years=[2026], year_weights=[1.0], actor="pytest", note="T09 端到端")
        active = repo.get_active()
        assert active is not None
        assert active.row_id == saved.row_id
        assert active.ref_years == (2026,)

        with SessionLocal() as db:
            actives = db.execute(select(DataVersion).where(DataVersion.is_active.is_(True))).scalars().all()
        assert len(actives) == 1, "data_version 必须保持唯一有效行"
    finally:
        # 复原现场（避免污染后续用例 / 手工验证环境）
        if original is not None:
            repo.save_active(original, "pytest-restore")
        else:
            repo.save_active(DataVersionConfig.from_strings(
                current_year=2026, ref_years=[2024, 2025, 2026], year_weights=[0.2, 0.3, 0.5]
            ), "pytest-restore")


@requires_mysql
def test_sql_repo_trend_matches_csv_derived_rows() -> None:
    """SQL 仓库趋势查询：某真实专业组返回的年份序列与库内一致（字段可读）。"""
    from app.services.data_version import SqlDataVersionRepository

    repo = SqlDataVersionRepository()
    rows = repo.admission_trend(
        province="广东", subject_type="物理", university_code="10001",
        group_code=201, years=[2024, 2025, 2026],
    )
    # 该组合可能不存在；存在时必须返回结构化行（不做值断言，避免依赖具体数据）
    for r in rows:
        assert {"year", "min_rank", "min_score", "plan_count"} <= set(r)


# ============================================================
# T09 冒烟修复回归：切换窗口后档位内不得出现同院校重复霸榜
# ============================================================


def test_switch_window_no_duplicate_university_within_tier() -> None:
    """回归：同一院校多个专业组不得在单一档位内重复出现（T09 冒烟发现的真实缺陷）。

    原实现排序忽略 ``group_code``，切到「仅 2026 年」后同院校多专业组并列霸榜，
    名义 10 所实际只有 3–4 所不同院校。
    """
    rows = []
    # 同一院校（10001）6 个专业组，全部同层次；再加 4 所其他院校凑满一档
    for g in range(201, 207):
        rows.append(_admission_row(2026, "10001", "重复院校", g, 5000 + g))
    for i, code in enumerate(("10002", "10003", "10004", "10005")):
        rows.append(_admission_row(2026, code, f"其他院校{i}", 201, 5000 + i))

    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2026])

    all_items = [it for g in res.tiers for it in g.items]
    codes = [it.university_code for it in all_items]
    assert len(codes) == len(set(codes)), f"同院校在结果中重复出现：{codes}"

    chong = next(g for g in res.tiers if g.tier == "chong")
    chong_codes = [it.university_code for it in chong.items]
    assert len(chong_codes) == len(set(chong_codes))
    assert "10001" in chong_codes, "重复院校应保留（去重而非排除）"


def test_switch_window_tier_fills_to_limit_when_enough_distinct_schools() -> None:
    """去重后仍有足够不同院校 → 档位应填满 ``tier_limit``（不因去重而缩水）。"""
    rows = []
    for i in range(15):
        rows.append(_admission_row(2026, f"3{i:04d}", f"院校{i}", 201, 5000 + i))
    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2026], tier_limit=10)
    chong = next(g for g in res.tiers if g.tier == "chong")
    assert chong.count == 10


def test_tier_ordering_prefers_closer_to_center() -> None:
    """同层次同地域院校 → 越贴近档位中心的参考位次越靠前（排序有确定含义）。"""
    rows = [
        _admission_row(2026, "40001", "贴中心", 201, 7000),   # 冲档区间 0–8500，中心 10000
        _admission_row(2026, "40002", "很靠前", 201, 500),    # 更靠前 → 离中心更远
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2026])
    chong = next(g for g in res.tiers if g.tier == "chong")
    assert chong.items[0].university_name == "贴中心"


# ============================================================
# 辅助
# ============================================================


def _tier_of(result, name: str) -> str | None:
    for g in result.tiers:
        for it in g.items:
            if it.university_name == name:
                return g.tier
    return None


def _item_of(result, name: str):
    for g in result.tiers:
        for it in g.items:
            if it.university_name == name:
                return it
    raise AssertionError(f"结果中未找到 {name}")
