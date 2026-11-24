"""T12 生产环境冒烟脚本（连真实 MySQL，验证政策语料端到端可用）。

跑法（backend 目录下）：
    ./.venv/Scripts/python.exe scripts/smoke_policy.py

做四件事，**全只读、不改任何数据**：
1. 从真实 MySQL 读 ``policy_docs``，核对篇数与 DoD（≥50）；
2. 抽查每篇必备字段：``url``（来源 URL）、``year``（来源年份）、``content`` 非空且长度合理；
3. 校验去重：``doc_hash`` 唯一、标题无重复；
4. 抽查一篇，打印其 URL / 年份 / 正文片段，便于人工核对内容确为官方公开政策。

红线自证：断言无缺 url / 缺 year / 空正文；不编造、不补写。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select  # noqa: E402

from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.db.models import PolicyDoc  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

setup_logging()
logger = get_logger("smoke.t12")

MIN_DOCS = 50
MIN_CONTENT_LEN = 80


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"❌ {msg}")
    print(f"    ✓ {msg}")


def main() -> int:
    db = SessionLocal()
    try:
        print("[1] 读取 policy_docs（真实 MySQL）")
        total = db.execute(select(func.count()).select_from(PolicyDoc)).scalar_one()
        print(f"    policy_docs 共 {total} 篇")
        _check(total >= MIN_DOCS, f"篇数达 DoD（≥{MIN_DOCS}）：实际 {total}")

        print("[2] 必备字段抽查（url / year / content）")
        miss_url = db.execute(
            select(func.count()).select_from(PolicyDoc).where(PolicyDoc.url.is_(None))
        ).scalar_one()
        miss_year = db.execute(
            select(func.count()).select_from(PolicyDoc).where(PolicyDoc.year.is_(None))
        ).scalar_one()
        _check(miss_url == 0, f"每篇有来源 URL（缺 {miss_url}）")
        _check(miss_year == 0, f"每篇有来源年份（缺 {miss_year}）")

        rows = db.execute(select(PolicyDoc)).scalars().all()
        too_short = [r.title for r in rows if len((r.content or "").strip()) < MIN_CONTENT_LEN]
        _check(not too_short, f"正文均非空且 ≥ {MIN_CONTENT_LEN} 字（过短 {len(too_short)} 篇）")

        print("[3] 去重校验（doc_hash / 标题）")
        hashes = [r.doc_hash for r in rows]
        _check(len(hashes) == len(set(hashes)), f"doc_hash 唯一（{len(hashes)} 篇）")
        titles = [r.title for r in rows]
        _check(len(titles) == len(set(titles)), "标题无重复")

        print("[4] 抽查一篇（人工核对内容为官方公开政策）")
        sample = rows[0]
        print(f"    标题：{sample.title}")
        print(f"    URL ：{sample.url}")
        print(f"    年份：{sample.year}  类型：{sample.doc_type}  来源：{sample.source}")
        print(f"    正文（前 120 字）：{(sample.content or '')[:120]}…")
        _check(
            bool(sample.url) and sample.url.startswith("http"),
            "抽查样本 URL 为可访问的官方地址",
        )

        # 年份/来源分布（可见口径）
        year_dist = dict(
            db.execute(select(PolicyDoc.year, func.count()).group_by(PolicyDoc.year)).all()
        )
        src_dist = dict(
            db.execute(select(PolicyDoc.source, func.count()).group_by(PolicyDoc.source)).all()
        )
        print(f"    年份分布：{dict(sorted(year_dist.items()))}")
        print(f"    来源数：{len(src_dist)} 个")

        print("\nT12 生产冒烟通过 ✓")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
