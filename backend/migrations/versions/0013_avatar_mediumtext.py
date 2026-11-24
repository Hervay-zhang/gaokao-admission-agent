"""头像数据改为 MEDIUMTEXT。

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_accounts",
        "avatar_data",
        existing_type=sa.Text(),
        type_=MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "user_accounts",
        "avatar_data",
        existing_type=MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
