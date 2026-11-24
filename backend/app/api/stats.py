"""公开数据统计接口（首页展示真实库内数量）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AdmissionLine, ForumPost, PolicyDoc, University
from app.db.session import get_db

logger = get_logger(__name__)
router = APIRouter(prefix="/api/stats", tags=["stats"])

# 首页每次打开都会调这里，而它要跑 4 次 COUNT。
# 60 秒缓存足够（数据导入后最迟 1 分钟自然刷新）；Redis 不可用时静默回源，绝不阻断接口。
_CACHE_KEY = "stats:public:v1"
_CACHE_TTL = 60


def _cache_get() -> dict | None:
    try:
        from app.core.redis import get_redis, run_sync

        raw = run_sync(lambda: get_redis().get(_CACHE_KEY))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 —— 缓存只是加速手段
        logger.debug("统计缓存读取失败：%r", exc)
        return None


def _cache_set(payload: dict) -> None:
    try:
        from app.core.redis import get_redis, run_sync

        run_sync(lambda: get_redis().set(_CACHE_KEY, json.dumps(payload, ensure_ascii=False), ex=_CACHE_TTL))
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计缓存写入失败：%r", exc)


@router.get("", summary="公开数据与运行统计")
def public_stats(db: Session = Depends(get_db)) -> dict:
    cached = _cache_get()
    if cached is not None:
        return cached

    def _count(model) -> int:  # noqa: ANN001
        return int(db.execute(select(func.count(model.id))).scalar() or 0)

    payload = {
        "universities": _count(University),
        "admission_lines": _count(AdmissionLine),
        "policy_docs": _count(PolicyDoc),
        "community_posts": int(
            db.execute(
                select(func.count(ForumPost.id)).where(ForumPost.is_removed.is_(False))
            ).scalar()
            or 0
        ),
        "current_year": settings.current_year,
        "ref_years": list(settings.ref_year_list),
    }
    _cache_set(payload)
    return payload
