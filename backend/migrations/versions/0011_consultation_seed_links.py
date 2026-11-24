"""为默认咨询消息补充官方参考链接。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE consultation_messages SET url = :url "
            "WHERE url = '' AND content LIKE :pattern"
        ).bindparams(url="https://gaokao.chsi.com.cn/", pattern="2027 高考志愿规划已开启%")
    )
    op.execute(
        sa.text(
            "UPDATE consultation_messages SET url = :url "
            "WHERE url = '' AND content LIKE :pattern"
        ).bindparams(url="https://www.moe.gov.cn/", pattern="志愿填报前请重点核对%")
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE consultation_messages SET url = '' "
            "WHERE url IN (:a, :b)"
        ).bindparams(a="https://gaokao.chsi.com.cn/", b="https://www.moe.gov.cn/")
    )
