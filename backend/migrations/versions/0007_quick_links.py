"""页面底部快捷链接表（quick_links）+ 默认官方平台入口。

背景（参考站对照）：参考站「见径」页面底部有可设置的快捷链接区，
本项目 footer 只有免责条。新增 quick_links 表，管理后台可增删改，
公开接口供前端 footer 渲染；默认写入官方高考平台入口（阳光高考/各地考试院/学信网等）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

# 默认快捷链接（官方权威入口；如需调整可在管理后台修改）
DEFAULT_LINKS = [
    {"label": "阳光高考（教育部）", "url": "https://gaokao.chsi.com.cn/", "icon": "官", "group": "gaokao", "sort": 1},
    {"label": "学信网", "url": "https://www.chsi.com.cn/", "icon": "学", "group": "gaokao", "sort": 2},
    {"label": "广东省教育考试院", "url": "https://eea.gd.gov.cn/", "icon": "粤", "group": "gaokao", "sort": 3},
    {"label": "教育部官网", "url": "https://www.moe.gov.cn/", "icon": "教", "group": "official", "sort": 4},
    {"label": "中国教育考试网", "url": "https://www.neea.edu.cn/", "icon": "考", "group": "official", "sort": 5},
]


def upgrade() -> None:
    op.create_table(
        "quick_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("icon", sa.String(16), nullable=False, server_default=""),
        sa.Column("group", sa.String(32), nullable=False, server_default="gaokao"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_quick_link_sort", "quick_links", ["is_active", "sort_order"])

    # 默认官方平台入口（一次性写入；管理后台可继续增删改）
    for i, link in enumerate(DEFAULT_LINKS):
        op.execute(
            sa.text(
                "INSERT INTO quick_links (label, url, icon, `group`, sort_order, is_active) "
                "VALUES (:label, :url, :icon, :grp, :sort, 1)"
            ).bindparams(
                label=link["label"],
                url=link["url"],
                icon=link["icon"],
                grp=link["group"],
                sort=link["sort"],
            )
        )


def downgrade() -> None:
    op.drop_index("ix_quick_link_sort", table_name="quick_links")
    op.drop_table("quick_links")