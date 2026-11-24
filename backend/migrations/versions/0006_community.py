"""社区/论坛模块：帖子、评论、点赞、收藏四表。

匿名社区，以 session_sid 为用户标识；软删 + 计数缓存。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- forum_posts ----
    op.create_table(
        "forum_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False, server_default="general"),
        sa.Column("author_sid", sa.String(64), nullable=False, index=True),
        sa.Column("author_nickname", sa.String(32), nullable=False, server_default="匿名同学"),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_removed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_forum_post_category", "forum_posts", ["category", "created_at"])
    op.create_index("ix_forum_post_author", "forum_posts", ["author_sid", "created_at"])

    # ---- forum_comments ----
    op.create_table(
        "forum_comments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_sid", sa.String(64), nullable=False, index=True),
        sa.Column("author_nickname", sa.String(32), nullable=False, server_default="匿名同学"),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reply_to_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_nickname", sa.String(32), nullable=True),
        sa.Column("is_removed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["post_id"], ["forum_posts.id"]),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_forum_comment_post", "forum_comments", ["post_id", "created_at"])
    op.create_index("ix_forum_comment_author", "forum_comments", ["author_sid", "created_at"])

    # ---- forum_post_likes ----
    op.create_table(
        "forum_post_likes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("user_sid", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["post_id"], ["forum_posts.id"]),
        sa.UniqueConstraint("post_id", "user_sid", name="uq_forum_post_like_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ---- forum_post_favorites ----
    op.create_table(
        "forum_post_favorites",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("user_sid", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["post_id"], ["forum_posts.id"]),
        sa.UniqueConstraint("post_id", "user_sid", name="uq_forum_post_fav_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("forum_post_favorites")
    op.drop_table("forum_post_likes")
    op.drop_table("forum_comments")
    op.drop_table("forum_posts")
