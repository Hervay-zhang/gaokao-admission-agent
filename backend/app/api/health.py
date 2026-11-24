"""健康检查路由。

- `GET /health`            → 进程存活
- `GET /api/health/llm`    → 模型链与各模型可用状态
- `GET /api/health/db`     → 数据库连通性（SELECT 1）
- `GET /api/health/redis`  → Redis 连通性（PING）
- `GET /api/health/ratelimit` → 限流阈值与当前连接占用（T20）
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.config import env_snapshot, settings
from app.core.llm import get_llm_client
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", summary="存活探针")
async def health() -> dict[str, object]:
    """返回进程存活与配置快照（脱敏）。"""
    return {"status": "ok", "config": env_snapshot()}


@router.get("/api/health/llm", summary="LLM 模型链状态")
async def health_llm() -> dict[str, object]:
    """报告模型降级链与各模型可用状态。"""
    client = get_llm_client()
    result = await client.health()
    return {"status": "ok", "chain": settings.llm_model_chain, **result}


@router.get("/api/health/db", summary="数据库连通性")
async def health_db() -> dict[str, object]:
    """执行 SELECT 1 验证数据库可用。"""
    from sqlalchemy import text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据库连通性检查失败：%r", exc)
        return {"status": "error", "database": "unavailable", "reason": type(exc).__name__}


@router.get("/api/health/redis", summary="Redis 连通性")
async def health_redis() -> dict[str, object]:
    """执行 PING 验证 Redis 可用。"""
    from app.core.redis import get_redis

    try:
        client = get_redis()
        pong = await client.ping()
        return {"status": "ok", "redis": "PONG" if pong else "unexpected"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 连通性检查失败：%r", exc)
        return {"status": "error", "redis": "unavailable", "reason": type(exc).__name__}


@router.get("/api/health/ratelimit", summary="限流阈值与当前占用")
async def health_ratelimit(request: Request) -> dict[str, object]:
    """暴露限流阈值与当前连接占用（供管理后台核对，**不含任何敏感信息**）。"""
    from app.core.ratelimit import client_ip, get_rate_limiter

    limiter = get_rate_limiter()
    ip = client_ip(request)
    s = limiter.s  # 汇报**生效中**的阈值（来自限流器自身配置，测试可替换）
    return {
        "status": "ok",
        "thresholds": {
            "ip_per_min": s.rate_limit_ip_per_min,
            "ip_per_day": s.rate_limit_ip_per_day,
            "session_per_min": s.rate_limit_session_per_min,
            "session_per_day": s.rate_limit_session_per_day,
            "sse_concurrent": s.rate_limit_sse_concurrent,
            "sse_max_seconds": s.rate_limit_sse_max_seconds,
            "retry_after": s.rate_limit_retry_after,
        },
        "caller": {"ip": ip, "whitelisted": limiter.is_whitelisted(ip=ip)},
        "current_connections": await limiter.current_connections(ip=ip),
    }
