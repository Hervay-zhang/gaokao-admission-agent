"""统一业务异常体系 + 全局异常处理器。

约定：
- 所有可预期的业务错误继承 `AppError`，携带 `code`（机器可读）与 `message`（人可读中文）。
- 未捕获异常由全局处理器兜底，**不向前端泄漏堆栈**，只返回统一结构 + request_id。
- LLM 相关错误单独成类，上层据此切换到「专家规则模式」。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """业务异常基类。"""

    code: str = "APP_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, detail: Any = None) -> None:
        self.message = message or self.__class__.__doc__ or "请求处理失败"
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self, request_id: str = "") -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "request_id": request_id,
        }


# ==================== 通用业务异常 ====================


class ConfigError(AppError):
    """配置错误（缺 .env、字段非法等）。"""

    code = "CONFIG_ERROR"
    http_status = 500


class ValidationError(AppError):
    """入参校验失败。"""

    code = "VALIDATION_ERROR"
    http_status = 422


class BadRequestError(AppError):
    """业务参数非法（400，不同于 Pydantic 校验的 422）。"""

    code = "BAD_REQUEST"
    http_status = 400


class UnauthorizedError(AppError):
    """未登录或登录态失效（401）。

    前端据此引导登录页；不要用 422/400 表达认证问题，否则用户只会看到一个
    错误提示却不知道要去登录。
    """

    code = "AUTH_REQUIRED"
    http_status = 401


class NotFoundError(AppError):
    """资源不存在。"""

    code = "NOT_FOUND"
    http_status = 404


class DataMissingError(AppError):
    """库内无对应数据（宁可少推不可错推）。"""

    code = "DATA_MISSING"
    http_status = 200  # 业务上是「正常但无数据」，用 200 + 明确文案


class RateLimitError(AppError):
    """触发限流。"""

    code = "RATE_LIMITED"
    http_status = 429


class FeedbackDuplicateError(AppError):
    """同一输出重复反馈（一条输出仅一次，DB 唯一约束兜底）。"""

    code = "FEEDBACK_DUPLICATE"
    http_status = 409


class PlanConflictError(AppError):
    """志愿表并发冲突（乐观锁版本不匹配）。"""

    code = "PLAN_VERSION_CONFLICT"
    http_status = 409


class ShareExpiredError(AppError):
    """分享链接已过期（只读快照仍在，但超出有效期）。"""

    code = "SHARE_EXPIRED"
    http_status = 410


class OutOfScopeError(AppError):
    """问题超出升学规划范围（Guardrails 拒答）。"""

    code = "OUT_OF_SCOPE"
    http_status = 200


# ==================== LLM 相关 ====================


class LLMError(AppError):
    """LLM 调用失败（含全链 failover 后仍失败）。

    上层捕获此异常后应切换到「专家规则模式」本地兜底，而不是直接报错给用户。
    """

    code = "LLM_ERROR"
    http_status = 200


# ==================== 注册全局处理器 ====================


def register_exception_handlers(app: FastAPI) -> None:
    """注册到 FastAPI 应用。"""
    from fastapi import status as http_status

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        # 只记警告级，不打印堆栈（避免敏感信息泄漏）
        logger.warning("业务异常 code=%s msg=%s path=%s", exc.code, exc.message, request.url.path)
        headers: dict[str, str] = {}
        # 限流：把 retry_after 写进 Retry-After 头（HTTP 语义标准）
        if isinstance(exc, RateLimitError) and isinstance(exc.detail, dict):
            ra = exc.detail.get("retry_after")
            if ra:
                headers["Retry-After"] = str(ra)
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_dict(request_id=request_id),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        # 未预期异常才记 error + 堆栈，便于排查
        logger.error("未捕获异常 path=%s err=%r", request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "ok": False,
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请稍后重试",
                "detail": None,
                "request_id": request_id,
            },
        )
