"""T09 生产环境冒烟脚本（连真实 MySQL，验证数据版本服务的端到端可用性）。

跑法（backend 目录下）：
    ./.venv/Scripts/python.exe scripts/smoke_data_version.py

做四件事，**只读 + 一次可复原的写入**：
1. 读当前生效配置（`data_version` 表 → .env 兜底）并打印口径文字；
2. 真实数据里挑一个「三年都有投档数据」的专业组，跑「近年位次趋势」；
3. 用推荐引擎按当前配置跑一次「冲稳保」，打印各档数量与口径；
4. 切换窗口 → 再跑一次推荐 → 对比差异 → **回滚复原**（并核对库内唯一有效行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select  # noqa: E402

from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.db.models import AdmissionLine, DataVersion  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.data_version import (  # noqa: E402
    DataVersionService,
    SqlDataVersionRepository,
)
from app.services.recommend import get_recommend_engine  # noqa: E402
from app.services.score import get_score_engine  # noqa: E402

setup_logging()
logger = get_logger("smoke.t09")

PROV = "广东"
SUBJECT = "物理"


def _active_row_count() -> int:
    with SessionLocal() as db:
        return int(db.execute(
            select(func.count()).select_from(DataVersion).where(DataVersion.is_active.is_(True))
        ).scalar_one())


def _pick_group_with_three_years() -> tuple[str, int, str] | None:
    """挑一个在 2024/2025/2026 三年都有投档位次的专业组。"""
    with SessionLocal() as db:
        stmt = (
            select(
                AdmissionLine.university_code,
                AdmissionLine.group_code,
                func.count(func.distinct(AdmissionLine.year)).label("n"),
            )
            .where(
                AdmissionLine.province == PROV,
                AdmissionLine.subject_type == SUBJECT,
                AdmissionLine.year.in_([2024, 2025, 2026]),
                AdmissionLine.min_rank.is_not(None),
            )
            .group_by(AdmissionLine.university_code, AdmissionLine.group_code)
            .having(func.count(func.distinct(AdmissionLine.year)) == 3)
            .order_by(AdmissionLine.university_code)
            .limit(1)
        )
        row = db.execute(stmt).first()
        if row is None:
            return None
        return str(row[0]), int(row[1]), SUBJECT


def main() -> int:
    print("=" * 72)
    print("T09 生产冒烟：数据时效与年份切换/对比")
    print("=" * 72)

    repo = SqlDataVersionRepository()
    svc = DataVersionService(repo)

    # ---------- 1. 读取生效配置 ----------
    cfg = svc.get_active()
    print(f"\n[1] 生效配置（source={cfg.source}）")
    print(f"    current_year = {cfg.current_year}")
    print(f"    ref_years    = {list(cfg.ref_years)}")
    print(f"    year_weights = {list(cfg.year_weights)}")
    print(f"    fingerprint  = {cfg.fingerprint}")
    print(f"    口径文字     = {cfg.caliber_text()}")

    # ---------- 2. 趋势对比 ----------
    print("\n[2] 近年位次趋势（真实数据）")
    picked = _pick_group_with_three_years()
    if picked is None:
        print("    !! 库内没有「三年全有投档位次」的专业组，无法演示趋势 —— 属数据现状，非代码问题")
    else:
        code, group, subject = picked
        tr = svc.trend(university_code=code, group_code=group, subject_type=subject)
        print(f"    {tr.university_name}（{code} / 专业组 {group}）")
        for p in tr.points:
            rank_txt = "-" if p.min_rank is None else f"{p.min_rank:>7}"
            delta_txt = "" if p.rank_delta is None else f"  Δ{p.rank_delta:+d}"
            score_txt = "-" if p.min_score is None else p.min_score
            print(f"      {p.year}  分数 {score_txt:>4}  位次 {rank_txt}  {p.direction:>11}{delta_txt}")
        print(f"    整体方向：{tr.direction_text}")
        print(f"    摘要：{tr.summary}")
        if tr.note:
            print(f"    标注：{tr.note}")

    # ---------- 3. 当前配置下的推荐 ----------
    print("\n[3] 当前配置下的推荐（物理 600 分）")
    score_engine = get_score_engine()
    rec_engine = get_recommend_engine()

    rank_res = score_engine.query_rank(cfg.current_year, PROV, SUBJECT, 600)
    print(f"    位次换算：{cfg.current_year} 年 {PROV} {SUBJECT} 600 分 → 位次 {rank_res.rank}")

    base = rec_engine.recommend(
        score=600, subject_type=SUBJECT, province=PROV, rank=rank_res.rank,
        current_year=cfg.current_year, ref_years=list(cfg.ref_years),
    )
    for g in base.tiers:
        names = "、".join(it.university_name for it in g.items[:3]) or "（无）"
        print(f"    {g.label}档 {g.count:>2} 所：{names}")
    print(f"    口径：{base.caliber_text}")

    # ---------- 4. 切换 → 对比 → 回滚 ----------
    print("\n[4] 切换窗口 → 对比 → 回滚")
    if len(cfg.ref_years) < 2:
        print("    当前窗口仅 1 年，跳过切换对比")
    else:
        newest = cfg.ref_years[-1]
        print(f"    切到「仅 {newest} 年」（权重 1.0）...")
        svc.switch(ref_years=[newest], year_weights=[1.0], actor="smoke", note="T09 冒烟")
        cfg2 = svc.get_active()
        print(f"    切换后：ref_years={list(cfg2.ref_years)}，口径={cfg2.caliber_text()}")

        after = rec_engine.recommend(
            score=600, subject_type=SUBJECT, province=PROV, rank=rank_res.rank,
            current_year=cfg2.current_year, ref_years=list(cfg2.ref_years),
        )
        for g_before, g_after in zip(base.tiers, after.tiers, strict=False):
            names_after = "、".join(it.university_name for it in g_after.items[:3]) or "（无）"
            flag = "出现变化" if g_before.count != g_after.count else "数量相同"
            print(f"      {g_after.label}档 {g_before.count} → {g_after.count} 所（{flag}）：{names_after}")
        print(f"    切换后口径：{after.caliber_text}")
        assert base.caliber_text != after.caliber_text, "切换后口径文字未变化 —— 口径漂移风险"
        print("    ✓ 口径文字随配置同步变化")

        print("    回滚...")
        restored = svc.rollback(actor="smoke")
        print(f"    回滚后：ref_years={list(restored.ref_years)}，口径={svc.caliber_text()}")
        assert restored.ref_years == cfg.ref_years, "回滚未恢复原窗口"
        print("    ✓ 回滚成功，配置已复原")

    # ---------- 5. 唯一有效行 ----------
    n_active = _active_row_count()
    print(f"\n[5] data_version 唯一有效行数 = {n_active}")
    assert n_active == 1, f"唯一有效行约束被破坏：{n_active} 行 is_active=1"

    print("\n" + "=" * 72)
    print("T09 生产冒烟通过 ✓")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
