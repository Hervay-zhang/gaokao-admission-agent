"""Alembic 迁移环境（T04）。

- **URL 从 `.env` 读取**（`app.core.config.settings.database_url`），`alembic.ini` 中不写账密；
- `target_metadata = Base.metadata`，支持 `alembic revision --autogenerate`；
- `compare_type=True` 让 autogenerate 感知列类型变化；
- **禁用 FLOAT**：模型层已用 Integer / Numeric，autogenerate 不会引入浮点。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 `app` 可被导入（backend 根目录）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402

# Alembic Config 对象
config = context.config

# 把 .env 中的 URL 注入（覆盖 alembic.ini 的空值）
config.set_main_option("sqlalchemy.url", settings.database_url)

# 日志配置（存在时生效）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标元数据（autogenerate 依据）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # utf8mb4 透传
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
