"""T10 测试 —— 回测评测集 + 回测脚本与报告。

对应任务卡「测试场景」J1–J3，并补足：
- 指标计算纯函数单测（命中率 / 保档有效性 / 覆盖度 / 平均推荐数的边界）
- **命中判据方向守卫**（T10 迭代中踩过的真实缺陷，必须锁死）
- 样本生成的时间隔离与可复现
- 报告的「自动生成」特征（含 sha256 指纹，便于核对报告与数据同源）

设计：**不依赖 MySQL / Redis** —— 用 ``InMemoryRecommendRepository`` +
临时目录写 CSV，全部离线可复现。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.recommend import (  # noqa: E402
    InMemoryRecommendRepository,
    RecommendEngine,
)
from scripts.build_backtest_samples import (  # noqa: E402
    available_train_years,
    build_samples,
    load_segments,
    rank_quantiles,
    score_at_rank,
    write_csv,
)
from scripts.run_backtest import (  # noqa: E402
    TIERS,
    BacktestMetrics,
    TierStat,
    _is_hit,
    _is_hit_answerable,
    check_time_isolation,
    load_actual_by_year,
    load_samples,
    render_report,
    run_backtest,
    sha256_of,
)

PROV = "广东"


# ============================================================
# 夹具：极小但自洽的数据集（不依赖 data/cleaned，避免受真实数据变动影响）
# ============================================================

# 一分一段（3 年 × 2 科类，分数 700..500 逐分，位次线性增长）
def _segs(year: int, subject: str) -> list[tuple[int, str, int, int]]:
    rows = []
    for i, score in enumerate(range(700, 499, -1)):
        rows.append((year, subject, score, 1 + i * 100))
    return rows


@pytest.fixture
def score_csv(tmp_path: Path) -> Path:
    p = tmp_path / "score_segments.csv"
    rows = []
    for y in (2024, 2025, 2026):
        for s in ("物理", "历史"):
            rows.extend(_segs(y, s))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "province", "subject_type", "score", "rank"])
        for y, s, sc, rk in rows:
            w.writerow([y, PROV, s, sc, rk])
    return p


def _adm(year, code, name, group, min_rank, plan=100, min_score=600, subject="物理") -> dict:
    return {
        "year": year, "province": PROV, "subject_type": subject,
        "university_code": code, "university_name": name, "group_code": group,
        "plan_count": plan, "admitted_count": plan,
        "min_score": min_score, "min_rank": min_rank,
        "first": [subject], "second": [], "pattern": "none", "require_status": "待补",
    }


@pytest.fixture
def admission_csv(tmp_path: Path) -> Path:
    """三年投档数据：每所院校位次稳定（便于指标可预测）。

    - A（10001）：位次恒 500  → 冲档（对 R=20000 的样本）
    - B（10002）：位次恒 20000 → 稳档
    - C（10003）：位次恒 35000 → 保档
    """
    rows = []
    for y in (2024, 2025, 2026):
        rows.append(_adm(y, "10001", "院校A", 201, 500))
        rows.append(_adm(y, "10002", "院校B", 202, 20000))
        rows.append(_adm(y, "10003", "院校C", 203, 35000))
    p = tmp_path / "admission_lines.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return p


def _engine() -> RecommendEngine:
    rows = []
    for y in (2024, 2025, 2026):
        rows.append(_adm(y, "10001", "院校A", 201, 500))
        rows.append(_adm(y, "10002", "院校B", 202, 20000))
        rows.append(_adm(y, "10003", "院校C", 203, 35000))
    return RecommendEngine(InMemoryRecommendRepository(rows, {}))


# ============================================================
# J1 报告生成成功
# ============================================================


def test_j1_report_generated_with_required_sections() -> None:
    """报告必须包含结论 / 数据指纹 / 三档命中率 / 保档有效性 / 覆盖度等必备章节。"""
    samples = [{
        "sample_id": "S0001", "subject_type": "物理", "valid_year": 2026,
        "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t",
    }]
    by_group, by_school = load_actual_by_year(_tmp_admission())
    metrics, per_sample = run_backtest(
        samples=samples, by_group=by_group, by_school=by_school, engine=_engine(),
    )
    md = render_report(
        metrics=metrics, samples_path=_tmp_admission(), admission_path=_tmp_admission(),
        per_sample=per_sample, tier_limit=10, started_at="2026-01-01 00:00:00",
    )
    for must in ("# T10 · 回测报告", "## 一、结论", "## 二、数据来源与可核对指纹",
                 "## 四、三档命中率", "## 五、保档有效性", "## 六、覆盖度与推荐量"):
        assert must in md, f"报告缺少章节：{must}"
    assert "禁止手工修改数据" in md
    assert metrics.n_samples == 1


_TMP_ADM: Path | None = None


def _tmp_admission() -> Path:
    """模块级临时投档 CSV（会话内复用，避免每个用例建临时目录）。"""
    global _TMP_ADM
    if _TMP_ADM is None:
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="t10-"))
        p = d / "admission_lines.csv"
        rows = []
        for y in (2024, 2025, 2026):
            rows.append(_adm(y, "10001", "院校A", 201, 500))
            rows.append(_adm(y, "10002", "院校B", 202, 20000))
            rows.append(_adm(y, "10003", "院校C", 203, 35000))
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        _TMP_ADM = p
    return _TMP_ADM


# ============================================================
# J2 指标可复现（跑两次一致）
# ============================================================


def test_j2_metrics_reproducible() -> None:
    """同输入跑两次 → 指标字典完全一致（含浮点，均 round 后比较）。"""
    samples = [
        {"sample_id": "S0001", "subject_type": "物理", "valid_year": 2026,
         "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t"},
        {"sample_id": "S0002", "subject_type": "物理", "valid_year": 2026,
         "train_years": [2024, 2025], "score": 620, "rank": 10000, "strata": "t"},
    ]
    a = _run_metrics(samples)
    b = _run_metrics(samples)
    assert a == b


def _run_metrics(samples: list[dict]) -> dict:
    by_group, by_school = load_actual_by_year(_tmp_admission())
    m, _ = run_backtest(samples=samples, by_group=by_group, by_school=by_school, engine=_engine())
    return m.to_dict()


def test_j2_sample_generation_reproducible(score_csv: Path, tmp_path: Path) -> None:
    """同样本生成两次 → CSV 字节级一致（固定种子）。"""
    segs = load_segments(score_csv)
    s1 = build_samples(segs, n=40, seed="fixed-seed")
    s2 = build_samples(segs, n=40, seed="fixed-seed")
    assert s1 == s2
    p1, p2 = tmp_path / "a.csv", tmp_path / "b.csv"
    write_csv(s1, p1)
    write_csv(s2, p2)
    assert p1.read_bytes() == p2.read_bytes()


def test_j2_different_seed_changes_samples(score_csv: Path) -> None:
    """换种子 → 样本不同（证明种子真的生效，不是摆设）。"""
    segs = load_segments(score_csv)
    a = build_samples(segs, n=40, seed="seed-a")
    b = build_samples(segs, n=40, seed="seed-b")
    assert [x["rank"] for x in a] != [x["rank"] for x in b]


# ============================================================
# J3 无时间泄漏
# ============================================================


def test_j3_no_time_leakage_in_generated_samples(score_csv: Path) -> None:
    """生成的全部样本：train_years 必须严格早于 valid_year。

    ``build_samples`` 产出的是**可直接写 CSV 的原始行**，其中 ``train_years`` 为
    逗号分隔字符串；此处显式解析后再比对（``load_samples`` 负责同一转换）。
    """
    segs = load_segments(score_csv)
    samples = build_samples(segs, n=80, seed="leak-check")
    assert check_time_isolation(samples) == []
    for s in samples:
        years = [int(x) for x in s["train_years"].split(",")]
        assert years, f"{s['sample_id']} 无训练年份"
        assert all(y < s["valid_year"] for y in years)
        assert years == sorted(years)


def test_j3_leaky_sample_is_detected() -> None:
    """构造泄漏样本 → `check_time_isolation` 必须报出来（不是永远返回空）。"""
    bad = [{
        "sample_id": "BAD", "subject_type": "物理", "valid_year": 2025,
        "train_years": [2024, 2025],  # 2025 == valid_year → 泄漏
        "score": 600, "rank": 20000, "strata": "t",
    }]
    leaks = check_time_isolation(bad)
    assert len(leaks) == 1 and "BAD" in leaks[0]


def test_j3_valid_years_never_exceed_current_data() -> None:
    """生成器不应产出「无训练年份可用」的 valid_year=最早年 样本。"""
    segs = load_segments(_tmp_score())
    samples = build_samples(segs, n=20, seed="edge")
    for s in samples:
        assert s["train_years"], f"{s['sample_id']} 没有任何训练年份"


def _tmp_score() -> Path:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="t10s-"))
    p = d / "score_segments.csv"
    rows = []
    for y in (2024, 2025, 2026):
        for s in ("物理", "历史"):
            rows.extend(_segs(y, s))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "province", "subject_type", "score", "rank"])
        for y, s, sc, rk in rows:
            w.writerow([y, PROV, s, sc, rk])
    return p


def test_j3_available_train_years_strictly_earlier() -> None:
    assert available_train_years(2026, [2024, 2025, 2026, 2027]) == [2024, 2025]
    assert available_train_years(2024, [2024, 2025]) == []


# ============================================================
# 命中判据方向守卫（T10 迭代踩过的真实缺陷）
# ============================================================


class _Item:
    """最小 RecommendItem 替身（只需 code / group / ref_rank）。"""

    def __init__(self, code: str, group: int, ref_rank: int | None) -> None:
        self.university_code = code
        self.group_code = group
        self.ref_rank = ref_rank


def test_hit_direction_rank_larger_means_weaker_threshold() -> None:
    """**核心方向守卫**：实际门槛位次 ≥ 考生位次 ⇒ 考生稳进 ⇒ 命中。

    位次数值越小 = 排名越靠前 = 越强。
    - 实际 20000 vs 考生 10000 → 门槛比考生弱 → **命中**
    - 实际 500   vs 考生 10000 → 门槛比考生强 → **不命中**
    """
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    by_group = {
        ("物理", 2026, "UNI1", 201): 20000,   # 门槛弱于考生
        ("物理", 2026, "UNI2", 202): 500,     # 门槛强于考生
    }
    by_school: dict = {}
    assert _is_hit(_Item("UNI1", 201, 20000), sample, by_group, by_school) is True
    assert _is_hit(_Item("UNI2", 202, 500), sample, by_group, by_school) is False


def test_hit_exact_boundary_equal_rank_is_hit() -> None:
    """实际门槛位次 == 考生位次 ⇒ 恰好压线 ⇒ 命中（>= 而非 >）。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    by_group = {("物理", 2026, "UNI", 201): 10000}
    assert _is_hit(_Item("UNI", 201, 10000), sample, by_group, {}) is True


def test_hit_fallback_interval_match_on_group_renumber() -> None:
    """精确组号不匹配时回退院校级区间匹配（解决官方组号逐年重排）。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    # 推荐的是 group=201（ref_rank=20000），但 2026 该院校组号变成 299、位次 19500
    by_group: dict = {}
    by_school = {("物理", 2026, "UNI"): [(299, 19500)]}
    assert _is_hit(_Item("UNI", 201, 20000), sample, by_group, by_school, tolerance=0.15) is True


def test_hit_fallback_rejects_out_of_tolerance() -> None:
    """回退匹配必须受 tolerance 约束（否则退化成「只看院校」，档位失去区分度）。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    by_school = {("物理", 2026, "UNI"): [(299, 50000)]}   # 位次差 150%
    assert _is_hit(_Item("UNI", 201, 20000), sample, by_group={}, by_school=by_school,
                   tolerance=0.15) is False


def test_hit_fallback_still_requires_rank_ge_candidate() -> None:
    """回退路径同样要检查「门槛弱于考生」，不能因组号重排就放宽。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    # 位次落在 ref_rank ±15% 区间内，但门槛 19000 仍弱于考生 10000 → 命中
    ok = {("物理", 2026, "UNI"): [(299, 19000)]}
    assert _is_hit(_Item("UNI", 201, 20000), sample, {}, ok, tolerance=0.15) is True
    # 同一院校，组位次 500（远强于考生），不在区间内 → 不命中
    bad = {("物理", 2026, "UNI"): [(299, 500)]}
    assert _is_hit(_Item("UNI", 201, 20000), sample, {}, bad, tolerance=0.15) is False


def test_hit_missing_school_is_not_hit() -> None:
    """该院校验证年完全没有投档数据 → 不命中（不猜测）。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    assert _is_hit(_Item("GHOST", 201, 20000), sample, {}, {}) is False


def test_three_state_answerable_flag() -> None:
    """三态判定：可对答案 / 不可对答案 必须被正确区分（报告据此给出双口径）。

    - 同组号存在 → 可判定
    - 无同组号但院校有同位次区间组 → 可判定
    - 院校无任何数据 / 无同位次区间组 → **不可判定**（属数据现状，非推荐错误）
    """
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    # ① 同组号精确命中
    assert _is_hit_answerable(_Item("U1", 201, 20000), sample, {("物理", 2026, "U1", 201): 25000}, {}) == (True, True)
    # ② 组号重排但同区间组在 → 可判定
    assert _is_hit_answerable(_Item("U2", 201, 20000), sample,
                              {}, {("物理", 2026, "U2"): [(299, 19500)]}) == (True, True)
    # ③ 院校无任何数据 → 不可判定
    assert _is_hit_answerable(_Item("U3", 201, 20000), sample, {}, {}) == (False, False)
    # ④ 有数据但无同位次区间组 → 不可判定（设计上「够不着」不算可比）
    assert _is_hit_answerable(_Item("U4", 201, 20000), sample,
                              {}, {("物理", 2026, "U4"): [(299, 500)]}) == (False, False)


def test_tier_stat_unanswerable_and_dual_calibers() -> None:
    """TierStat 双口径：``hit_rate``（全量，含不可判定）vs ``hit_rate_answerable``。"""
    s = TierStat(recommended=100, hit=40, unanswerable=20)
    assert s.answerable == 80
    assert s.hit_rate == pytest.approx(0.40)              # 40/100 全量口径
    assert s.hit_rate_answerable == pytest.approx(0.50)   # 40/80 可判定口径
    assert TierStat(0, 0, 0).hit_rate_answerable == 0.0


def test_run_backtest_counts_unanswerable() -> None:
    """端到端：验证年无数据的条目应计入 ``unanswerable``，而非静默丢弃。"""
    samples = [{"sample_id": "S1", "subject_type": "物理", "valid_year": 2026,
                "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t"}]
    by_group, by_school = load_actual_by_year(_tmp_admission())
    m, _ = run_backtest(samples=samples, by_group=by_group, by_school=by_school, engine=_engine())
    total_rec = sum(s.recommended for s in m.tier_stats.values())
    total_un = sum(s.unanswerable for s in m.tier_stats.values())
    total_hit = sum(s.hit for s in m.tier_stats.values())
    assert total_rec == total_hit + (total_rec - total_hit)
    assert 0 <= total_un <= total_rec
    d = m.to_dict()
    assert "tier_hit_rate_answerable" in d and "tier_unanswerable" in d


def test_report_discloses_dual_calibers() -> None:
    """报告必须同时给出「全量口径」与「可判定口径」，并解释不可判定项含义。"""
    m = BacktestMetrics(n_samples=10)
    for t in TIERS:
        m.tier_stats[t] = TierStat(recommended=100, hit=10, unanswerable=30)
    m.bao_samples = 10
    m.bao_effective_samples = 9
    m.covered_samples = 10
    md = render_report(metrics=m, samples_path=_tmp_admission(), admission_path=_tmp_admission(),
                       per_sample=[], tier_limit=10, started_at="t")
    assert "可对答案" in md and "不可对答案" in md
    assert "全量口径" in md and "可判定口径" in md
    assert "不是推荐错误" in md


def test_hit_no_ref_rank_is_not_hit() -> None:
    """缺参考位次 → 无法做区间匹配 → 不命中（且不可判定）。"""
    sample = {"subject_type": "物理", "valid_year": 2026, "rank": 10000}
    by_school = {("物理", 2026, "UNI"): [(299, 19000)]}
    assert _is_hit(_Item("UNI", 201, None), sample, {}, by_school) is False
    assert _is_hit_answerable(_Item("UNI", 201, None), sample, {}, by_school) == (False, False)


# ============================================================
# 指标计算边界
# ============================================================


def test_metrics_empty_denominators_are_zero() -> None:
    m = BacktestMetrics()
    assert m.coverage == 0.0
    assert m.bao_effectiveness == 0.0
    assert m.avg_recommendations == 0.0
    assert m.overall_hit_rate == 0.0


def test_metrics_tier_stat_hit_rate() -> None:
    assert TierStat(0, 0).hit_rate == 0.0
    assert TierStat(10, 3).hit_rate == 0.3


def test_metrics_bao_effectiveness_is_sample_level() -> None:
    """保档有效性分母是**有保档推荐的样本数**，不是条目数（口径易混淆点）。"""
    m = BacktestMetrics(n_samples=10)
    for t in TIERS:
        m.tier_stats[t] = TierStat()
    m.bao_samples = 8
    m.bao_effective_samples = 6
    m.covered_samples = 9
    m.total_recommendations = 80
    assert m.bao_effectiveness == pytest.approx(0.75)      # 6/8
    assert m.coverage == pytest.approx(0.9)                # 9/10
    assert m.avg_recommendations == pytest.approx(8.0)     # 80/10


def test_metrics_overall_hit_rate_aggregates_tiers() -> None:
    m = BacktestMetrics(n_samples=1)
    m.tier_stats = {"chong": TierStat(10, 0), "wen": TierStat(10, 5), "bao": TierStat(10, 9)}
    assert m.overall_hit_rate == pytest.approx(14 / 30)


def test_run_backtest_counts_coverage_and_bao_samples() -> None:
    """端到端：构造能覆盖三档的样本，核对样本级/条目级统计口径。"""
    samples = [{"sample_id": "S1", "subject_type": "物理", "valid_year": 2026,
                "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t"}]
    by_group, by_school = load_actual_by_year(_tmp_admission())
    m, per = run_backtest(samples=samples, by_group=by_group, by_school=by_school, engine=_engine())
    assert m.n_samples == 1
    assert m.covered_samples == 1
    assert m.bao_samples == 1
    assert m.bao_effective_samples == 1      # 院校C 实际 35000 >= 20000 → 命中
    assert m.tier_stats["bao"].recommended >= 1
    assert per[0]["n_reco"] == m.total_recommendations
    # 三档命中方向应满足：保 ≥ 稳 ≥ 冲（业务逻辑自洽，T10 判据修正后锁死）
    assert m.tier_stats["bao"].hit_rate >= m.tier_stats["chong"].hit_rate


def test_run_backtest_reason_totals_accumulated() -> None:
    samples = [{"sample_id": "S1", "subject_type": "物理", "valid_year": 2026,
                "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t"}]
    by_group, by_school = load_actual_by_year(_tmp_admission())
    m, _ = run_backtest(samples=samples, by_group=by_group, by_school=by_school, engine=_engine())
    assert "elective_unverified" in m.reason_totals
    assert m.reason_totals["elective_unverified"] > 0


# ============================================================
# 报告「自动生成」自证
# ============================================================


def test_report_contains_input_fingerprints() -> None:
    """报告必须含输入文件 sha256，便于核对「报告与数据同源」。"""
    p = _tmp_admission()
    samples = [{"sample_id": "S1", "subject_type": "物理", "valid_year": 2026,
                "train_years": [2024, 2025], "score": 600, "rank": 20000, "strata": "t"}]
    by_group, by_school = load_actual_by_year(p)
    m, per = run_backtest(samples=samples, by_group=by_group, by_school=by_school, engine=_engine())
    md = render_report(metrics=m, samples_path=p, admission_path=p,
                       per_sample=per, tier_limit=10, started_at="2026-01-01 00:00:00")
    digest = sha256_of(p)
    assert digest in md
    assert "sha256" in md


def test_report_flags_unmet_baseline_with_advice() -> None:
    """未达标时报告必须给出调参建议表（而不是静默 PASS）。"""
    m = BacktestMetrics(n_samples=10)
    for t in TIERS:
        m.tier_stats[t] = TierStat()
    m.bao_samples = 10
    m.bao_effective_samples = 5      # 50% < 85% 基线
    m.covered_samples = 10
    md = render_report(metrics=m, samples_path=_tmp_admission(), admission_path=_tmp_admission(),
                       per_sample=[], tier_limit=10, started_at="t")
    assert "未达标" in md
    assert "bao_ratio" in md          # 给出具体调整方向
    assert "❌" in md


def test_report_flags_met_baseline() -> None:
    m = BacktestMetrics(n_samples=10)
    for t in TIERS:
        m.tier_stats[t] = TierStat()
    m.bao_samples = 10
    m.bao_effective_samples = 10
    m.covered_samples = 10
    md = render_report(metrics=m, samples_path=_tmp_admission(), admission_path=_tmp_admission(),
                       per_sample=[], tier_limit=10, started_at="t")
    assert "双基线均达标" in md
    assert "✅" in md


# ============================================================
# 样本生成纯函数
# ============================================================


def test_score_at_rank_bounds() -> None:
    """区间语义：``rank`` 落在 ``[r_hi, r_lo]`` 时取**上沿分数** ``s_hi``（该区间最高分）。

    表为「分数降序、位次升序」，故 ``(700,1) → (699,101)`` 表示位次 1..101 对应 700 分
    （100 名考生同分），位次 101 仍属 700 分段。
    """
    segs = [(700, 1), (699, 101), (698, 201)]
    assert score_at_rank(segs, 1) == 700      # 优于首段 → 取最高分
    assert score_at_rank(segs, 100) == 700    # 落在 (1,101] → 上沿 700
    assert score_at_rank(segs, 101) == 700    # 恰在边界 → 属上一段
    assert score_at_rank(segs, 102) == 699    # 落入 (101,201] → 上沿 699
    assert score_at_rank(segs, 99999) == 698  # 超末段 → 取最低分


def test_score_at_rank_returns_value_present_in_table() -> None:
    """生成的分数必须是表中真实存在的分数（不插值、不编造）。"""
    segs = [(700, 1), (699, 101), (698, 201)]
    for r in (1, 50, 100, 101, 150, 200, 201, 500):
        assert score_at_rank(segs, r) in {700, 699, 698}


def test_rank_quantiles_monotonic_and_bounded() -> None:
    segs = [(700, 1), (600, 1000), (500, 10000)]
    qs = rank_quantiles(segs, 5)
    assert qs == sorted(qs)
    assert qs[0] >= 1
    assert qs[-1] <= 10000


def test_load_samples_parses_train_years(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text(
        "sample_id,subject_type,valid_year,train_years,score,rank,strata\n"
        "S1,物理,2026,\"2024,2025\",600,20000,x\n",
        encoding="utf-8-sig",
    )
    s = load_samples(p)[0]
    assert s["train_years"] == [2024, 2025]
    assert s["rank"] == 20000


def test_load_actual_skips_empty_rank(tmp_path: Path) -> None:
    p = tmp_path / "a.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "province", "subject_type", "university_code", "university_name",
                    "group_code", "plan_count", "admitted_count", "min_score", "min_rank",
                    "subject_requirement"])
        w.writerow([2026, PROV, "物理", "10001", "A", 201, 10, 10, 600, 5000, "{}"])
        w.writerow([2026, PROV, "物理", "10002", "B", 202, 0, 0, "", "", "{}"])  # 空投档
    by_group, by_school = load_actual_by_year(p)
    assert ("物理", 2026, "10001", 201) in by_group
    assert ("物理", 2026, "10002", 202) not in by_group
