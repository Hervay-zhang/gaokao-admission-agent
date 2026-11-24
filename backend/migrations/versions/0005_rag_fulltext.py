"""T30 · 补 RAG 检索结构：policy_chunks.content FULLTEXT(ngram) 索引。

背景（T30 E2E 发现的真实缺口）：0001 建表时未含该索引，T13 起靠 ``build_rag_index.py``
在**本地库手工**建过——容器/全新环境 ``alembic upgrade head`` 后**没有**该索引，
任何走 RAG 的问答直接 ``Can't find FULLTEXT index matching the column list``。
红线「一键拉起不需要手工建表/建索引」要求索引随迁移落地。

幂等：MySQL 不支持 ``ADD FULLTEXT INDEX IF NOT EXISTS``，先查 information_schema 再建；
已有索引（本地库由脚本建过）则跳过。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

INDEX_NAME = "ft_chunk_content"
TABLE = "policy_chunks"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.exec_driver_sql(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "AND INDEX_NAME = %s",
        (TABLE, INDEX_NAME),
    ).fetchall()
    if rows:
        return  # 已存在（如本地库由 build_rag_index.py 建过）——幂等跳过
    conn.exec_driver_sql(
        f"ALTER TABLE {TABLE} ADD FULLTEXT INDEX {INDEX_NAME} (content) WITH PARSER ngram"
    )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.exec_driver_sql(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "AND INDEX_NAME = %s",
        (TABLE, INDEX_NAME),
    ).fetchall()
    if not rows:
        return
    conn.exec_driver_sql(f"ALTER TABLE {TABLE} DROP INDEX {INDEX_NAME}")
