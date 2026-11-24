"""专家服务预约记录。

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expert_appointments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("plan_tier", sa.String(32), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("contact_name", sa.String(64), nullable=False),
        sa.Column("contact_info", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_expert_appointment_status",
        "expert_appointments",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_expert_appointment_status", table_name="expert_appointments")
    op.drop_table("expert_appointments")
