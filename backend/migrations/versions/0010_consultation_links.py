"""咨询浮窗消息增加跳转链接。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consultation_messages",
        sa.Column("url", sa.String(512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("consultation_messages", "url")
