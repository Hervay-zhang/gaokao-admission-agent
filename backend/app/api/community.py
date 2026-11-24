"""社区/论坛接口。

匿名社区，session_sid 为用户标识。
限流沿用全局 limiter（IP + 会话），发帖/评论单独限速。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.db.models import UserAccount
from app.db.session import get_db
from app.schemas.community import (
    CommentCreateRequest,
    CommentCreateResponse,
    CommentListResponse,
    CommunityReportCreateRequest,
    CommunityReportItem,
    CommunityReportListResponse,
    CommunityReportResolveRequest,
    FavoriteToggleRequest,
    FavoriteToggleResponse,
    LikeToggleRequest,
    LikeToggleResponse,
    PinnedUpdateRequest,
    PinnedUpdateResponse,
    PostCreateRequest,
    PostCreateResponse,
    PostDetail,
    PostListResponse,
    PostUpdateRequest,
)
from app.services.community import get_community_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/community", tags=["community"])


def _get_sid(request: Request, param_sid: str = "") -> str:
    """优先用请求头 X-Session-Id，其次用 body 参数。"""
    header_sid = request.headers.get("X-Session-Id", "")
    return header_sid or param_sid or ""


def _identity(
    request: Request,
    db: Session,
    *,
    required: bool = False,
    fallback_sid: str = "",
) -> str:
    """生产环境写操作绑定登录账号；Mock 模式保留匿名测试能力。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        from app.services.auth import decode_token

        payload = decode_token(auth[7:].strip())
        if payload and payload.get("sub"):
            user = db.execute(
                select(UserAccount).where(
                    UserAccount.username == payload["sub"],
                    UserAccount.is_active.is_(True),
                )
            ).scalar_one_or_none()
            if user is not None:
                return f"u:{user.username}"
    if not settings.is_production:
        sid = _get_sid(request, fallback_sid)
        if sid:
            return sid
    if required:
        raise UnauthorizedError("请先登录后再操作", detail={"code": "AUTH_REQUIRED"})
    return ""


def _service(db: Session):
    """测试可注入内存服务；运行时每个请求使用独立数据库会话。"""
    return get_community_service(db)


def _clean_report(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    for key in ("created_at", "resolved_at"):
        value = out.get(key)
        if value is not None and not isinstance(value, str):
            out[key] = value.strftime("%Y-%m-%d %H:%M")
    return out


@router.get("/posts", response_model=PostListResponse, summary="帖子列表")
async def list_posts(
    request: Request,
    category: str | None = Query(default=None, max_length=32, description="板块筛选"),
    sort: str = Query(default="latest", description="latest / hot / most_commented"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=60),
    db: Session = Depends(get_db),
) -> PostListResponse:
    service = _service(db)
    user_sid = _identity(request, db)
    result = service.list_posts(
        category=category,
        sort=sort,
        page=page,
        page_size=page_size,
        keyword=keyword,
        user_sid=user_sid,
    )
    return PostListResponse(**result)


@router.get("/posts/{post_id}", response_model=PostDetail, summary="帖子详情")
async def get_post(post_id: int, request: Request, db: Session = Depends(get_db)) -> PostDetail:
    service = _service(db)
    user_sid = _identity(request, db)
    post = service.get_post_detail(post_id, user_sid)
    return PostDetail(**post)


@router.post("/posts", response_model=PostCreateResponse, summary="发帖")
async def create_post(req: PostCreateRequest, request: Request, db: Session = Depends(get_db)) -> PostCreateResponse:
    service = _service(db)
    session_sid = _identity(request, db, required=True, fallback_sid=req.session_sid)
    result = service.create_post(
        title=req.title,
        content=req.content,
        category=req.category,
        session_sid=session_sid,
        nickname=req.nickname,
    )
    logger.info("发帖 post_id=%s category=%s author_sid_len=%s", result["post_id"], req.category, len(session_sid))
    return PostCreateResponse(post_id=result["post_id"])


@router.post("/posts/{post_id}/like", response_model=LikeToggleResponse, summary="点赞/取消点赞")
async def toggle_like(
    post_id: int, req: LikeToggleRequest, request: Request, db: Session = Depends(get_db)
) -> LikeToggleResponse:
    service = _service(db)
    user_sid = _identity(request, db, required=True, fallback_sid=req.session_sid)
    result = service.toggle_like(post_id, user_sid)
    return LikeToggleResponse(**result)


@router.post("/posts/{post_id}/favorite", response_model=FavoriteToggleResponse, summary="收藏/取消收藏")
async def toggle_favorite(
    post_id: int, req: FavoriteToggleRequest, request: Request, db: Session = Depends(get_db)
) -> FavoriteToggleResponse:
    service = _service(db)
    user_sid = _identity(request, db, required=True, fallback_sid=req.session_sid)
    result = service.toggle_favorite(post_id, user_sid)
    return FavoriteToggleResponse(**result)


@router.get("/posts/{post_id}/comments", response_model=CommentListResponse, summary="评论列表")
async def list_comments(post_id: int, request: Request, db: Session = Depends(get_db)) -> CommentListResponse:
    service = _service(db)
    user_sid = _identity(request, db)
    result = service.list_comments(post_id, user_sid)
    return CommentListResponse(**result)


@router.post("/posts/{post_id}/comments", response_model=CommentCreateResponse, summary="发表评论")
async def create_comment(
    post_id: int, req: CommentCreateRequest, request: Request, db: Session = Depends(get_db)
) -> CommentCreateResponse:
    service = _service(db)
    session_sid = _identity(request, db, required=True, fallback_sid=req.session_sid)
    result = service.create_comment(
        post_id=post_id,
        content=req.content,
        session_sid=session_sid,
        nickname=req.nickname,
        reply_to_id=req.reply_to_id,
    )
    return CommentCreateResponse(comment_id=result["comment_id"])


@router.delete("/posts/{post_id}", summary="删除帖子（仅作者）")
async def delete_post(post_id: int, request: Request, db: Session = Depends(get_db)) -> dict[str, bool]:
    service = _service(db)
    user_sid = _identity(request, db, required=True)
    service.delete_post(post_id, user_sid)
    return {"ok": True}


@router.put("/posts/{post_id}", response_model=PostDetail, summary="编辑帖子（仅作者）")
async def update_post(
    post_id: int,
    req: PostUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> PostDetail:
    service = _service(db)
    user_sid = _identity(request, db, required=True)
    service.update_post(
        post_id,
        user_sid,
        title=req.title,
        content=req.content,
        category=req.category,
    )
    return PostDetail(**service.get_post_detail(post_id, user_sid))


@router.put("/posts/{post_id}/pin", response_model=PinnedUpdateResponse, summary="管理员置顶/取消置顶")
async def set_post_pinned(
    post_id: int,
    req: PinnedUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> PinnedUpdateResponse:
    require_admin(request)
    _service(db).set_pinned(post_id, req.pinned)
    return PinnedUpdateResponse(pinned=req.pinned)


@router.post("/comments/{comment_id}/like", response_model=LikeToggleResponse, summary="评论点赞/取消")
async def toggle_comment_like(
    comment_id: int,
    req: LikeToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> LikeToggleResponse:
    service = _service(db)
    user_sid = _identity(request, db, required=True, fallback_sid=req.session_sid)
    return LikeToggleResponse(**service.toggle_comment_like(comment_id, user_sid))


@router.delete("/comments/{comment_id}", summary="删除评论（仅作者）")
async def delete_comment(
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    service = _service(db)
    user_sid = _identity(request, db, required=True)
    service.delete_comment(comment_id, user_sid)
    return {"ok": True}


@router.post("/reports", response_model=CommunityReportItem, summary="举报帖子或评论")
async def create_report(
    req: CommunityReportCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> CommunityReportItem:
    service = _service(db)
    reporter_sid = _identity(request, db, required=True)
    return CommunityReportItem(
        **_clean_report(service.create_report(
            target_type=req.target_type,
            target_id=req.target_id,
            reporter_sid=reporter_sid,
            reason=req.reason,
            detail=req.detail,
        ))
    )


@router.get(
    "/admin/reports",
    response_model=CommunityReportListResponse,
    summary="管理员查看社区举报",
)
async def admin_list_reports(
    request: Request,
    status: str | None = Query(default=None, pattern="^(pending|resolved|dismissed)$"),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> CommunityReportListResponse:
    require_admin(request)
    items = [_clean_report(item) for item in _service(db).list_reports(status=status, limit=limit)]
    return CommunityReportListResponse(total=len(items), items=[CommunityReportItem(**item) for item in items])


@router.patch(
    "/admin/reports/{report_id}",
    response_model=CommunityReportItem,
    summary="管理员处理社区举报",
)
async def admin_resolve_report(
    report_id: int,
    req: CommunityReportResolveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> CommunityReportItem:
    require_admin(request)
    item = _service(db).resolve_report(report_id, req.status, "admin")
    return CommunityReportItem(**_clean_report(item))


@router.delete("/admin/posts/{post_id}", summary="管理员删除帖子")
async def admin_delete_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    require_admin(request)
    _service(db).admin_delete_post(post_id)
    return {"ok": True}


@router.delete("/admin/comments/{comment_id}", summary="管理员删除评论")
async def admin_delete_comment(
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    require_admin(request)
    _service(db).admin_delete_comment(comment_id)
    return {"ok": True}


# ============================================================
#  个人主页
# ============================================================


@router.get("/user/{user_sid}/posts", response_model=PostListResponse, summary="用户发帖列表")
async def user_posts(
    user_sid: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PostListResponse:
    service = _service(db)
    viewer_sid = _identity(request, db)
    result = service.user_posts(user_sid, page, page_size, viewer_sid)
    result["categories"] = {}
    return PostListResponse(**result)


@router.get("/user/{user_sid}/favorites", response_model=PostListResponse, summary="用户收藏列表")
async def user_favorites(
    user_sid: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PostListResponse:
    service = _service(db)
    viewer_sid = _identity(request, db)
    result = service.user_favorites(user_sid, page, page_size, viewer_sid)
    result["categories"] = {}
    return PostListResponse(**result)


@router.get("/user/{user_sid}/comments", response_model=CommentListResponse, summary="用户评论列表")
async def user_comments(
    user_sid: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> CommentListResponse:
    service = _service(db)
    viewer_sid = _identity(request, db)
    result = service.user_comments(user_sid, page, page_size, viewer_sid)
    return CommentListResponse(**result)


@router.get("/user/{user_sid}/stats", summary="用户统计（发帖/评论/收藏数）")
async def user_stats(user_sid: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    service = _service(db)
    return service.user_stats(user_sid)


@router.get("/categories", summary="板块分类")
async def list_categories() -> dict[str, dict[str, str]]:
    from app.schemas.community import FORUM_CATEGORIES
    return {"categories": FORUM_CATEGORIES}
