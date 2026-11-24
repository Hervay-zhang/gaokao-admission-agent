"""日志与脱敏模块。

红线：**日志禁止出现明文考分 / 位次 / 身份信息**。
本模块提供：
1. `setup_logging()`：统一日志格式（时间/级别/模块/行号）。
2. `mask_score()` / `mask_rank()`：把敏感数值转为区间描述。
3. `SensitiveFilter`：兜底过滤器，拦截形如「分数=612」的明文串并替换。

用法：
    from app.core.logging import get_logger
    logger = get_logger(__name__)
    logger.info("用户档案已建立，分数区间=%s", mask_score(612))
"""

from __future__ import annotations

import logging
import re
import sys

from app.core.config import settings

# ==================== 脱敏工具 ====================


def mask_score(score: int | float | None) -> str:
    """把具体分数转为 10 分档区间，如 612 -> '610-619'。"""
    if score is None:
        return "未知"
    try:
        v = int(score)
    except (TypeError, ValueError):
        return "非法"
    low = (v // 10) * 10
    return f"{low}-{low + 9}"


def mask_rank(rank: int | None) -> str:
    """把具体位次转为千分档区间，如 35412 -> '35000-35999'。"""
    if rank is None:
        return "未知"
    try:
        v = int(rank)
    except (TypeError, ValueError):
        return "非法"
    low = (v // 1000) * 1000
    return f"{low}-{low + 999}"


def mask_phone(phone: str | None) -> str:
    """手机号中间四位打码，如 13812345678 -> 138****5678。"""
    if not phone or len(phone) < 7:
        return "***"
    return f"{phone[:3]}****{phone[-4:]}"


# ==================== 兜底过滤器 ====================

# 匹配「分数=612」「score: 612」「位次 35412」等明文形态
_PATTERNS = [
    (re.compile(r"(分数|考分|score)\s*[=:：]\s*(\d{3})"), r"\1=***"),
    (re.compile(r"(位次|排名|rank)\s*[=:：]\s*(\d{4,7})"), r"\1=***"),
    (re.compile(r"(1[3-9]\d{9})"), lambda m: mask_phone(m.group(1))),
]


class SensitiveFilter(logging.Filter):
    """兜底：即使调用方忘了用 mask_*，也不会把明文分数/位次/手机号写进日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志过滤器绝不能自己抛异常
            return True
        masked = msg
        for pattern, repl in _PATTERNS:
            masked = pattern.sub(repl, masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


# ==================== 初始化 ====================

_LOGGER_INITIALIZED = False


def setup_logging() -> None:
    """初始化根 logger（幂等，可重复调用）。"""
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SensitiveFilter())

    root = logging.getLogger()
    # 清理已有 handler，避免 uvicorn --reload 重复输出
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # 压低第三方库噪音
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGER_INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """获取带脱敏能力的 logger。"""
    logger = logging.getLogger(name)
    if not any(isinstance(f, SensitiveFilter) for f in logger.filters):
        logger.addFilter(SensitiveFilter())
    return logger
