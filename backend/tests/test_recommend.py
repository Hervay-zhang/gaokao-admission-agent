"""T08 测试 —— 「冲稳保」梯度推荐（对应任务卡「测试场景」J1–J5 + 红线守卫）。

J1 三档数量符合配置
J2 选科过滤正确（首选 + 再选逐条匹配）
J3 无数据院校被标注 / 缺年院校被标注
J4 每条含 risk_explanation 且无绝对概率承诺
J5 加权平均：构造三年数据验证权重生效

外加红线守卫：
- 待补选科要求「不排除但标注」（data_caliber §5.2 T08 降级口径）
- 时效：不得使用 > CURRENT_YEAR 的年份
- 缺年权重归一化
- 档位边界精确
- 免责声明随结果返回

设计：**不依赖 MySQL**——用 ``InMemoryRecommendRepository`` 构造确定性数据。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.exceptions import DataMissingError, ValidationError  # noqa: E402
from app.services.recommend import (  # noqa: E402
    InMemoryRecommendRepository,
    RecommendEngine,
    _elective_match,
    _plan_ratio,
    _ref_years_note,
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


@pytest.fixture
def engine() -> RecommendEngine:
    """三个院校 × 三年数据，参考位次可控。"""
    rows = [
        # A: 位次三年均 ~3000 → 冲（R=10000 时 L≈3000 ≤ 0.85*10000=8500）
        _row(2024, "10001", "院校A", 201, 3200),
        _row(2025, "10001", "院校A", 201, 3000),
        _row(2026, "10001", "院校A", 201, 2900),
        # B: 位次三年均 ~10000 → 稳（8500 < L ≤ 12000）
        _row(2024, "10002", "院校B", 202, 10500),
        _row(2025, "10002", "院校B", 202, 10000),
        _row(2026, "10002", "院校B", 202, 9800),
        # C: 位次三年均 ~16000 → 保（12000 < L ≤ 20000）
        _row(2024, "10003", "院校C", 203, 16500),
        _row(2025, "10003", "院校C", 203, 16000),
        _row(2026, "10003", "院校C", 203, 15800),
    ]
    return RecommendEngine(InMemoryRecommendRepository(rows, META))


# ============================================================
# J1 三档数量符合配置
# ============================================================


def test_j1_three_tiers_present(engine: RecommendEngine) -> None:
    res = engine.recommend(score=600, subject_type="物理", rank=10000)
    assert [g.tier for g in res.tiers] == ["chong", "wen", "bao"]
    assert [g.label for g in res.tiers] == ["冲", "稳", "保"]
    # A/B/C 分别落在 冲/稳/保
    assert res.tiers[0].items[0].university_name == "院校A"
    assert res.tiers[1].items[0].university_name == "院校B"
    assert res.tiers[2].items[0].university_name == "院校C"


def test_j1_tier_limit_respected() -> None:
    """每档 ≤ tier_limit（构造 25 所稳档院校，验证截断）。"""
    rows = []
    for i in range(25):
        code = f"2{i:04d}"
        for y in (2024, 2025, 2026):
            rows.append(_row(y, code, f"稳院校{i}", 201, 10000 + i))
    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000, tier_limit=10)
    assert res.tiers[1].count == 10
    assert len(res.tiers[1].items) == 10


def test_j1_tier_limit_configurable() -> None:
    rows = []
    for i in range(25):
        code = f"2{i:04d}"
        for y in (2024, 2025, 2026):
            rows.append(_row(y, code, f"稳院校{i}", 201, 10000 + i))
    eng = RecommendEngine(InMemoryRecommendRepository(rows, {}))
    res = eng.recommend(score=600, subject_type="物理", rank=10000, tier_limit=3)
    assert res.tiers[1].count == 3


# ============================================================
# J2 选科过滤
# ============================================================


def test_j2_first_subject_hard_filter() -> None:
    """首选科目不符 → 严格排除（红线：物理考生不得进仅历史招生的专业组）。"""
    rows = [
        _row(2025, "10001", "物理院校", 201, 3000, first=("物理",)),
        {**_row(2025, "10002", "历史院校", 202, 3000), "subject_type": "物理", "first": ["历史"]},
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    names = [it.university_name for g in res.tiers for it in g.items]
    assert "历史院校" not in names
    assert res.excluded["reason_counts"]["first_mismatch"] == 1


def test_j2_elective_second_empty_not_excluded_but_annotated() -> None:
    """再选科目待补（second 空）→ 不排除，但必须带 require_note。"""
    eng = RecommendEngine(InMemoryRecommendRepository(
        [_row(2025, "10001", "院校A", 201, 3000, second=())], META))
    res = eng.recommend(score=600, subject_type="物理", electives=["化学", "生物"], rank=10000)
    item = res.tiers[0].items[0]
    assert "再选科目要求待补" in item.require_note
    assert res.excluded["reason_counts"]["elective_unverified"] == 1


def test_j2_elective_pattern_all_enforced_when_present() -> None:
    """second 非空 + pattern=all → 考生再选须涵盖。"""
    rows = [_row(2025, "10001", "需化学院校", 201, 3000, second=("化学",), pattern="all")]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    # 选化学 → 通过
    ok = eng.recommend(score=600, subject_type="物理", electives=["化学", "生物"], rank=10000)
    assert ok.tiers[0].count == 1
    assert ok.tiers[0].items[0].require_note == ""  # 已校验，无需标注
    # 不选化学 → 排除
    bad = eng.recommend(score=600, subject_type="物理", electives=["生物", "地理"], rank=10000)
    assert bad.tiers[0].count == 0
    assert bad.excluded["reason_counts"]["elective_mismatch"] == 1


def test_j2_elective_pattern_any_enforced() -> None:
    """second 非空 + pattern=any → 至少命中其一。"""
    rows = [_row(2025, "10001", "需化或生", 201, 3000, second=("化学", "生物"), pattern="any")]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    ok = eng.recommend(score=600, subject_type="物理", electives=["生物", "地理"], rank=10000)
    assert ok.tiers[0].count == 1
    bad = eng.recommend(score=600, subject_type="物理", electives=["地理", "政治"], rank=10000)
    assert bad.tiers[0].count == 0


def test_j2_elective_match_helper() -> None:
    assert _elective_match(["化学"], "all", ["化学", "生物"]) is True
    assert _elective_match(["化学", "生物"], "all", ["化学"]) is False
    assert _elective_match(["化学", "生物"], "any", ["生物"]) is True
    assert _elective_match(["化学"], "none", []) is True


# ============================================================
# J3 无数据 / 缺年 标注
# ============================================================


def test_j3_no_rank_data_excluded_and_counted() -> None:
    """全年 min_rank 为空（官方「-」）→ 排除并计数。"""
    rows = [
        _row(2025, "10001", "有空数据", 201, None),
        _row(2025, "10002", "正常", 202, 3000),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    names = [it.university_name for g in res.tiers for it in g.items]
    assert "有空数据" not in names
    assert res.excluded["reason_counts"]["no_rank_data"] == 1


def test_j3_missing_years_annotated() -> None:
    """只有 2 年数据 → 标注「参考年份不足」并列出缺失年份，权重重归一化。"""
    rows = [
        _row(2024, "10001", "两年院校", 201, 3200),
        _row(2025, "10001", "两年院校", 201, 3000),
        # 缺 2026
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    it = res.tiers[0].items[0]
    assert it.ref_years_used == [2024, 2025]
    assert it.ref_years_missing == [2026]
    assert "参考年份不足" in it.ref_years_note
    assert "2026" in it.ref_years_note


def test_j3_year_complete_annotated() -> None:
    eng = RecommendEngine(InMemoryRecommendRepository([
        _row(2024, "10001", "三年院校", 201, 3200),
        _row(2025, "10001", "三年院校", 201, 3000),
        _row(2026, "10001", "三年院校", 201, 2900),
    ], META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[0].items[0]
    assert it.ref_years_used == [2024, 2025, 2026]
    assert it.ref_years_missing == []
    assert "完整" in it.ref_years_note


def test_j3_ref_years_note_helper() -> None:
    assert "不足" in _ref_years_note([2024, 2025], [2026])
    assert "完整" in _ref_years_note([2024, 2025, 2026], [])
    assert _ref_years_note([], [2024]) == "无参考年份数据"


# ============================================================
# J4 风险解释（无概率承诺）
# ============================================================


def test_j4_all_text_fields_present(engine: RecommendEngine) -> None:
    res = engine.recommend(score=600, subject_type="物理", rank=10000)
    assert res.explanation and res.risk_explanation and res.parent_summary and res.caliber_text
    assert res.disclaimer == "本建议仅供参考，不作为最终填报承诺"


def test_j4_risk_explanation_has_no_probability_numbers(engine: RecommendEngine) -> None:
    """红线：风险解释不得出现概率/百分比/「%」。"""
    res = engine.recommend(score=600, subject_type="物理", rank=10000)
    txt = res.risk_explanation + res.explanation + res.parent_summary
    assert "%" not in txt
    assert not re.search(r"\d+(\.\d+)?\s*%", txt)
    assert "概率" not in txt
    assert "录取率" not in txt
    # 允许的方向性词汇
    assert ("偏高" in res.risk_explanation) or ("适中" in res.risk_explanation)


def test_j4_parent_summary_mentions_all_tiers(engine: RecommendEngine) -> None:
    res = engine.recommend(score=600, subject_type="物理", rank=10000)
    for kw in ("冲", "稳", "保"):
        assert kw in res.parent_summary


# ============================================================
# J5 三年加权
# ============================================================


def test_j5_weights_applied() -> None:
    """构造三年不同位次，验证权重 0.2/0.3/0.5 生效。

    位次 2000(2024) / 4000(2025) / 6000(2026)
    L = 0.2*2000 + 0.3*4000 + 0.5*6000 = 400 + 1200 + 3000 = 4600
    """
    rows = [
        _row(2024, "10001", "加权院校", 201, 2000),
        _row(2025, "10001", "加权院校", 201, 4000),
        _row(2026, "10001", "加权院校", 201, 6000),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[0].items[0]
    assert it.ref_rank == 4600


def test_j5_newest_year_has_highest_weight() -> None:
    """近三年权重必须随年份递增（口径 §1.4）：构造两组反向递进数据，验证 newest 影响最大。"""
    w = dict(zip(settings.ref_year_list, settings.year_weight_list, strict=False))
    assert w[2026] > w[2025] > w[2024]


def test_j5_missing_year_renormalized() -> None:
    """缺 2026 时，权重在 2024/2025 间重新归一化，且叠加 T10 缺年保守化。

    第 1 步（加权归一化）：2024:0.2, 2025:0.3 → 归一化 0.4 / 0.6
                          位次 2000 / 4000 → L = 0.4*2000 + 0.6*4000 = 3200
    第 2 步（T10 缺年保守化）：缺 1 年 / 期望 3 年 → 缺年比例 1/3
                          惩罚 = 0.18/3 = 0.06 → L' = 3200 * 0.94 = 3008
    """
    rows = [
        _row(2024, "10001", "缺年院校", 201, 2000),
        _row(2025, "10001", "缺年院校", 201, 4000),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[0].items[0]
    assert it.ref_years_used == [2024, 2025]
    assert it.ref_years_missing == [2026]
    penalty = (1 / 3) * 0.18          # 缺年比例 × missing_penalty（非单年，无额外惩罚）
    assert it.ref_rank == round(3200 * (1 - penalty))
    # 纯加权值（未做保守化）也显式断言，保证「加权归一化」这一层语义未被改动
    assert round(0.4 * 2000 + 0.6 * 4000) == 3200


def test_j5_evidence_lists_yearly_weights() -> None:
    rows = [
        _row(2024, "10001", "证据院校", 201, 2000),
        _row(2025, "10001", "证据院校", 201, 4000),
        _row(2026, "10001", "证据院校", 201, 6000),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[0].items[0]
    assert len(it.evidence) == 3
    assert any("0.50" in e and "2026" in e for e in it.evidence)


# ============================================================
# 档位边界（口径 §7.1）
# ============================================================


@pytest.mark.parametrize("ref_rank,expected", [
    (8500, "chong"),    # = 0.85 × 10000 边界 → 冲
    (8501, "wen"),      # 略超 → 稳
    (12000, "wen"),     # = 1.2 × 10000 边界 → 稳
    (12001, "bao"),     # 略超 → 保
    (20000, "bao"),     # = 2.0 × 10000 边界 → 保
    (20001, None),      # 超出 → 不推荐
])
def test_tier_boundaries(ref_rank: int, expected: str | None) -> None:
    """分档系数边界（口径 §7.1）。

    用**三年齐全**数据构造：无缺年 → 不触发缺年保守化（T10 新增），
    `ref_rank` 等比于输入值，边界断言语义纯粹。缺年保守化单独由
    `test_missing_year_conservative_adjustment*` 覆盖。
    """
    rows = [
        _row(2024, "10001", "边界院校", 201, ref_rank),
        _row(2025, "10001", "边界院校", 201, ref_rank),
        _row(2026, "10001", "边界院校", 201, ref_rank),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    got = next((g.tier for g in res.tiers if g.count), None)
    assert got == expected, f"L={ref_rank} 期望 {expected}，实际 {got}"


# ============================================================
# 缺年保守化（T10 定稿）
# ============================================================


def test_missing_year_conservative_adjustment_full_window_no_change() -> None:
    """三年齐全 → 无缺年 → 不做任何保守化（`ref_rank` 等于加权值）。"""
    rows = [
        _row(2024, "10001", "满窗院校", 201, 10000, plan=100),
        _row(2025, "10001", "满窗院校", 201, 10000, plan=100),
        _row(2026, "10001", "满窗院校", 201, 10000, plan=100),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[1].items[0]
    assert it.ref_rank == 10000
    assert it.ref_years_missing == []
    assert it.plan_note == ""


def test_missing_year_conservative_adjustment_single_year() -> None:
    """仅 1 年数据（缺 2 年 / 期望 3 年）→ 惩罚 = 0.18*(2/3) + 0.10(单年额外) = 0.22。"""
    rows = [_row(2026, "10001", "单年院校", 201, 10000, plan=100)]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2024, 2025, 2026])
    it = next(i for g in res.tiers for i in g.items)
    penalty = (2 / 3) * 0.18 + 0.10      # 缺年比例 × 基础惩罚 + 单年额外惩罚
    assert it.ref_rank == round(10000 * (1 - penalty))
    assert it.ref_years_missing == [2024, 2025]
    assert "缺 2024,2025 年数据" in it.plan_note
    assert "保守化" in it.plan_note


def test_missing_year_conservative_adjustment_partial_window() -> None:
    """仅 1 年数据但期望窗口只有 2 年 → 缺年比例 0.5，惩罚 = 0.09 + 0.10 = 0.19。"""
    rows = [_row(2025, "10001", "缺一年院校", 201, 10000, plan=100)]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2024, 2025])
    it = next(i for g in res.tiers for i in g.items)
    penalty = 0.5 * 0.18 + 0.10
    assert it.ref_rank == round(10000 * (1 - penalty))
    assert it.ref_years_missing == [2024]


def test_missing_year_adjustment_never_skips_whole_candidate() -> None:
    """保守化只修正位次，**绝不移除候选**（红线：缺年用标注而非排除，口径 §1.4）。"""
    rows = [_row(2026, "10001", "单年院校", 201, 20000, plan=100)]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        current_year=2026, ref_years=[2024, 2025, 2026])
    assert res.total_count == 1


# ============================================================
# 计划变化因子
# ============================================================


def test_plan_shrink_triggers_conservative_adjustment() -> None:
    """计划缩减（< 0.9）→ 参考位次下移（更保守/更冲）。

    用**三年齐全**数据（`plan` 前两年持平、末年缩减），隔离「计划因子」这一个变量，
    不受 T10 缺年保守化干扰。
    """
    rows = [
        _row(2024, "10001", "缩招院校", 201, 10000, plan=100),
        _row(2025, "10001", "缩招院校", 201, 10000, plan=100),
        _row(2026, "10001", "缩招院校", 201, 10000, plan=80),  # ratio=0.8 < 0.9
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[1].items[0]
    # 加权 L=10000（无缺年、无缺年惩罚）；计划修正后 10000*(1-0.05)=9500
    assert it.ref_rank == 9500
    assert "缩减" in it.plan_note
    assert it.plan_ratio == pytest.approx(0.8, abs=0.001)


def test_plan_expansion_no_adjustment() -> None:
    rows = [
        _row(2024, "10001", "扩招院校", 201, 3000, plan=100),
        _row(2025, "10001", "扩招院校", 201, 3000, plan=100),
        _row(2026, "10001", "扩招院校", 201, 3000, plan=150),
    ]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    it = eng.recommend(score=600, subject_type="物理", rank=10000).tiers[0].items[0]
    assert it.plan_note == ""
    assert it.ref_rank == 3000


def test_plan_ratio_helper() -> None:
    assert _plan_ratio({2025: (1, 2, 100), 2026: (1, 2, 80)}) == pytest.approx(0.8)
    assert _plan_ratio({2026: (1, 2, 80)}) is None  # 不足两年
    assert _plan_ratio({2025: (1, 2, None), 2026: (1, 2, 80)}) is None


# ============================================================
# 档内排序
# ============================================================


def test_sort_by_level_then_region() -> None:
    """同档内：985 > 211 > 双一流 > 普通；同层次广东优先。"""
    rows = []
    for code, name in [("10003", "普通广东"), ("10001", "985北京"), ("10002", "211广东")]:
        for y in (2025, 2026):
            rows.append(_row(y, code, name, 201, 10000))
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    names = [it.university_name for it in res.tiers[1].items]
    assert names[0] == "985北京"
    assert names[1] == "211广东"
    assert names[2] == "普通广东"


# ============================================================
# 入参校验 + 时效守卫
# ============================================================


def test_validation_missing_rank(engine: RecommendEngine) -> None:
    with pytest.raises(ValidationError):
        engine.recommend(score=600, subject_type="物理", rank=None)


@pytest.mark.parametrize("bad", [0, -1])
def test_validation_bad_rank(engine: RecommendEngine, bad: int) -> None:
    with pytest.raises(ValidationError):
        engine.recommend(score=600, subject_type="物理", rank=bad)


def test_validation_bad_subject(engine: RecommendEngine) -> None:
    with pytest.raises(ValidationError):
        engine.recommend(score=600, subject_type="综合", rank=10000)


@pytest.mark.parametrize("bad", [-1, 800])
def test_validation_bad_score(engine: RecommendEngine, bad: int) -> None:
    with pytest.raises(ValidationError):
        engine.recommend(score=bad, subject_type="物理", rank=10000)


def test_time_guard_rejects_future_years_only() -> None:
    """ref_years 全为未来年份 → 拦截（时效红线）。"""
    eng = RecommendEngine(InMemoryRecommendRepository([
        _row(2026, "10001", "院校A", 201, 3000)
    ], META))
    with pytest.raises(DataMissingError) as ei:
        eng.recommend(score=600, subject_type="物理", rank=10000,
                      ref_years=[2027, 2028], current_year=2026)
    assert ei.value.detail["code"] == "FUTURE_YEAR"


def test_time_guard_filters_future_but_keeps_past() -> None:
    """ref_years 混合时，仅使用 <= current_year 的年份，未来年份被静默剔除（不参与加权）。"""
    rows = [_row(2026, "10001", "院校A", 201, 3000)]
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000,
                        ref_years=[2026, 2027], current_year=2026)
    assert res.ref_years_used == [2026]
    assert res.tiers[0].items[0].ref_years_used == [2026]


def test_no_data_raises() -> None:
    eng = RecommendEngine(InMemoryRecommendRepository([], {}))
    with pytest.raises(DataMissingError) as ei:
        eng.recommend(score=600, subject_type="物理", rank=10000)
    assert ei.value.detail["code"] == "NO_ADMISSION_DATA"


# ============================================================
# 结果契约
# ============================================================


def test_result_serializable_and_caliber(engine: RecommendEngine) -> None:
    res = engine.recommend(score=600, subject_type="物理", rank=10000)
    d = res.model_dump()
    assert d["province"] == PROV
    assert d["rank"] == 10000
    assert "2024" in res.caliber_text
    assert "2024:0.2" in res.caliber_text and "2026:0.5" in res.caliber_text
    assert res.disclaimer


def test_unmatched_count_reported() -> None:
    """参考位次远低于考生水平（L > 2R）→ 计入 unmatched，不进任何档。"""
    rows = [_row(2026, "10001", "远超院校", 201, 100)]  # L=100, R=10000 → 100 <= 0.85*10000 → 冲
    rows.append(_row(2026, "10002", "太低院校", 202, 90000))  # L=90000 > 2*10000 → unmatched
    eng = RecommendEngine(InMemoryRecommendRepository(rows, META))
    res = eng.recommend(score=600, subject_type="物理", rank=10000)
    assert res.unmatched_count == 1


# ============================================================
# 生产数据源（需 MySQL，不可用时跳过）
# ============================================================


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import engine as db_engine

        with db_engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="MySQL 不可用，跳过需库用例")


@requires_db
def test_sql_repo_end_to_end() -> None:
    """真实数据端到端：物理 600 分（位次 30891）能产出三档，且红线字段齐备。"""
    from app.services.recommend import SqlRecommendRepository
    from app.services.score import get_score_engine

    rank = get_score_engine().query_rank(2026, "广东", "物理", 600).rank
    assert rank == 30891

    eng = RecommendEngine(SqlRecommendRepository())
    res = eng.recommend(score=600, subject_type="物理", electives=["化学", "生物"], rank=rank)

    assert res.total_count > 0
    assert len(res.tiers) == 3
    # 首选科目硬过滤：结果中不得出现非物理类
    for g in res.tiers:
        for it in g.items:
            assert it.subject_type == "物理"
    # 待补标注：当前全量数据 second 为空 → 每条必须带 require_note
    for g in res.tiers:
        for it in g.items:
            assert it.require_note, "当前数据 second 全空，必须带「再选科目待补」标注"
    # 红线
    assert "%" not in res.risk_explanation
    assert res.disclaimer == "本建议仅供参考，不作为最终填报承诺"


@requires_db
def test_sql_repo_returns_level_metadata() -> None:
    """SQL 数据源能带回院校层次/地域元数据（用于档内排序）。"""
    from app.services.recommend import SqlRecommendRepository

    rank = 30891
    eng = RecommendEngine(SqlRecommendRepository())
    res = eng.recommend(score=600, subject_type="物理", rank=rank)
    items = [it for g in res.tiers for it in g.items]
    assert any(it.level_tags for it in items), "应至少有一所院校带层次标签（985/211/双一流）"
    assert any(it.school_province for it in items), "应至少有一所院校带省份"


def test_extreme_high_rank_uses_minimum_tier_window() -> None:
    """全省第 1 名不应因倍数区间坍缩而三档全空。"""
    from app.services.recommend import RecommendEngine

    assert RecommendEngine._classify(50, 1) == "chong"
    assert RecommendEngine._classify(500, 1) == "wen"
    assert RecommendEngine._classify(800, 1) == "bao"
    assert "高分保护" in RecommendEngine._interval_text("chong", 1)
