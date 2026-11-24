"""T22 · feedback 表扩展：维度/原因/快照/模型 + 一条输出仅一次反馈唯一约束。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

# 新增列（0001 的 feedback 表只有 message_id/session_sid/kind/content/created_at）
_NEW_COLUMNS = [
    ("dimension", sa.String(length=32), "反馈维度：answer / recommend"),
    ("rating", sa.SmallInteger(), "评分：up=1 / down=-1"),
    ("reason", sa.String(length=64), "点踩原因"),
    ("prompt_snapshot", sa.Text(), "提问快照（脱敏）"),
    ("response_snapshot", sa.Text(), "回复快照（脱敏）"),
    ("model", sa.String(length=64), "生成该回复的模型"),
]


def upgrade() -> None:
    for name, type_, comment in _NEW_COLUMNS:
        op.add_column("feedback", sa.Column(name, type_, nullable=True, comment=comment))
    # 一条输出仅可反馈一次（唯一约束兜底；message_id 可空，多个 NULL 不冲突）
    op.create_unique_constraint("uq_feedback_message", "feedback", ["message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_feedback_message", "feedback", type_="unique")
    for name, _, _ in reversed(_NEW_COLUMNS):
        op.drop_column("feedback", name)
