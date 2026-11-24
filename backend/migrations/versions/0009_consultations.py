"""首页滚动咨询浮窗消息。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consultation_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("content", sa.String(160), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_consultation_active_sort",
        "consultation_messages",
        ["is_active", "sort_order"],
    )
    for index, content in enumerate(
        (
            "2027 高考志愿规划已开启，建议提前完成分数位次与选科画像。",
            "院校专业组数据持续更新中，请以省教育考试院当年正式公告为准。",
            "志愿填报前请重点核对招生章程、选科要求和身体条件限制。",
        )
    ):
        op.execute(
            sa.text(
                "INSERT INTO consultation_messages (content, is_active, sort_order) "
                "VALUES (:content, 1, :sort_order)"
            ).bindparams(content=content, sort_order=index)
        )


def downgrade() -> None:
    op.drop_index("ix_consultation_active_sort", table_name="consultation_messages")
    op.drop_table("consultation_messages")
