"""FastAPI 应用入口。

职责：
- 启动时校验配置、初始化日志
- 注册异常处理器、中间件（request_id）
- 挂载路由（T01 只有 health；后续卡逐步增加）
- 托管前端静态文件（可选，开发期前端单独起 http.server）
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin as admin_router
from app.api import auth as auth_router
from app.api import cases as cases_router
from app.api import chat as chat_router
from app.api import community as community_router
from app.api import consultations as consultations_router
from app.api import expert as expert_router
from app.api import feedback as feedback_router
from app.api import health as health_router
from app.api import plan as plan_router
from app.api import profile as profile_router
from app.api import quicklinks as quicklinks_router
from app.api import recommend as recommend_router
from app.api import report as report_router
from app.api import schools as schools_router
from app.api import share as share_router
from app.api import stats as stats_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动校验 + 优雅关闭。"""
    # ---- 启动 ----
    problems = settings.validate_critical()
    if problems:
        for p in problems:
            logger.warning("配置检查：%s", p)
        if settings.is_production:
            raise RuntimeError("生产环境配置校验失败：" + "；".join(problems))
    else:
        logger.info("配置检查通过（province=%s, year=%s, mock=%s）", settings.province, settings.current_year, settings.mock_mode)

    chain = " → ".join(settings.llm_model_chain)
    logger.info("LLM 模型降级链：%s", chain)
    logger.info("%s v%s 已启动", settings.app_name, settings.app_version)

    yield

    # ---- 关闭 ----
    from app.core.redis import close_redis

    await close_redis()
    logger.info("应用已关闭")


app = FastAPI(
    title="高考升学志愿填报 AI 助手",
    version=settings.app_version,
    description="基于近三年真实投档数据的志愿推荐 + 政策问答（RAG，带引用）",
    lifespan=lifespan,
)

# ---- 异常处理 ----
register_exception_handlers(app)

# ---- CORS（开发期前端 8080 访问后端 8000）----
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """为每个请求注入 request_id，便于日志追溯（且不泄漏敏感信息）。"""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ---- 路由 ----
app.include_router(health_router.router)
app.include_router(auth_router.router)
app.include_router(recommend_router.router)
app.include_router(chat_router.router)
app.include_router(feedback_router.router)
app.include_router(profile_router.router)
app.include_router(schools_router.router)
app.include_router(report_router.router)
app.include_router(plan_router.router)
app.include_router(cases_router.router)
app.include_router(share_router.router)
app.include_router(community_router.router)
app.include_router(consultations_router.router)
app.include_router(expert_router.router)
app.include_router(quicklinks_router.router)
app.include_router(stats_router.router)
app.include_router(admin_router.router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }
