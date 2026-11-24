"""T26 · 志愿表扩展：乐观锁版本 + 系统档位只读 + 用户调整标记。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # volunteer_plans：乐观锁版本（每次变更 +1，防并发覆盖）
    op.add_column(
        "volunteer_plans",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0", comment="乐观锁版本（每次变更 +1）"),
    )
    # volunteer_items：系统档位只读 + 用户调整标记
    op.add_column(
        "volunteer_items",
        sa.Column("system_tier", sa.String(length=8), nullable=True, comment="系统推荐档位（写入后只读，用户调整不得覆盖）"),
    )
    op.add_column(
        "volunteer_items",
        sa.Column(
            "user_adjusted", sa.Boolean(), nullable=False, server_default="0", comment="用户是否调整过档位"
        ),
    )


def downgrade() -> None:
    op.drop_column("volunteer_items", "user_adjusted")
    op.drop_column("volunteer_items", "system_tier")
    op.drop_column("volunteer_plans", "version")
