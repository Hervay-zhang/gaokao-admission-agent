"""T28 · 志愿表分享字段：只读分享过期时间。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 分享快照以 **MySQL 为唯一事实源**（Redis 在本项目为非硬依赖，分享链接必须跨重启可用）
    op.add_column(
        "volunteer_plans",
        sa.Column("share_expires_at", sa.DateTime(), nullable=True, comment="分享链接过期时间（只读分享，过期失效）"),
    )
    op.create_index("ix_plan_share_token", "volunteer_plans", ["share_token"])


def downgrade() -> None:
    op.drop_index("ix_plan_share_token", table_name="volunteer_plans")
    op.drop_column("volunteer_plans", "share_expires_at")
