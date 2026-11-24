"""账号归属、报告历史与社区审核扩展。

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 登录账号可编辑资料
    op.add_column("user_accounts", sa.Column("nickname", sa.String(64), nullable=True))
    op.add_column("user_accounts", sa.Column("grade", sa.String(16), nullable=True))
    op.execute("UPDATE user_accounts SET nickname = display_name WHERE nickname IS NULL")

    # 会话与档案归属登录账号
    op.add_column("sessions", sa.Column("owner_username", sa.String(64), nullable=True))
    op.create_index("ix_sessions_owner_username", "sessions", ["owner_username"])
    op.add_column("user_profiles", sa.Column("owner_username", sa.String(64), nullable=True))
    op.create_index("ix_user_profiles_owner_username", "user_profiles", ["owner_username"])

    # 院校招生官网维护
    op.add_column("universities", sa.Column("admission_url", sa.String(512), nullable=True))
    op.add_column(
        "universities",
        sa.Column("admission_url_status", sa.String(16), nullable=False, server_default="待核"),
    )
    op.add_column("universities", sa.Column("admission_url_updated_at", sa.DateTime(), nullable=True))

    # 志愿表归属
    op.add_column("volunteer_plans", sa.Column("owner_username", sa.String(64), nullable=True))
    op.create_index("ix_volunteer_plans_owner_username", "volunteer_plans", ["owner_username"])

    # 报告历史
    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("owner_username", sa.String(64), nullable=False),
        sa.Column(
            "profile_id",
            sa.BigInteger(),
            sa.ForeignKey("user_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(128), nullable=False, server_default="冲稳保路径报告"),
        sa.Column("province", sa.String(32), nullable=False, server_default=""),
        sa.Column("subject_type", sa.String(16), nullable=False, server_default=""),
        sa.Column("score_bucket", sa.String(16), nullable=True),
        sa.Column("rank_bucket", sa.String(24), nullable=True),
        sa.Column("current_year", sa.SmallInteger(), nullable=False),
        sa.Column("tier_counts", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_report_owner_created",
        "report_snapshots",
        ["owner_username", "created_at"],
    )
    op.create_index(
        "ix_report_snapshots_owner_username",
        "report_snapshots",
        ["owner_username"],
    )

    # 评论点赞
    op.create_table(
        "forum_comment_likes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "comment_id",
            sa.BigInteger(),
            sa.ForeignKey("forum_comments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_sid", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("comment_id", "user_sid", name="uq_forum_comment_like_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_forum_comment_likes_comment_id",
        "forum_comment_likes",
        ["comment_id"],
    )

    # 社区举报与审核记录
    op.create_table(
        "community_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("reporter_sid", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("resolver", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_community_report_status",
        "community_reports",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_community_report_target",
        "community_reports",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_community_report_target", table_name="community_reports")
    op.drop_index("ix_community_report_status", table_name="community_reports")
    op.drop_table("community_reports")

    op.drop_index("ix_forum_comment_likes_comment_id", table_name="forum_comment_likes")
    op.drop_table("forum_comment_likes")

    op.drop_index("ix_report_snapshots_owner_username", table_name="report_snapshots")
    op.drop_index("ix_report_owner_created", table_name="report_snapshots")
    op.drop_table("report_snapshots")

    op.drop_index("ix_volunteer_plans_owner_username", table_name="volunteer_plans")
    op.drop_column("volunteer_plans", "owner_username")

    op.drop_column("universities", "admission_url_updated_at")
    op.drop_column("universities", "admission_url_status")
    op.drop_column("universities", "admission_url")

    op.drop_index("ix_user_profiles_owner_username", table_name="user_profiles")
    op.drop_column("user_profiles", "owner_username")
    op.drop_index("ix_sessions_owner_username", table_name="sessions")
    op.drop_column("sessions", "owner_username")

    op.drop_column("user_accounts", "grade")
    op.drop_column("user_accounts", "nickname")
