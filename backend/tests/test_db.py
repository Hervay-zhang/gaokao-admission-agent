"""T04 测试 —— MySQL 建模 + 迁移 + 幂等导入（对应任务卡「测试场景」J1–J4）。

J1 行数与 CSV 一致（3503 / 1095 / 15868）
J2 幂等：重跑行数不变（且内容一致）
J3 索引生效（EXPLAIN 走索引，非全表扫描）
J4 downgrade 后表清空、可重新 upgrade（迁移可回滚）

设计
----
- 无 MySQL 时**整体跳过**（不判失败），与 T01 的 J2 处理一致；
- 需真实库的用例通过 `pytest.mark.skipif` 守卫；
- 只读断言为主（重导由 `import_data.py` 保证幂等），避免测试污染开发库。
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import engine  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent
CLEANED = PROJECT_ROOT / "data" / "cleaned"

EXPECTED = {"score_segments": 3503, "universities": 1095, "admission_lines": 15868}

REQUIRED_TABLES = {
    "provinces", "score_segments", "universities", "admission_lines",
    "policy_docs", "policy_chunks", "sessions", "messages", "user_profiles",
    "recommendations", "report_snapshots", "volunteer_plans", "volunteer_items",
    "feedback", "audit_log", "data_version", "admin_user",
    "forum_posts", "forum_comments", "forum_comment_likes",
    "forum_post_likes", "forum_post_favorites", "community_reports",
    "quick_links", "consultation_messages", "expert_appointments", "user_accounts",
}


def _db_available() -> bool:
    """能否连上 MySQL（决定是否跳过需库用例）。"""
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


DB_OK = _db_available()
requires_db = pytest.mark.skipif(not DB_OK, reason="MySQL 不可用，跳过需库用例")
RUN_DESTRUCTIVE_DB_TESTS = os.getenv("RUN_DESTRUCTIVE_DB_TESTS") == "1"
requires_destructive_db = pytest.mark.skipif(
    not RUN_DESTRUCTIVE_DB_TESTS,
    reason="破坏性迁移测试需显式设置 RUN_DESTRUCTIVE_DB_TESTS=1，并先停止 API/DB 客户端",
)


def _csv_rows(name: str) -> list[dict[str, str]]:
    with (CLEANED / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ==================== 模型层（不依赖库）====================


def test_models_table_inventory() -> None:
    """模型层表清单与当前迁移头一致。"""
    from app.db.models import Base

    names = set(Base.metadata.tables.keys())
    assert names == REQUIRED_TABLES, f"表清单不一致：缺 {REQUIRED_TABLES - names}；多 {names - REQUIRED_TABLES}"


def test_models_no_float_columns() -> None:
    """红线：分数/位次/计划数禁 FLOAT/DOUBLE —— 模型层不得出现浮点列类型。"""
    from sqlalchemy import Float

    from app.db.models import Base

    bad: list[str] = []
    for tname, table in Base.metadata.tables.items():
        for col in table.columns:
            if isinstance(col.type, Float):
                bad.append(f"{tname}.{col.name}")
    assert not bad, f"存在浮点列（违反红线）：{bad}"


def test_models_unique_constraints_declared() -> None:
    """关键唯一约束在模型层已声明。"""
    from app.db.models import Base

    def unique_cols(table: str) -> set[frozenset[str]]:
        t = Base.metadata.tables[table]
        out: set[frozenset[str]] = set()
        for c in t.constraints:
            cols = getattr(c, "columns", None)
            if cols is not None and c.__class__.__name__ == "UniqueConstraint":
                out.add(frozenset(c.name for c in cols))
        return out

    assert frozenset({"province", "year", "subject_type", "score"}) in unique_cols("score_segments")
    assert frozenset({"year", "subject_type", "university_code", "group_code"}) in unique_cols(
        "admission_lines"
    )
    assert frozenset({"code"}) in unique_cols("universities")


def test_models_sensitive_columns_are_present() -> None:
    """脱敏列存在（绝不存明文分数）。"""
    from app.db.models import Base

    cols = {c.name for c in Base.metadata.tables["user_profiles"].columns}
    assert "score_hash" in cols and "score_bucket" in cols
    assert "rank_hash" in cols and "rank_bucket" in cols
    # 明文列名不得出现
    assert "score" not in cols, "user_profiles 不应有明文字段 score"
    assert "rank" not in cols, "user_profiles 不应有明文字段 rank"


# ==================== J1 行数一致（需库）====================


@requires_db
def test_j1_row_counts_match_csv() -> None:
    """J1：库内行数与清洗 CSV 完全一致。"""
    with engine.connect() as c:
        for table, name in (
            ("score_segments", "score_segments.csv"),
            ("universities", "universities.csv"),
            ("admission_lines", "admission_lines.csv"),
        ):
            db_count = c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            csv_count = len(_csv_rows(name))
            assert db_count == csv_count == EXPECTED[table], (
                f"{table}: 库 {db_count} vs CSV {csv_count} vs 期望 {EXPECTED[table]}"
            )


@requires_db
def test_j1_all_required_tables_exist() -> None:
    """J1：当前业务表全部建出。"""
    with engine.connect() as c:
        rows = c.execute(text("SHOW TABLES")).fetchall()
    tbls = {r[0] for r in rows}
    missing = REQUIRED_TABLES - tbls
    assert not missing, f"缺表：{missing}"


# ==================== J2 幂等（需库）====================


@requires_db
def test_j2_import_idempotent() -> None:
    """J2：重跑导入脚本，行数不发生漂移（且与期望一致）。"""
    before = _counts()
    r = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "import_data.py")],
        cwd=str(BACKEND_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"导入失败：{r.stderr[-500:]}"
    after = _counts()
    assert before == after, f"重跑后行数漂移：{before} -> {after}"
    assert after == EXPECTED


def _counts() -> dict[str, int]:
    with engine.connect() as c:
        return {
            t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
            for t in EXPECTED
        }


# ==================== J3 索引生效（需库）====================


@requires_db
def test_j3_score_segment_lookup_uses_index() -> None:
    """J3：一分一段按 (省,年,科类,分) 查询走索引，非全表扫描。"""
    sql = (
        "EXPLAIN SELECT `rank` FROM score_segments "
        "WHERE province=:p AND year=2026 AND subject_type=:s AND score=600"
    )
    with engine.connect() as c:
        row = c.execute(text(sql), {"p": "广东", "s": "物理"}).fetchone()
    plan = dict(row._mapping)
    # const/eq_ref/ref 均为索引访问；ALL = 全表扫描（不合格）
    assert plan["type"] in {"const", "eq_ref", "ref", "range"}, f"未走索引：{plan}"
    assert plan["key"] is not None, f"key 为空：{plan}"


@requires_db
def test_j3_admission_lookup_uses_index() -> None:
    """J3：投档线按 (年,科类,院校代码) 查询走索引。"""
    sql = (
        "EXPLAIN SELECT * FROM admission_lines "
        "WHERE year=2026 AND subject_type=:s AND university_code=:c"
    )
    with engine.connect() as c:
        row = c.execute(text(sql), {"s": "物理", "c": "10001"}).fetchone()
    plan = dict(row._mapping)
    assert plan["type"] in {"const", "eq_ref", "ref", "range"}, f"未走索引：{plan}"
    assert plan["key"] is not None, f"key 为空：{plan}"


# ==================== J4 迁移可回滚（需库，全程 subprocess）====================


@requires_db
@requires_destructive_db
@pytest.mark.destructive
def test_j4_downgrade_then_upgrade() -> None:
    """J4：downgrade base 后业务表清空，再 upgrade head 可重建，重导后行数恢复。

    ⚠️ 本用例会**重建整个库表**，因此放在最后执行；跑完自动恢复数据。
    """
    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    try:
        # 1) 回滚到底
        r1 = alembic("downgrade", "base")
        assert r1.returncode == 0, f"downgrade 失败：{r1.stderr[-500:]}"
        with engine.connect() as c:
            tbls = {row[0] for row in c.execute(text("SHOW TABLES")).fetchall()}
        assert tbls <= {"alembic_version"}, f"回滚后仍有业务表：{tbls}"

        # 2) 重新 upgrade
        r2 = alembic("upgrade", "head")
        assert r2.returncode == 0, f"upgrade 失败：{r2.stderr[-500:]}"
        with engine.connect() as c:
            tbls2 = {row[0] for row in c.execute(text("SHOW TABLES")).fetchall()}
        assert REQUIRED_TABLES <= tbls2, f"升级后缺表：{REQUIRED_TABLES - tbls2}"

        # 3) 重导数据并核对
        r3 = subprocess.run(
            [sys.executable, str(BACKEND_DIR / "scripts" / "import_data.py")],
            cwd=str(BACKEND_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r3.returncode == 0, f"重导失败：{r3.stderr[-500:]}"
        assert _counts() == EXPECTED
    finally:
        # 兜底：无论断言是否失败，确保库处于 head + 已导入状态
        alembic("upgrade", "head")
        subprocess.run(
            [sys.executable, str(BACKEND_DIR / "scripts" / "import_data.py")],
            cwd=str(BACKEND_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )


# ==================== 数据正确性抽查（需库）====================


@requires_db
def test_spotcheck_data_values() -> None:
    """库内抽样值与官方黄金样本一致。"""
    with engine.connect() as c:
        v = c.execute(
            text(
                "SELECT `rank` FROM score_segments "
                "WHERE province='广东' AND year=2026 AND subject_type='物理' AND score=600"
            )
        ).scalar_one()
        assert v == 30891, f"2026 物理 600 分位次应为 30891，实际 {v}"

        row = c.execute(
            text(
                "SELECT university_name, min_score, min_rank FROM admission_lines "
                "WHERE year=2024 AND subject_type='历史' AND university_code='10002' AND group_code=201"
            )
        ).fetchone()
        assert row is not None and row[0] == "中国人民大学"
        assert row[1] == 648 and row[2] == 150


@requires_db
def test_spotcheck_empty_admission_kept() -> None:
    """空投档行（官方「-」）保留 12 行，不补造。"""
    with engine.connect() as c:
        n = c.execute(
            text("SELECT COUNT(*) FROM admission_lines WHERE min_score IS NULL OR min_rank IS NULL")
        ).scalar_one()
    assert n == 12, f"空投档行应为 12，实际 {n}"
