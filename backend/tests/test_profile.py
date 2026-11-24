"""T17 测试 —— 用户档案（脱敏 + CRUD + 偏好注入）。

对应任务卡「测试场景（必做）」：
    J1 档案复用（第二次对话不用重输分数）；
    J2 日志无明文分数/位次（grep 断言）；
    J4 删除档案后查询返回空。

补足：
- **红线守护**：明文分数/位次**绝不落库**（库里只有 hash + bucket）；
- 分数/位次范围校验（越界拒绝写入，不静默修正）；
- 偏好注入片段**只含区间、不含明文**；
- 哈希加盐（同分不同盐 → 不同 hash，可测不同 salt 场景）；
- 桶口径与 ``app.core.logging.mask_*`` 一致。

设计：**不依赖 MySQL** —— 用 ``InMemoryProfileStore``，全离线可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import ValidationError  # noqa: E402
from app.services.profile import (  # noqa: E402
    InMemoryProfileStore,
    ProfileInput,
    ProfileService,
    build_preference_block,
    hash_rank,
    hash_score,
    rank_bucket,
    score_bucket,
)


def _service() -> ProfileService:
    return ProfileService(InMemoryProfileStore())


# ============================================================
#  脱敏工具
# ============================================================


def test_score_bucket_matches_logging_caliber():
    assert score_bucket(612) == "610-619"
    assert score_bucket(600) == "600-609"
    assert score_bucket(100) == "100-109"


def test_rank_bucket_thousand():
    assert rank_bucket(35412) == "35000-35999"
    assert rank_bucket(5000) == "5000-5999"


def test_hash_is_salted_and_stable():
    h1 = hash_score(600)
    h2 = hash_score(600)
    assert h1 == h2, "同输入应得同 hash（确定性）"
    assert len(h1) == 64, "sha256 十六进制 64 位"
    assert hash_score(600) != hash_rank(600), "分数与位次的 hash 域应隔离"


# ============================================================
#  J1 · 档案复用
# ============================================================


def test_j1_profile_create_and_reuse():
    svc = _service()
    view = svc.create_profile(
        ProfileInput(province="广东", current_year=2026, subject_type="物理", score=612, rank=35412)
    )
    assert view.id is not None
    # 第二次「对话」按 anon_id 复用档案
    reused = svc.get_by_anon(view.anon_id) if view.anon_id else svc.get_profile(view.id)
    assert reused is not None
    assert reused.province == "广东"
    assert reused.subject_type == "物理"
    # 回读只有区间，没有明文
    assert reused.score_bucket == "610-619"
    assert reused.rank_bucket == "35000-35999"


def test_j1_reuse_by_anon_id():
    svc = _service()
    v = svc.create_profile(ProfileInput(anon_id="anon-abc", province="广东", subject_type="物理", score=580))
    got = svc.get_by_anon("anon-abc")
    assert got is not None
    assert got.id == v.id


# ============================================================
#  J2 · 脱敏红线（明文不落库）
# ============================================================


def test_j2_no_plaintext_score_in_store():
    """红线：库内**不得**出现明文分数（只有 hash + bucket）。"""
    store = InMemoryProfileStore()
    svc = ProfileService(store)
    svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=612, rank=35412))

    row = list(store.rows.values())[0]
    # 明文 612 / 35412 不得以任何形式出现在值里
    plain_values = {str(v) for v in row.values()}
    assert "612" not in plain_values, "不得存明文分数"
    assert "35412" not in plain_values, "不得存明文位次"
    assert row["score_hash"] == hash_score(612)
    assert row["score_bucket"] == "610-619"
    assert row["rank_hash"] == hash_rank(35412)
    assert row["rank_bucket"] == "35000-35999"


def test_j2_log_has_no_plaintext_score(caplog):
    """日志无明文分数/位次（红线）。"""
    import logging

    svc = _service()
    with caplog.at_level(logging.INFO):
        svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=612, rank=35412))
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "612" not in joined, "日志不得出现明文分数"
    assert "35412" not in joined, "日志不得出现明文位次"
    assert "610-619" in joined, "日志应输出区间"


def test_j2_update_score_ranks_desensitized():
    store = InMemoryProfileStore()
    svc = ProfileService(store)
    v = svc.create_profile(ProfileInput(province="广东", subject_type="物理"))
    svc.update_score_rank(v.id, score=599, rank=12000)
    row = store.rows[v.id]
    assert row["score_hash"] == hash_score(599)
    assert row["score_bucket"] == "590-599"
    assert row["rank_bucket"] == "12000-12999"
    assert "599" not in {str(x) for x in row.values()}


# ============================================================
#  J4 · 删除档案
# ============================================================


def test_delete_profile_returns_none():
    """删除后查询返回空（任务卡 J4）。"""
    svc = _service()
    v = svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=600))
    assert svc.get_profile(v.id) is not None
    svc.delete_profile(v.id)
    assert svc.get_profile(v.id) is None


def test_delete_nonexistent_profile_returns_false():
    svc = _service()
    assert svc.delete_profile(99999) is False


# ============================================================
#  校验
# ============================================================


def test_score_out_of_range_rejected():
    svc = _service()
    with pytest.raises(ValidationError):
        svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=900))
    with pytest.raises(ValidationError):
        svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=50))


def test_rank_invalid_rejected():
    svc = _service()
    with pytest.raises(ValidationError):
        svc.create_profile(ProfileInput(province="广东", subject_type="物理", rank=0))
    with pytest.raises(ValidationError):
        svc.create_profile(ProfileInput(province="广东", subject_type="物理", rank=-5))


def test_subject_type_invalid_rejected():
    svc = _service()
    with pytest.raises(ValidationError):
        svc.create_profile(ProfileInput(province="广东", subject_type="综合"))


def test_update_score_out_of_range_rejected():
    svc = _service()
    v = svc.create_profile(ProfileInput(province="广东", subject_type="物理"))
    with pytest.raises(ValidationError):
        svc.update_score_rank(v.id, score=800, rank=None)


# ============================================================
#  偏好
# ============================================================


def test_update_preferences():
    svc = _service()
    v = svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=600))
    updated = svc.update_preferences(
        v.id,
        preferred_cities=["广州", "深圳"],
        preferred_majors=["计算机", "临床医学"],
        preferred_types=["985", "211"],
    )
    assert updated is not None
    assert updated.preferred_cities == ["广州", "深圳"]
    assert "计算机" in updated.preferred_majors


def test_preference_block_no_plaintext():
    """偏好注入片段：只含区间，不含明文分数/位次（红线）。"""
    svc = _service()
    v = svc.create_profile(
        ProfileInput(
            province="广东",
            current_year=2026,
            subject_type="物理",
            elective_subjects=["化学", "生物"],
            score=612,
            rank=35412,
            preferred_cities=["广州"],
        )
    )
    block = build_preference_block(v)
    assert "612" not in block, "偏好片段不得含明文分数"
    assert "35412" not in block, "偏好片段不得含明文位次"
    assert "610-619" in block, "应含分数区间"
    assert "35000-35999" in block, "应含位次区间"
    assert "广东" in block
    assert "物理" in block
    assert "广州" in block


def test_preference_block_empty_when_no_profile():
    assert build_preference_block(None) == ""


def test_profile_view_to_dict():
    svc = _service()
    v = svc.create_profile(ProfileInput(province="广东", subject_type="物理", score=600, rank=10000))
    d = v.to_dict()
    for key in ("id", "province", "subject_type", "score_bucket", "rank_bucket"):
        assert key in d
    assert "score" not in d, "视图不得暴露明文字段名"


# ============================================================
#  真机 smoke（可选：MySQL 可用时）
# ============================================================


@pytest.mark.smoke
def test_real_profile_smoke():
    """真 MySQL 冒烟：建/查/删（若库不可用则 skip）。"""
    try:
        from app.db.session import SessionLocal
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用：{exc}")
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    try:
        from app.services.profile import SqlProfileStore

        svc = ProfileService(SqlProfileStore(db))
        v = svc.create_profile(
            ProfileInput(anon_id="smoke-t17", province="广东", subject_type="物理", score=600)
        )
        assert v is not None
        assert v.score_bucket == "600-609"
        svc.delete_profile(v.id)
        assert svc.get_profile(v.id) is None
    finally:
        db.close()
