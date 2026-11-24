"""数据库会话与引擎（MySQL + SQLAlchemy 2.0）。

T01 只建立引擎与 Session 工厂；表结构在 T04 定义并迁移。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# MySQL 连接池配置
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # 连接前探活，避免 MySQL 8h 断连
    pool_recycle=3600,    # 1 小时回收
    pool_size=10,
    max_overflow=20,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入：每请求一个会话，结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
