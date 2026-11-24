"""服务端登录账号（考生 / 家长 / 管理员）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

from __future__ import annotations

import hashlib
import secrets

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def _password_hash(password: str) -> str:
    iterations = 120_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="student"),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("avatar", sa.String(8), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_user_account_username"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_account_role", "user_accounts", ["role", "is_active"])

    accounts = (
        ("student", "student123", "student", "李同学", "李"),
        ("parent", "parent123", "parent", "考生家长", "家"),
        ("admin", "admin123", "admin", "平台管理员", "管"),
    )
    for username, password, role, name, avatar in accounts:
        op.execute(
            sa.text(
                "INSERT INTO user_accounts "
                "(username, password_hash, role, display_name, avatar, is_active) "
                "VALUES (:username, :password_hash, :role, :name, :avatar, 1)"
            ).bindparams(
                username=username,
                password_hash=_password_hash(password),
                role=role,
                name=name,
                avatar=avatar,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_user_account_role", table_name="user_accounts")
    op.drop_table("user_accounts")
