"""T07 测试 —— 位次换算引擎（对应任务卡「测试场景」J1–J4）。

J1 已知分数 → 位次与官网一致（黄金样本回归）
J2 边界（最高分段 / 100 分 / <100 分 / 座次超界）
J3 跨年等位分合理（单调性 + 插值正确）
J4 缓存命中后结果一致（且能反映数据版本失效）

设计
----
- **不依赖 MySQL / Redis**：数据源用 ``InMemoryScoreRepository``（从真实 CSV 载入），
  缓存用注入的假 Redis（记录读写），因此单测完全离线、可复现。
- J1 的黄金样本取自 **T02 已验证的官方值**（与 validate_data.py 的 S6 回归同源），
  确保「分数→位次」与官网一致。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import DataMissingError, ValidationError  # noqa: E402
from app.services import score as score_mod  # noqa: E402
from app.services.score import (  # noqa: E402
    InMemoryScoreRepository,
    ScoreEngine,
    _interp_score,
)

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"
SEGMENTS_CSV = CLEANED / "score_segments.csv"
PROVINCE = "广东"


# ============================================================
# 夹具
# ============================================================


@pytest.fixture(scope="module")
def repo() -> InMemoryScoreRepository:
    return InMemoryScoreRepository.from_csv(str(SEGMENTS_CSV), province=PROVINCE)


@pytest.fixture
def engine(repo: InMemoryScoreRepository) -> ScoreEngine:
    """关闭缓存的引擎（纯算法验证）。"""
    return ScoreEngine(repo, use_cache=False)


@pytest.fixture(scope="module")
def raw_rows() -> list[tuple[int, str, int, int]]:
    rows: list[tuple[int, str, int, int]] = []
    with SEGMENTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        for rec in csv.DictReader(f):
            if rec["province"] == PROVINCE:
                rows.append((int(rec["year"]), rec["subject_type"], int(rec["score"]), int(rec["rank"])))
    return rows


# ============================================================
# J1 已知分数 → 位次 与官网一致
# ============================================================


# 黄金样本：来自 data_caliber §二 与 T02 的 S6 独立二次解析回归
GOLDEN_RANK = [
    # (year, subject, score, rank)
    (2026, "物理", 699, 20),
    (2026, "物理", 600, 30891),
    (2026, "物理", 500, 181455),
    (2026, "物理", 300, 407474),
    (2026, "物理", 100, 433366),
    (2025, "物理", 597, 29336),
    (2024, "历史", 665, 20),
]


@pytest.mark.parametrize("year,subject,score,rank", GOLDEN_RANK)
def test_j1_rank_matches_official(engine: ScoreEngine, year, subject, score, rank) -> None:
    res = engine.query_rank(year, PROVINCE, subject, score)
    assert res.rank == rank, f"{year}{subject}{score} 期望 rank={rank}，实际 {res.rank}"
    assert res.capped is False


def test_j1_all_rows_roundtrip(repo: InMemoryScoreRepository, engine: ScoreEngine) -> None:
    """全表自洽：每个 (年, 科类, 分数) 精确命中返回原 rank（无平滑、无插值）。"""
    checked = 0
    for year in (2024, 2025, 2026):
        for subject in ("物理", "历史"):
            for s, r in repo.list_segments(PROVINCE, year, subject):
                if s < score_mod.MIN_SCORE:
                    continue
                res = engine.query_rank(year, PROVINCE, subject, s)
                assert res.rank == r and res.capped is False
                checked += 1
    assert checked >= 3000, f"覆盖行数不足：{checked}"


# ============================================================
# J2 边界
# ============================================================


def test_j2_above_top_segment_capped(engine: ScoreEngine) -> None:
    """分数高于表中最高段 → 取最高段累计值 + capped，不外推。"""
    res = engine.query_rank(2026, PROVINCE, "物理", 700)  # 表首为 699
    assert res.rank == 20
    assert res.capped is True
    assert res.top_score == 699
    assert "公开数据上限" in res.note


def test_j2_at_top_segment_not_capped(engine: ScoreEngine) -> None:
    """恰好等于最高段（699）→ 精确命中，不算 capped。"""
    res = engine.query_rank(2026, PROVINCE, "物理", 699)
    assert res.rank == 20 and res.capped is False


def test_j2_full_marks_750_capped(engine: ScoreEngine) -> None:
    """满分 750（超出所有年份最高段）→ capped，取上限。"""
    res = engine.query_rank(2026, PROVINCE, "物理", 750)
    assert res.capped is True and res.rank == 20


def test_j2_min_score_100(engine: ScoreEngine) -> None:
    """100 分是官方下限，精确命中。"""
    res = engine.query_rank(2026, PROVINCE, "物理", 100)
    assert res.rank == 433366 and res.capped is False


def test_j2_below_min_score_raises(engine: ScoreEngine) -> None:
    """< 100 分 → DataMissingError（不虚构）。"""
    with pytest.raises(DataMissingError) as ei:
        engine.query_rank(2026, PROVINCE, "物理", 99)
    assert ei.value.detail["code"] == "BELOW_MIN_SCORE"


@pytest.mark.parametrize("bad_score", [-1, 800, 1000])
def test_j2_invalid_score_raises(engine: ScoreEngine, bad_score: int) -> None:
    with pytest.raises(ValidationError):
        engine.query_rank(2026, PROVINCE, "物理", bad_score)


def test_j2_bad_subject_raises(engine: ScoreEngine) -> None:
    with pytest.raises(ValidationError):
        engine.query_rank(2026, PROVINCE, "综合", 600)


def test_j2_missing_year_raises(engine: ScoreEngine) -> None:
    with pytest.raises(DataMissingError) as ei:
        engine.query_rank(2023, PROVINCE, "物理", 600)
    assert ei.value.detail["code"] == "NO_SEGMENT_DATA"


def test_j2_rank_bounds_for_equiv(engine: ScoreEngine) -> None:
    """位次 → 等位分 的上下界：优于最高段取最高分；低于最低段取最低分。"""
    top = engine.equiv_score(2024, PROVINCE, "历史", 20)   # 表首 rank=20
    assert top.score == 665.0 and top.capped is True
    bot = engine.equiv_score(2024, PROVINCE, "历史", 10_000_000)
    assert bot.score == 100.0 and bot.capped is True


@pytest.mark.parametrize("bad_rank", [0, -5])
def test_j2_invalid_rank_raises(engine: ScoreEngine, bad_rank: int) -> None:
    with pytest.raises(ValidationError):
        engine.equiv_score(2025, PROVINCE, "物理", bad_rank)


# ============================================================
# J3 跨年等位分（单调性 + 插值正确）
# ============================================================


def test_j3_interp_known_value(engine: ScoreEngine) -> None:
    """插值正确性：2025 物理 rank=30891 落在 (596:30131, 595:30924) 之间。

    修正后的线性插值（T23 修复段内方向缺陷）：
    596 - (596-595)*(30891-30131)/(30924-30131) = 596 - 0.958 ≈ 595.0
    （位次 30891 已进入 595 分段，等位分应接近 595 而非 597。）
    """
    res = engine.equiv_score(2025, PROVINCE, "物理", 30891)
    assert res.low_score == 595 and res.high_score == 596
    assert res.score == pytest.approx(595.0, abs=0.05)
    assert res.capped is False


def test_j3_monotonic_decreasing(engine: ScoreEngine) -> None:
    """位次越大 → 等位分越低（严格单调不增），覆盖 6 组 × 多点。"""
    for year in (2024, 2025, 2026):
        for subject in ("物理", "历史"):
            ranks = [100, 1000, 5000, 20000, 50000, 100000, 200000]
            scores = [engine.equiv_score(year, PROVINCE, subject, r).score for r in ranks]
            for a, b in zip(scores, scores[1:], strict=False):
                assert a >= b, f"{year}{subject} 非单调：{scores}"


def test_j3_roundtrip_score_rank_score(engine: ScoreEngine) -> None:
    """往返自洽：score → rank → equiv_score(同年)。

    口径说明（重要）：``rank`` 是「该分数及以上累计人数」= 位次**上限**，
    即该分数段**最后一名**的位次。故 ``rank`` 反查等位分时会略高于原始分数，
    落在 ``[score, score+2]`` 区间内属**正确**行为，不是误差。

    反例验证：2026 物理 600 → rank 30891（=≥600 累计）；反查恰落在段末边界
    → 等位分 600（601(29981)~600(30891) 段的 r_lo 端，ratio=1 → 601-1=600）。
    """
    cases = [
        (2026, "物理", 600),
        (2025, "物理", 597),
        (2024, "历史", 500),
    ]
    for year, subject, score in cases:
        rank = engine.query_rank(year, PROVINCE, subject, score).rank
        back = engine.equiv_score(year, PROVINCE, subject, rank)
        assert score <= back.score <= score + 2, f"{year}{subject}{score} 往返 {back.score} 超出 [score, score+2]"


def test_j3_cross_year_equiv(engine: ScoreEngine) -> None:
    """「2026 位次 → 各年等位分」批量映射，年份齐备且不抛错。"""
    res = engine.equiv_scores_across_years(12000, [2024, 2025, 2026], PROVINCE, "物理")
    assert [r.year for r in res] == [2024, 2025, 2026]
    assert all(r.score > 0 for r in res)


def test_j3_interp_helper_zero_person_segment() -> None:
    """零人段（相邻同累计）不得除零：直接取较高分。"""
    segs = [(600, 100), (599, 100), (598, 150)]  # 599 与 600 累计相同 → 零人段
    res = _interp_score(segs, 2026, PROVINCE, "物理", 100)
    assert res.score == 600.0


# ============================================================
# J4 缓存
# ============================================================


class _FakeRedis:
    """内存假 Redis（同步接口，够用；记录 get/set 次数）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key: str):
        self.gets += 1
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):  # noqa: A002
        self.sets += 1
        self.store[key] = value
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(score_mod, "get_redis", lambda: fake, raising=False)
    # app.core.redis.get_redis 在 _Cache 内部按需导入 → 打补丁到该模块
    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis", lambda: fake)
    return fake


def test_j4_cache_hit_consistent(repo: InMemoryScoreRepository, fake_redis) -> None:
    """首次未命中 → 直查并写缓存；二次命中 → 结果完全一致且不再查库。"""
    calls = {"n": 0}
    real_rank_at = repo.rank_at

    def counting_rank_at(*a, **kw):
        calls["n"] += 1
        return real_rank_at(*a, **kw)

    repo.rank_at = counting_rank_at  # type: ignore[method-assign]  # 测试期临时替换

    eng = ScoreEngine(repo, use_cache=True)
    r1 = eng.query_rank(2026, PROVINCE, "物理", 600)
    r2 = eng.query_rank(2026, PROVINCE, "物理", 600)

    assert r1 == r2
    assert r1.rank == 30891
    assert calls["n"] == 1, "第二次应命中缓存，不应再查库"
    assert fake_redis.sets == 1
    assert fake_redis.gets >= 2


def test_j4_equiv_cache_hit(fake_redis) -> None:
    """等位分缓存：重复请求命中缓存，结果一致。

    插值（修正方向后）：rank 30100 落在 (600:30000, 599:30200) 之间
    → 600 - (600-599)*(30100-30000)/(30200-30000) = 599.5
    """
    repo = InMemoryScoreRepository([(2025, "物理", 600, 30000), (2025, "物理", 599, 30200)], PROVINCE)
    eng = ScoreEngine(repo, use_cache=True)
    a = eng.equiv_score(2025, PROVINCE, "物理", 30100)
    b = eng.equiv_score(2025, PROVINCE, "物理", 30100)
    assert a == b
    assert a.low_score == 599 and a.high_score == 600
    assert a.score == pytest.approx(599.5, abs=0.01)


def test_j4_cache_invalidated_by_dataset_version(fake_redis) -> None:
    """数据版本变化 → 指纹变 → 旧键不再命中（缓存的失效策略）。"""
    rows_v1 = [(2025, "物理", 600, 30000), (2025, "物理", 599, 30200)]
    eng1 = ScoreEngine(InMemoryScoreRepository(rows_v1, PROVINCE), use_cache=True)
    eng1.query_rank(2025, PROVINCE, "物理", 600)

    # 数据变更（新增一行）→ 新引擎指纹不同
    rows_v2 = rows_v1 + [(2025, "物理", 598, 30500)]
    eng2 = ScoreEngine(InMemoryScoreRepository(rows_v2, PROVINCE), use_cache=True)
    res = eng2.query_rank(2025, PROVINCE, "物理", 600)
    assert res.rank == 30000  # 仍正确

    keys1 = {k.split(":")[2] for k in fake_redis.store}
    assert len(keys1) >= 2, "版本变化后应生成不同前缀的键"


def test_j4_cache_failure_degrades_gracefully(monkeypatch) -> None:
    """Redis 抛异常时，主流程不得中断（缓存是优化而非依赖）。"""
    def boom():
        raise RuntimeError("redis down")

    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis", boom)
    repo = InMemoryScoreRepository([(2025, "物理", 600, 30000)], PROVINCE)
    eng = ScoreEngine(repo, use_cache=True)
    assert eng.query_rank(2025, PROVINCE, "物理", 600).rank == 30000


def test_j4_version_fingerprint_stable(repo: InMemoryScoreRepository) -> None:
    """同一份数据两次计算指纹一致（指纹必须稳定，否则缓存永远不命中）。"""
    v1 = repo.dataset_version(PROVINCE)
    v2 = repo.dataset_version(PROVINCE)
    assert v1.fingerprint == v2.fingerprint
    assert len(v1.fingerprint) == 12
    assert v1.years == [2024, 2025, 2026]


# ============================================================
# 配置口径守卫（防止 .env 与 data_caliber 再次漂移）
# ============================================================


def test_config_year_caliber_consistent() -> None:
    """REF_YEARS 必须从旧到新、权重与之一一对应、不得引用未来年份。"""
    from app.core.config import settings

    years = settings.ref_year_list
    weights = settings.year_weight_list
    assert years == sorted(years), "REF_YEARS 必须从旧到新排列"
    assert len(years) == len(weights)
    assert abs(sum(weights) - 1.0) < 1e-6
    assert max(years) <= settings.current_year, "不得引用晚于 CURRENT_YEAR 的年份"
    # 越近权重越高（口径 data_caliber §1.4）
    assert weights[-1] == max(weights)


# ============================================================
# 生产数据源一致性（需 MySQL，不可用时跳过）
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
def test_sql_repo_parity_with_csv(repo: InMemoryScoreRepository) -> None:
    """SQL 数据源与 CSV 数据源结果必须完全一致（两条路径不得漂移）。"""
    from app.services.score import SqlScoreRepository

    sql_repo = SqlScoreRepository()
    eng_csv = ScoreEngine(repo, use_cache=False)
    eng_sql = ScoreEngine(sql_repo, use_cache=False)

    for year, subject, score, _ in GOLDEN_RANK:
        a = eng_csv.query_rank(year, PROVINCE, subject, score)
        b = eng_sql.query_rank(year, PROVINCE, subject, score)
        assert a.rank == b.rank and a.capped == b.capped, f"{year}{subject}{score}: CSV={a.rank} SQL={b.rank}"

    for year, subject, rank in [(2025, "物理", 30891), (2026, "历史", 5000), (2024, "物理", 100000)]:
        a = eng_csv.equiv_score(year, PROVINCE, subject, rank)
        b = eng_sql.equiv_score(year, PROVINCE, subject, rank)
        assert a.score == pytest.approx(b.score, abs=0.05)
        assert a.capped == b.capped


@requires_db
def test_sql_dataset_version_matches_csv(repo: InMemoryScoreRepository) -> None:
    """SQL 与 CSV 的版本指纹一致（行数/边界摘要相同）。"""
    from app.services.score import SqlScoreRepository

    v_csv = repo.dataset_version(PROVINCE)
    v_sql = SqlScoreRepository().dataset_version(PROVINCE)
    assert v_csv.fingerprint == v_sql.fingerprint, f"CSV={v_csv.fingerprint} SQL={v_sql.fingerprint}"
