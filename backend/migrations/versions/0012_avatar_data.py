"""用户账号增加头像数据。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_accounts", sa.Column("avatar_data", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_accounts", "avatar_data")
