"""T31 · 管理后台 API（Token 鉴权；管理功能收敛入口）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/admin/overview` | 数据量总览 + 当前数据版本 + 反馈统计 + 限流阈值与占用 |
| GET  | `/api/admin/versions` | 数据版本历史（含回滚快照）+ 当前生效配置 |
| POST | `/api/admin/versions/switch` | 年份窗口切换（先校验后落库；支持 dry_run 预览） |
| POST | `/api/admin/import` | 触发幂等数据导入（后台线程；重复触发 409） |
| GET  | `/api/admin/import` | 导入任务状态 |
| GET  | `/api/admin/feedback` | 最近反馈列表（快照已脱敏） |
| GET  | `/api/admin/disclaimer` | 免责文案清单（**红线常量，只读**，不做在线配置） |

鉴权：请求头 `X-Admin-Token` 须等于 `settings.admin_token`（`hmac.compare_digest`
常数时间比较）；未带/错误 → **404**（不暴露端点存在性）。本地 `MOCK_MODE=true`
时允许登录页管理员角色通过 `X-Admin-Demo-Role: admin` 查看后台；生产模式必须使用
Token。Token 默认值 `change-me` 仅本机可用，**生产必须在 `.env` 修改**（见 docs/deploy.md §6）。

范围说明（与任务卡对齐的取舍）
----------------------------
- **免责文案不做在线修改**：免责是合规红线常量（推荐/问答/分享/导出多处引用），
  在线可配反而引入「改丢免责」的合规风险——故只提供只读清单端点。
- **限流日志**：限流触发事件进应用日志（`触发限流 scope=…`），不落 DB（高频写无价值）；
  本后台提供**实时阈值与连接占用**视图 + 可观测端点，满足「限流状态可查」。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError, PlanConflictError
from app.core.logging import get_logger
from app.db.models import AuditLog, QuickLink, University, UserAccount
from app.db.session import get_db
from app.schemas.quicklinks import (
    QuickLinkCreateRequest,
    QuickLinkItem,
    QuickLinkListResponse,
    QuickLinkUpdateRequest,
)
from app.services.data_version import get_data_version_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserUpdateRequest(BaseModel):
    role: str = Field(..., pattern="^(student|parent|admin)$")
    is_active: bool = True
    # 重置密码（留空表示不改）。自助找回需要短信/邮箱，目前没有，这是唯一的重置通道。
    new_password: str = Field(default="", max_length=128)


class UniversityAdminUpdateRequest(BaseModel):
    admission_url: str = Field(default="", max_length=512)
    admission_url_status: str = Field(default="待核", pattern="^(已核|待核|不可用)$")


def _audit(db: Session, *, action: str, actor: str, detail: dict[str, Any]) -> None:
    db.add(AuditLog(action=action, actor=actor, detail=json.dumps(detail, ensure_ascii=False)))
    db.commit()


# ============================================================
#  鉴权
# ============================================================


def _require_admin(request: Request) -> None:
    require_admin(request)


# ============================================================
#  总览
# ============================================================

_COUNT_TABLES = (
    ("provinces", "省份"),
    ("score_segments", "一分一段"),
    ("universities", "院校"),
    ("admission_lines", "投档线"),
    ("policy_docs", "政策文档"),
    ("policy_chunks", "政策切片"),
    ("sessions", "会话"),
    ("messages", "消息"),
    ("feedback", "反馈"),
)


def _table_counts() -> dict[str, int]:
    """各业务表行数（管理端低频查询，直接 COUNT）。"""
    from sqlalchemy import text

    from app.db.session import SessionLocal

    out: dict[str, int] = {}
    with SessionLocal() as db:
        for table, _label in _COUNT_TABLES:
            try:
                out[table] = int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            except Exception as exc:  # noqa: BLE001 —— 单表失败不拖垮总览
                logger.debug("admin 行数统计失败 %s: %r", table, exc)
                out[table] = -1
    return out


def _active_version() -> dict[str, Any]:
    try:
        from app.services.data_version import get_data_version_service

        cfg = get_data_version_service().get_active()
        return {
            "current_year": cfg.current_year,
            "ref_years": list(cfg.ref_years),
            "year_weights": list(cfg.year_weights),
            "note": cfg.note,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("admin 版本读取失败: %r", exc)
        return {"error": "unavailable"}


def _ratelimit_view() -> dict[str, Any]:
    from app.core.ratelimit import get_rate_limiter

    limiter = get_rate_limiter()
    s = limiter.s
    return {
        "ip_per_min": s.rate_limit_ip_per_min,
        "ip_per_day": s.rate_limit_ip_per_day,
        "session_per_min": s.rate_limit_session_per_min,
        "session_per_day": s.rate_limit_session_per_day,
        "sse_concurrent": s.rate_limit_sse_concurrent,
        "sse_max_seconds": s.rate_limit_sse_max_seconds,
        "retry_after": s.rate_limit_retry_after,
        "whitelist_ips": s.rate_limit_whitelist_ips,
    }


@router.get("/overview", summary="数据与运行总览")
async def admin_overview(request: Request) -> dict[str, Any]:
    _require_admin(request)
    counts = await asyncio.to_thread(_table_counts)
    feedback = counts.get("feedback", 0)
    return {
        "table_counts": counts,
        "data_version": _active_version(),
        "feedback_total": feedback,
        "ratelimit": _ratelimit_view(),
        "mock_mode": settings.mock_mode,
        "current_year": settings.current_year,
    }


# ============================================================
#  数据版本（年份窗口切换 / 回滚历史）
# ============================================================


@router.get("/versions", summary="版本历史与当前生效配置")
def admin_versions(request: Request) -> dict[str, Any]:
    _require_admin(request)
    svc = get_data_version_service()
    active = svc.get_active()
    return {
        "active": {
            "current_year": active.current_year,
            "ref_years": list(active.ref_years),
            "year_weights": list(active.year_weights),
            "note": active.note,
        },
        "snapshots": [s.to_dict() for s in svc.list_snapshots()][-20:],
    }


class SwitchRequest(BaseModel):
    # 只做类型/粗界校验；业务校验（时效红线等）在服务层统一把关（口径唯一）
    current_year: int | None = Field(None, ge=2000, le=2100)
    ref_years: list[int] | None = Field(None, description="参考年份（从旧到新）")
    year_weights: list[float] | None = Field(None, description="年份权重（与 ref_years 一一对应）")
    note: str = Field("", max_length=200)
    dry_run: bool = Field(False, description="只校验不落库（预览影响面）")


@router.post("/versions/switch", summary="切换年份窗口（先校验后落库）")
async def admin_switch_version(payload: SwitchRequest, request: Request) -> dict[str, Any]:
    _require_admin(request)
    svc = get_data_version_service()
    cfg = svc.switch(
        current_year=payload.current_year,
        ref_years=payload.ref_years,
        year_weights=payload.year_weights,
        actor="admin-api",
        note=payload.note,
        dry_run=payload.dry_run,
    )
    return {"ok": True, "dry_run": payload.dry_run, "active": {
        "current_year": cfg.current_year,
        "ref_years": list(cfg.ref_years),
        "year_weights": list(cfg.year_weights),
        "note": cfg.note,
    }}


# ============================================================
#  数据导入（触发 / 状态）
# ============================================================

_import_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_import_lock = threading.Lock()


def _run_import() -> None:
    """后台线程跑幂等导入（scripts/import_data.py 的 main([])）。"""
    global _import_state
    try:
        from scripts.import_data import main as import_main  # type: ignore[import-not-found]

        code = import_main([])
        _import_state.update(result="ok" if code == 0 else f"exit={code}", error=None)
    except Exception as exc:  # noqa: BLE001 —— 状态如实记录，不抛
        logger.error("admin 触发导入失败: %r", exc)
        _import_state.update(result=None, error=str(exc)[:500])
    finally:
        _import_state.update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


@router.post("/import", summary="触发幂等数据导入（后台执行）")
async def admin_import(request: Request) -> dict[str, Any]:
    _require_admin(request)
    with _import_lock:
        if _import_state["running"]:
            raise PlanConflictError("已有导入任务在执行中", detail={"code": "IMPORT_RUNNING"})
        _import_state.update(running=True, started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                             finished_at=None, result=None, error=None)
    threading.Thread(target=_run_import, daemon=True).start()
    logger.info("admin 触发数据导入")
    return {"ok": True, "running": True}


@router.get("/import", summary="导入任务状态")
async def admin_import_status(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return dict(_import_state)


# ============================================================
#  反馈查看 / 免责清单（只读）
# ============================================================


@router.get("/feedback", summary="最近反馈（快照已脱敏）")
def admin_feedback(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    from app.services.feedback import get_feedback_service

    items = get_feedback_service(db).recent(limit)
    return {"total": len(items), "items": items}


@router.get("/disclaimer", summary="免责文案清单（红线常量，只读）")
async def admin_disclaimer(request: Request) -> dict[str, Any]:
    _require_admin(request)
    from app.schemas.recommend import RecommendResult
    from app.services.share import DISCLAIMER as SHARE_DISCLAIMER

    return {
        "note": "免责文案为合规红线常量，仅只读展示，不支持在线修改（避免「改丢免责」的合规风险）",
        "entries": {
            # 唯一来源：schemas/recommend.py RecommendResult.disclaimer 默认值
            "recommend_report": RecommendResult.model_fields["disclaimer"].default,
            "chat_stream": "本建议仅供参考，不作为最终填报承诺",
            "share_snapshot": SHARE_DISCLAIMER,
        },
    }


# ============================================================
#  快捷链接（页面底部，可增删改；公开读取见 /api/quick-links）
# ============================================================


def _quick_link_item(r: QuickLink) -> QuickLinkItem:
    return QuickLinkItem(
        id=r.id,
        label=r.label,
        url=r.url,
        icon=r.icon or "",
        group=r.group or "gaokao",
        sort_order=r.sort_order or 0,
        is_active=r.is_active,
    )


@router.get("/quick-links", response_model=QuickLinkListResponse, summary="快捷链接管理列表（含禁用）")
def admin_quick_links(
    request: Request,
    db: Session = Depends(get_db),
) -> QuickLinkListResponse:
    _require_admin(request)
    rows = db.execute(
        select(QuickLink).order_by(QuickLink.sort_order.asc(), QuickLink.id.asc())
    ).scalars().all()
    items = [_quick_link_item(r) for r in rows]
    return QuickLinkListResponse(total=len(items), items=items)


@router.post("/quick-links", response_model=QuickLinkItem, summary="新增快捷链接")
def admin_create_quick_link(
    payload: QuickLinkCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> QuickLinkItem:
    _require_admin(request)
    row = QuickLink(
        label=payload.label.strip(),
        url=payload.url.strip(),
        icon=(payload.icon or "").strip(),
        group=(payload.group or "gaokao").strip() or "gaokao",
        sort_order=payload.sort_order,
        is_active=payload.is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(db, action="quick_link_create", actor="admin", detail={"id": row.id, "label": row.label})
    return _quick_link_item(row)


@router.put("/quick-links/{link_id}", response_model=QuickLinkItem, summary="更新快捷链接")
def admin_update_quick_link(
    link_id: int,
    payload: QuickLinkUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> QuickLinkItem:
    _require_admin(request)
    row = db.execute(select(QuickLink).where(QuickLink.id == link_id)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("快捷链接不存在", detail={"code": "QUICK_LINK_NOT_FOUND"})
    row.label = payload.label.strip()
    row.url = payload.url.strip()
    row.icon = (payload.icon or "").strip()
    row.group = (payload.group or "gaokao").strip() or "gaokao"
    row.sort_order = payload.sort_order
    row.is_active = payload.is_active
    db.commit()
    db.refresh(row)
    _audit(db, action="quick_link_update", actor="admin", detail={"id": row.id, "label": row.label})
    return _quick_link_item(row)


@router.delete("/quick-links/{link_id}", summary="删除快捷链接")
def admin_delete_quick_link(
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    row = db.execute(select(QuickLink).where(QuickLink.id == link_id)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("快捷链接不存在", detail={"code": "QUICK_LINK_NOT_FOUND"})
    db.delete(row)
    db.commit()
    _audit(db, action="quick_link_delete", actor="admin", detail={"id": link_id})
    logger.info("admin 删除快捷链接 id=%s", link_id)
    return {"ok": True, "deleted": link_id}


# ============================================================
#  用户管理
# ============================================================


def _user_item(row: UserAccount) -> dict[str, Any]:
    return {
        "username": row.username,
        "role": row.role,
        "role_label": {"student": "考生", "parent": "家长", "admin": "管理员"}.get(row.role, row.role),
        "name": row.nickname or row.display_name,
        "grade": row.grade or "",
        "is_active": bool(row.is_active),
        "avatar_url": row.avatar_data or "",
        "last_login_at": row.last_login_at.strftime("%Y-%m-%d %H:%M") if row.last_login_at else "",
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


@router.get("/users", summary="账号列表（管理员）")
def admin_users(
    request: Request,
    keyword: str = Query(default="", max_length=64),
    role: str = Query(default="", pattern="^(|student|parent|admin)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    stmt = select(UserAccount)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                UserAccount.username.like(like),
                UserAccount.display_name.like(like),
                UserAccount.nickname.like(like),
            )
        )
    if role:
        stmt = stmt.where(UserAccount.role == role)
    rows = db.execute(stmt.order_by(UserAccount.id.asc())).scalars().all()
    items = [_user_item(row) for row in rows]
    return {"total": len(items), "items": items}


@router.patch("/users/{username}", summary="更新账号角色或启停（管理员）")
def admin_update_user(
    username: str,
    payload: AdminUserUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    row = db.execute(select(UserAccount).where(UserAccount.username == username)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("账号不存在", detail={"code": "USER_NOT_FOUND"})
    if row.username == "admin" and (payload.role != "admin" or not payload.is_active):
        raise BadRequestError("内置管理员账号不能停用或降级", detail={"code": "ADMIN_PROTECTED"})
    new_password = payload.new_password.strip()
    if new_password and len(new_password) < 8:
        raise BadRequestError("新密码至少 8 位", detail={"code": "PASSWORD_TOO_SHORT"})
    old = {"role": row.role, "is_active": bool(row.is_active)}
    row.role = payload.role
    row.is_active = payload.is_active
    if new_password:
        from app.services.auth import hash_password

        row.password_hash = hash_password(new_password)
    db.commit()
    _audit(
        db,
        action="admin_user_update",
        actor="admin",
        # 审计只记「是否重置了密码」，绝不落明文
        detail={
            "username": username,
            "old": old,
            "new": payload.model_dump(exclude={"new_password"}),
            "password_reset": bool(new_password),
        },
    )
    db.refresh(row)
    return {"ok": True, "item": _user_item(row), "password_reset": bool(new_password)}


# ============================================================
#  院校招生官网维护
# ============================================================


def _university_admin_item(row: University) -> dict[str, Any]:
    return {
        "code": row.code,
        "name": row.name,
        "province": row.province or "",
        "level": row.level,
        "admission_url": row.admission_url or "",
        "admission_url_status": row.admission_url_status or "待核",
        "admission_url_updated_at": (
            row.admission_url_updated_at.strftime("%Y-%m-%d %H:%M")
            if row.admission_url_updated_at else ""
        ),
    }


@router.get("/universities", summary="院校官网维护列表（管理员）")
def admin_universities(
    request: Request,
    keyword: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    stmt = select(University)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(University.name.like(like), University.code.like(like)))
    total = len(db.execute(stmt).scalars().all())
    rows = (
        db.execute(stmt.order_by(University.code.asc()).offset((page - 1) * page_size).limit(page_size))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_university_admin_item(row) for row in rows],
    }


@router.patch("/universities/{code}", summary="更新院校招生官网（管理员）")
def admin_update_university(
    code: str,
    payload: UniversityAdminUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    row = db.execute(select(University).where(University.code == code)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("院校不存在", detail={"code": "UNI_NOT_FOUND"})
    url = payload.admission_url.strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise BadRequestError("官网地址必须以 http:// 或 https:// 开头", detail={"code": "URL_INVALID"})
    row.admission_url = url or None
    row.admission_url_status = payload.admission_url_status
    row.admission_url_updated_at = datetime.now()
    db.commit()
    _audit(
        db,
        action="university_admission_url_update",
        actor="admin",
        detail={"code": code, **payload.model_dump()},
    )
    db.refresh(row)
    return {"ok": True, "item": _university_admin_item(row)}


# ============================================================
#  操作日志
# ============================================================


@router.get("/audit-logs", summary="管理操作日志")
def admin_audit_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(request)
    rows = (
        db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    items = [
        {
            "id": int(row.id),
            "action": row.action,
            "actor": row.actor or "",
            "detail": row.detail or "",
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        }
        for row in rows
    ]
    return {"total": len(items), "items": items}
