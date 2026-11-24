"""社区/论坛服务层。

匿名优先：以 session_sid 标识用户，昵称快照化。
计数缓存：like_count / comment_count / view_count 在帖子表上冗余维护。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import desc, func, or_, select

from app.core.exceptions import BadRequestError, NotFoundError, UnauthorizedError
from app.db.models import (
    CommunityReport,
    ForumComment,
    ForumCommentLike,
    ForumPost,
    ForumPostFavorite,
    ForumPostLike,
)
from app.schemas.community import (
    ALLOWED_CATEGORIES,
    COMMENT_MAX_LEN,
    CONTENT_MAX_LEN,
    FORUM_CATEGORIES,
    TITLE_MAX_LEN,
)

# ============================================================
#  Protocol
# ============================================================


class CommunityRepository(Protocol):
    def create_post(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_post(self, post_id: int) -> dict[str, Any] | None: ...
    def list_posts(
        self,
        category: str | None,
        sort: str,
        page: int,
        page_size: int,
        keyword: str | None,
    ) -> tuple[int, list[dict[str, Any]]]: ...
    def increment_view(self, post_id: int) -> None: ...
    def delete_post(self, post_id: int, user_sid: str) -> None: ...
    def update_post(self, post_id: int, user_sid: str, **changes: Any) -> dict[str, Any]: ...
    def admin_delete_post(self, post_id: int) -> None: ...
    def set_pinned(self, post_id: int, pinned: bool) -> None: ...

    def create_comment(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_comments(self, post_id: int) -> tuple[int, list[dict[str, Any]]]: ...
    def delete_comment(self, comment_id: int, user_sid: str) -> None: ...
    def admin_delete_comment(self, comment_id: int) -> None: ...

    def toggle_like(self, post_id: int, user_sid: str) -> tuple[bool, int]: ...
    def toggle_comment_like(self, comment_id: int, user_sid: str) -> tuple[bool, int]: ...
    def toggle_favorite(self, post_id: int, user_sid: str) -> bool: ...
    def has_liked(self, post_id: int, user_sid: str) -> bool: ...
    def has_comment_liked(self, comment_id: int, user_sid: str) -> bool: ...
    def has_favorited(self, post_id: int, user_sid: str) -> bool: ...

    def create_report(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_reports(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...
    def resolve_report(self, report_id: int, status: str, resolver: str) -> dict[str, Any]: ...

    def user_posts(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]: ...
    def user_favorites(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]: ...
    def user_comments(
        self, user_sid: str, page: int, page_size: int, viewer_sid: str = ""
    ) -> tuple[int, list[dict[str, Any]]]: ...
    def user_stats(self, user_sid: str) -> dict[str, Any]: ...


# ============================================================
#  SQL 实现
# ============================================================


class SqlCommunityRepository:
    def __init__(self, db):
        from app.db.session import get_db  # noqa: F401
        self._db = db

    def _row_to_post(self, row) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "title": row.title,
            "content": row.content,
            "category": row.category,
            "author_sid": row.author_sid,
            "author_nickname": row.author_nickname,
            "like_count": int(row.like_count),
            "comment_count": int(row.comment_count),
            "view_count": int(row.view_count),
            "is_pinned": bool(row.is_pinned),
            "is_removed": bool(row.is_removed),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _row_to_comment(self, row) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "post_id": int(row.post_id),
            "content": row.content,
            "author_sid": row.author_sid,
            "author_nickname": row.author_nickname,
            "like_count": int(row.like_count),
            "reply_to_id": int(row.reply_to_id) if row.reply_to_id else None,
            "reply_to_nickname": row.reply_to_nickname,
            "is_removed": bool(row.is_removed),
            "created_at": row.created_at,
        }

    def create_post(self, **kwargs: Any) -> dict[str, Any]:
        post = ForumPost(**kwargs)
        self._db.add(post)
        self._db.flush()
        self._db.commit()
        self._db.refresh(post)
        return self._row_to_post(post)

    def get_post(self, post_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id, ForumPost.is_removed.is_(False))
        ).scalar_one_or_none()
        return self._row_to_post(row) if row else None

    def list_posts(
        self,
        category: str | None,
        sort: str,
        page: int,
        page_size: int,
        keyword: str | None,
    ) -> tuple[int, list[dict[str, Any]]]:
        query = select(ForumPost).where(ForumPost.is_removed.is_(False))
        count_query = select(func.count(ForumPost.id)).where(ForumPost.is_removed.is_(False))

        if category and category in ALLOWED_CATEGORIES:
            query = query.where(ForumPost.category == category)
            count_query = count_query.where(ForumPost.category == category)

        if keyword:
            like_pat = f"%{keyword}%"
            query = query.where(or_(ForumPost.title.like(like_pat), ForumPost.content.like(like_pat)))
            count_query = count_query.where(or_(ForumPost.title.like(like_pat), ForumPost.content.like(like_pat)))

        # 排序：置顶优先
        if sort == "hot":
            query = query.order_by(
                desc(ForumPost.is_pinned),
                desc(ForumPost.like_count + ForumPost.comment_count * 2 + ForumPost.view_count // 10),
                desc(ForumPost.created_at),
            )
        elif sort == "most_commented":
            query = query.order_by(desc(ForumPost.is_pinned), desc(ForumPost.comment_count), desc(ForumPost.created_at))
        else:
            query = query.order_by(desc(ForumPost.is_pinned), desc(ForumPost.created_at))

        total = self._db.execute(count_query).scalar() or 0
        rows = (
            self._db.execute(query.offset((page - 1) * page_size).limit(page_size))
            .scalars()
            .all()
        )
        return int(total), [self._row_to_post(r) for r in rows]

    def increment_view(self, post_id: int) -> None:
        self._db.query(ForumPost).filter(ForumPost.id == post_id).update(
            {"view_count": ForumPost.view_count + 1}
        )
        self._db.commit()

    def delete_post(self, post_id: int, user_sid: str) -> None:
        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id)
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        if post.author_sid != user_sid:
            raise BadRequestError("只能删除自己的帖子", detail={"code": "NOT_AUTHOR"})
        post.is_removed = True
        self._db.commit()

    def update_post(self, post_id: int, user_sid: str, **changes: Any) -> dict[str, Any]:
        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id, ForumPost.is_removed.is_(False))
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        if post.author_sid != user_sid:
            raise BadRequestError("只能编辑自己的帖子", detail={"code": "NOT_AUTHOR"})
        for key in ("title", "content", "category"):
            if key in changes and changes[key] is not None:
                setattr(post, key, changes[key])
        self._db.commit()
        self._db.refresh(post)
        return self._row_to_post(post)

    def admin_delete_post(self, post_id: int) -> None:
        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id)
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        post.is_removed = True
        self._db.commit()

    def set_pinned(self, post_id: int, pinned: bool) -> None:
        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id, ForumPost.is_removed.is_(False))
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        post.is_pinned = bool(pinned)
        self._db.commit()

    def create_comment(self, **kwargs: Any) -> dict[str, Any]:
        comment = ForumComment(**kwargs)
        self._db.add(comment)
        self._db.flush()
        # 同步帖子评论计数
        self._db.query(ForumPost).filter(ForumPost.id == kwargs["post_id"]).update(
            {"comment_count": ForumPost.comment_count + 1}
        )
        self._db.commit()
        self._db.refresh(comment)
        return self._row_to_comment(comment)

    def list_comments(self, post_id: int) -> tuple[int, list[dict[str, Any]]]:
        count = (
            self._db.execute(
                select(func.count(ForumComment.id)).where(
                    ForumComment.post_id == post_id,
                    ForumComment.is_removed.is_(False),
                )
            ).scalar()
            or 0
        )
        rows = (
            self._db.execute(
                select(ForumComment)
                .where(ForumComment.post_id == post_id, ForumComment.is_removed.is_(False))
                .order_by(ForumComment.created_at.asc())
            )
            .scalars()
            .all()
        )
        return int(count), [self._row_to_comment(r) for r in rows]

    def delete_comment(self, comment_id: int, user_sid: str) -> None:
        row = self._db.execute(
            select(ForumComment).where(ForumComment.id == comment_id)
        ).scalar_one_or_none()
        if not row:
            raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})
        if row.author_sid != user_sid:
            raise BadRequestError("只能删除自己的评论", detail={"code": "NOT_AUTHOR"})
        row.is_removed = True
        post = self._db.get(ForumPost, row.post_id)
        if post is not None:
            post.comment_count = max(0, (post.comment_count or 0) - 1)
        self._db.commit()

    def admin_delete_comment(self, comment_id: int) -> None:
        row = self._db.execute(
            select(ForumComment).where(ForumComment.id == comment_id)
        ).scalar_one_or_none()
        if not row:
            raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})
        row.is_removed = True
        post = self._db.get(ForumPost, row.post_id)
        if post is not None:
            post.comment_count = max(0, (post.comment_count or 0) - 1)
        self._db.commit()

    def toggle_like(self, post_id: int, user_sid: str) -> tuple[bool, int]:
        existing = self._db.execute(
            select(ForumPostLike).where(
                ForumPostLike.post_id == post_id,
                ForumPostLike.user_sid == user_sid,
            )
        ).scalar_one_or_none()

        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id, ForumPost.is_removed.is_(False))
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

        if existing:
            self._db.delete(existing)
            post.like_count = max(0, post.like_count - 1)
            liked = False
        else:
            like = ForumPostLike(post_id=post_id, user_sid=user_sid)
            self._db.add(like)
            post.like_count = post.like_count + 1
            liked = True

        self._db.commit()
        return liked, post.like_count

    def toggle_comment_like(self, comment_id: int, user_sid: str) -> tuple[bool, int]:
        row = self._db.execute(
            select(ForumComment).where(
                ForumComment.id == comment_id,
                ForumComment.is_removed.is_(False),
            )
        ).scalar_one_or_none()
        if not row:
            raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})
        existing = self._db.execute(
            select(ForumCommentLike).where(
                ForumCommentLike.comment_id == comment_id,
                ForumCommentLike.user_sid == user_sid,
            )
        ).scalar_one_or_none()
        if existing:
            self._db.delete(existing)
            row.like_count = max(0, row.like_count - 1)
            liked = False
        else:
            self._db.add(ForumCommentLike(comment_id=comment_id, user_sid=user_sid))
            row.like_count += 1
            liked = True
        self._db.commit()
        return liked, row.like_count

    def toggle_favorite(self, post_id: int, user_sid: str) -> bool:
        existing = self._db.execute(
            select(ForumPostFavorite).where(
                ForumPostFavorite.post_id == post_id,
                ForumPostFavorite.user_sid == user_sid,
            )
        ).scalar_one_or_none()

        post = self._db.execute(
            select(ForumPost).where(ForumPost.id == post_id, ForumPost.is_removed.is_(False))
        ).scalar_one_or_none()
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

        if existing:
            self._db.delete(existing)
            self._db.commit()
            return False
        else:
            fav = ForumPostFavorite(post_id=post_id, user_sid=user_sid)
            self._db.add(fav)
            self._db.commit()
            return True

    def has_liked(self, post_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        row = self._db.execute(
            select(ForumPostLike).where(
                ForumPostLike.post_id == post_id,
                ForumPostLike.user_sid == user_sid,
            )
        ).scalar_one_or_none()
        return row is not None

    def has_comment_liked(self, comment_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        row = self._db.execute(
            select(ForumCommentLike).where(
                ForumCommentLike.comment_id == comment_id,
                ForumCommentLike.user_sid == user_sid,
            )
        ).scalar_one_or_none()
        return row is not None

    def has_favorited(self, post_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        row = self._db.execute(
            select(ForumPostFavorite).where(
                ForumPostFavorite.post_id == post_id,
                ForumPostFavorite.user_sid == user_sid,
            )
        ).scalar_one_or_none()
        return row is not None

    def user_posts(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
        count = (
            self._db.execute(
                select(func.count(ForumPost.id)).where(
                    ForumPost.author_sid == user_sid,
                    ForumPost.is_removed.is_(False),
                )
            ).scalar()
            or 0
        )
        rows = (
            self._db.execute(
                select(ForumPost)
                .where(ForumPost.author_sid == user_sid, ForumPost.is_removed.is_(False))
                .order_by(desc(ForumPost.created_at))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return int(count), [self._row_to_post(r) for r in rows]

    def user_favorites(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
        base = (
            select(ForumPost)
            .join(ForumPostFavorite, ForumPostFavorite.post_id == ForumPost.id)
            .where(
                ForumPostFavorite.user_sid == user_sid,
                ForumPost.is_removed.is_(False),
            )
            .order_by(desc(ForumPostFavorite.created_at))
        )
        count_base = select(func.count(ForumPost.id)).join(
            ForumPostFavorite, ForumPostFavorite.post_id == ForumPost.id
        ).where(
            ForumPostFavorite.user_sid == user_sid,
            ForumPost.is_removed.is_(False),
        )
        total = self._db.execute(count_base).scalar() or 0
        rows = self._db.execute(
            base.offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        return int(total), [self._row_to_post(r) for r in rows]

    def user_comments(
        self, user_sid: str, page: int, page_size: int, viewer_sid: str = ""
    ) -> tuple[int, list[dict[str, Any]]]:
        count = (
            self._db.execute(
                select(func.count(ForumComment.id)).where(
                    ForumComment.author_sid == user_sid,
                    ForumComment.is_removed.is_(False),
                )
            ).scalar()
            or 0
        )
        rows = (
            self._db.execute(
                select(ForumComment)
                .where(ForumComment.author_sid == user_sid, ForumComment.is_removed.is_(False))
                .order_by(desc(ForumComment.created_at))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
.all()
        )
        return int(count), [self._row_to_comment(r) for r in rows]

    def user_stats(self, user_sid: str) -> dict[str, Any]:
        post_count = (
            self._db.execute(
                select(func.count(ForumPost.id)).where(
                    ForumPost.author_sid == user_sid,
                    ForumPost.is_removed.is_(False),
                )
            ).scalar()
            or 0
        )
        comment_count = (
            self._db.execute(
                select(func.count(ForumComment.id)).where(
                    ForumComment.author_sid == user_sid,
                    ForumComment.is_removed.is_(False),
                )
            ).scalar()
            or 0
        )
        favorite_count = (
            self._db.execute(
                select(func.count(ForumPostFavorite.id)).where(
                    ForumPostFavorite.user_sid == user_sid,
                )
            ).scalar()
            or 0
        )
        nickname = (
            self._db.execute(
                select(ForumPost.author_nickname)
                .where(
                    ForumPost.author_sid == user_sid,
                    ForumPost.is_removed.is_(False),
                )
                .order_by(desc(ForumPost.created_at))
                .limit(1)
            ).scalar()
            or (
                self._db.execute(
                    select(ForumComment.author_nickname)
                    .where(
                        ForumComment.author_sid == user_sid,
                        ForumComment.is_removed.is_(False),
                    )
                    .order_by(desc(ForumComment.created_at))
                    .limit(1)
                ).scalar()
                or "匿名同学"
            )
        )
        return {
            "post_count": int(post_count),
            "comment_count": int(comment_count),
            "favorite_count": int(favorite_count),
            "nickname": nickname,
        }

    def create_report(self, **kwargs: Any) -> dict[str, Any]:
        row = CommunityReport(**kwargs)
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return self._report_dict(row)

    def list_reports(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        stmt = select(CommunityReport)
        if status:
            stmt = stmt.where(CommunityReport.status == status)
        rows = (
            self._db.execute(stmt.order_by(CommunityReport.created_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [self._report_dict(row) for row in rows]

    def resolve_report(self, report_id: int, status: str, resolver: str) -> dict[str, Any]:
        from datetime import datetime

        row = self._db.get(CommunityReport, report_id)
        if row is None:
            raise NotFoundError("举报记录不存在", detail={"code": "REPORT_NOT_FOUND"})
        row.status = status
        row.resolver = resolver
        row.resolved_at = datetime.now()
        self._db.commit()
        self._db.refresh(row)
        return self._report_dict(row)

    def _report_dict(self, row: CommunityReport) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "target_type": row.target_type,
            "target_id": int(row.target_id),
            "reason": row.reason,
            "detail": row.detail or "",
            "status": row.status,
            "resolver": row.resolver,
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            "resolved_at": row.resolved_at.strftime("%Y-%m-%d %H:%M") if row.resolved_at else None,
        }


# ============================================================
#  In-Memory 实现（测试用）
# ============================================================


class InMemoryCommunityRepository:
    def __init__(self):
        self._posts: list[dict[str, Any]] = []
        self._comments: list[dict[str, Any]] = []
        self._likes: list[dict[str, Any]] = []
        self._comment_likes: list[dict[str, Any]] = []
        self._favorites: list[dict[str, Any]] = []
        self._reports: list[dict[str, Any]] = []
        self._post_seq = 0
        self._comment_seq = 0

    def create_post(self, **kwargs: Any) -> dict[str, Any]:
        self._post_seq += 1
        from datetime import datetime
        post = {
            "id": self._post_seq,
            "title": kwargs["title"],
            "content": kwargs["content"],
            "category": kwargs.get("category", "general"),
            "author_sid": kwargs["author_sid"],
            "author_nickname": kwargs.get("author_nickname", "匿名同学"),
            "like_count": 0,
            "comment_count": 0,
            "view_count": 0,
            "is_pinned": False,
            "is_removed": False,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }
        self._posts.append(post)
        return dict(post)

    def get_post(self, post_id: int) -> dict[str, Any] | None:
        for p in self._posts:
            if p["id"] == post_id and not p["is_removed"]:
                return dict(p)
        return None

    def list_posts(
        self,
        category: str | None,
        sort: str,
        page: int,
        page_size: int,
        keyword: str | None,
    ) -> tuple[int, list[dict[str, Any]]]:
        filtered = [p for p in self._posts if not p["is_removed"]]
        if category and category in ALLOWED_CATEGORIES:
            filtered = [p for p in filtered if p["category"] == category]
        if keyword:
            kw = keyword.lower()
            filtered = [p for p in filtered if kw in p["title"].lower() or kw in p["content"].lower()]

        def sort_key(p):
            pin = 1 if p["is_pinned"] else 0
            if sort == "hot":
                return (pin, p["like_count"] + p["comment_count"] * 2 + p["view_count"] // 10)
            elif sort == "most_commented":
                return (pin, p["comment_count"])
            return (pin, p["created_at"].timestamp())

        filtered.sort(key=sort_key, reverse=True)
        total = len(filtered)
        start = (page - 1) * page_size
        items = [dict(p) for p in filtered[start:start + page_size]]
        return total, items

    def increment_view(self, post_id: int) -> None:
        for p in self._posts:
            if p["id"] == post_id:
                p["view_count"] += 1
                return

    def delete_post(self, post_id: int, user_sid: str) -> None:
        for p in self._posts:
            if p["id"] == post_id:
                if p["author_sid"] != user_sid:
                    raise BadRequestError("只能删除自己的帖子", detail={"code": "NOT_AUTHOR"})
                p["is_removed"] = True
                return
        raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

    def update_post(self, post_id: int, user_sid: str, **changes: Any) -> dict[str, Any]:
        for post in self._posts:
            if post["id"] == post_id and not post["is_removed"]:
                if post["author_sid"] != user_sid:
                    raise BadRequestError("只能编辑自己的帖子", detail={"code": "NOT_AUTHOR"})
                for key in ("title", "content", "category"):
                    if key in changes and changes[key] is not None:
                        post[key] = changes[key]
                return dict(post)
        raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

    def admin_delete_post(self, post_id: int) -> None:
        for post in self._posts:
            if post["id"] == post_id:
                post["is_removed"] = True
                return
        raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

    def set_pinned(self, post_id: int, pinned: bool) -> None:
        for post in self._posts:
            if post["id"] == post_id and not post["is_removed"]:
                post["is_pinned"] = bool(pinned)
                return
        raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

    def create_comment(self, **kwargs: Any) -> dict[str, Any]:
        self._comment_seq += 1
        from datetime import datetime
        comment = {
            "id": self._comment_seq,
            "post_id": kwargs["post_id"],
            "content": kwargs["content"],
            "author_sid": kwargs["author_sid"],
            "author_nickname": kwargs.get("author_nickname", "匿名同学"),
            "like_count": 0,
            "reply_to_id": kwargs.get("reply_to_id"),
            "reply_to_nickname": kwargs.get("reply_to_nickname"),
            "is_removed": False,
            "created_at": datetime.now(),
        }
        self._comments.append(comment)
        # 同步计数
        for p in self._posts:
            if p["id"] == kwargs["post_id"]:
                p["comment_count"] += 1
                break
        return dict(comment)

    def list_comments(self, post_id: int) -> tuple[int, list[dict[str, Any]]]:
        items = [dict(c) for c in self._comments if c["post_id"] == post_id and not c["is_removed"]]
        items.sort(key=lambda c: c["created_at"])
        return len(items), items

    def delete_comment(self, comment_id: int, user_sid: str) -> None:
        for comment in self._comments:
            if comment["id"] == comment_id and not comment["is_removed"]:
                if comment["author_sid"] != user_sid:
                    raise BadRequestError("只能删除自己的评论", detail={"code": "NOT_AUTHOR"})
                comment["is_removed"] = True
                for post in self._posts:
                    if post["id"] == comment["post_id"]:
                        post["comment_count"] = max(0, post["comment_count"] - 1)
                return
        raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})

    def admin_delete_comment(self, comment_id: int) -> None:
        for comment in self._comments:
            if comment["id"] == comment_id:
                comment["is_removed"] = True
                for post in self._posts:
                    if post["id"] == comment["post_id"]:
                        post["comment_count"] = max(0, post["comment_count"] - 1)
                return
        raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})

    def toggle_like(self, post_id: int, user_sid: str) -> tuple[bool, int]:
        post = None
        for p in self._posts:
            if p["id"] == post_id and not p["is_removed"]:
                post = p
                break
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

        existing_idx = None
        for i, like in enumerate(self._likes):
            if like["post_id"] == post_id and like["user_sid"] == user_sid:
                existing_idx = i
                break

        from datetime import datetime
        if existing_idx is not None:
            del self._likes[existing_idx]
            post["like_count"] = max(0, post["like_count"] - 1)
            return False, post["like_count"]
        else:
            self._likes.append({"post_id": post_id, "user_sid": user_sid, "created_at": datetime.now()})
            post["like_count"] += 1
            return True, post["like_count"]

    def toggle_comment_like(self, comment_id: int, user_sid: str) -> tuple[bool, int]:
        comment = next(
            (item for item in self._comments if item["id"] == comment_id and not item["is_removed"]),
            None,
        )
        if comment is None:
            raise NotFoundError("评论不存在", detail={"code": "COMMENT_NOT_FOUND"})
        existing_idx = next(
            (
                idx
                for idx, item in enumerate(self._comment_likes)
                if item["comment_id"] == comment_id and item["user_sid"] == user_sid
            ),
            None,
        )
        if existing_idx is not None:
            del self._comment_likes[existing_idx]
            comment["like_count"] = max(0, comment["like_count"] - 1)
            return False, comment["like_count"]
        self._comment_likes.append({"comment_id": comment_id, "user_sid": user_sid})
        comment["like_count"] += 1
        return True, comment["like_count"]

    def toggle_favorite(self, post_id: int, user_sid: str) -> bool:
        post = None
        for p in self._posts:
            if p["id"] == post_id and not p["is_removed"]:
                post = p
                break
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})

        existing_idx = None
        for i, f in enumerate(self._favorites):
            if f["post_id"] == post_id and f["user_sid"] == user_sid:
                existing_idx = i
                break

        from datetime import datetime
        if existing_idx is not None:
            del self._favorites[existing_idx]
            return False
        else:
            self._favorites.append({"post_id": post_id, "user_sid": user_sid, "created_at": datetime.now()})
            return True

    def has_liked(self, post_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        return any(like["post_id"] == post_id and like["user_sid"] == user_sid for like in self._likes)

    def has_comment_liked(self, comment_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        return any(
            like["comment_id"] == comment_id and like["user_sid"] == user_sid
            for like in self._comment_likes
        )

    def has_favorited(self, post_id: int, user_sid: str) -> bool:
        if not user_sid:
            return False
        return any(f["post_id"] == post_id and f["user_sid"] == user_sid for f in self._favorites)

    def user_posts(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
        items = [dict(p) for p in self._posts if p["author_sid"] == user_sid and not p["is_removed"]]
        items.sort(key=lambda p: p["created_at"], reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return total, items[start:start + page_size]

    def user_favorites(self, user_sid: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
        fav_ids = {f["post_id"] for f in self._favorites if f["user_sid"] == user_sid}
        items = [dict(p) for p in self._posts if p["id"] in fav_ids and not p["is_removed"]]
        items.sort(key=lambda p: p["created_at"], reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return total, items[start:start + page_size]

    def user_comments(
        self, user_sid: str, page: int, page_size: int, viewer_sid: str = ""
    ) -> tuple[int, list[dict[str, Any]]]:
        items = [dict(c) for c in self._comments if c["author_sid"] == user_sid and not c["is_removed"]]
        items.sort(key=lambda c: c["created_at"], reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return total, items[start:start + page_size]

    def user_stats(self, user_sid: str) -> dict[str, Any]:
        post_count = sum(1 for p in self._posts if p["author_sid"] == user_sid and not p["is_removed"])
        comment_count = sum(1 for c in self._comments if c["author_sid"] == user_sid and not c["is_removed"])
        favorite_count = sum(1 for f in self._favorites if f["user_sid"] == user_sid)
        nickname = "匿名同学"
        for p in sorted(
            (p for p in self._posts if p["author_sid"] == user_sid and not p["is_removed"]),
            key=lambda p: p["created_at"],
            reverse=True,
        ):
            if p.get("author_nickname"):
                nickname = p["author_nickname"]
                break
        else:
            for c in sorted(
                (c for c in self._comments if c["author_sid"] == user_sid and not c["is_removed"]),
                key=lambda c: c["created_at"],
                reverse=True,
            ):
                if c.get("author_nickname"):
                    nickname = c["author_nickname"]
                    break
        return {
            "post_count": post_count,
            "comment_count": comment_count,
            "favorite_count": favorite_count,
            "nickname": nickname,
        }

    def create_report(self, **kwargs: Any) -> dict[str, Any]:
        from datetime import datetime

        row = {
            "id": len(self._reports) + 1,
            "target_type": kwargs["target_type"],
            "target_id": kwargs["target_id"],
            "reporter_sid": kwargs["reporter_sid"],
            "reason": kwargs["reason"],
            "detail": kwargs.get("detail", ""),
            "status": "pending",
            "resolver": None,
            "created_at": datetime.now(),
            "resolved_at": None,
        }
        self._reports.append(row)
        return dict(row)

    def list_reports(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._reports if not status or row["status"] == status]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    def resolve_report(self, report_id: int, status: str, resolver: str) -> dict[str, Any]:
        from datetime import datetime

        for row in self._reports:
            if row["id"] == report_id:
                row["status"] = status
                row["resolver"] = resolver
                row["resolved_at"] = datetime.now()
                return dict(row)
        raise NotFoundError("举报记录不存在", detail={"code": "REPORT_NOT_FOUND"})


# ============================================================
#  Service
# ============================================================


class CommunityService:
    def __init__(self, repo: CommunityRepository):
        self._repo = repo

    def _fmt_time(self, dt) -> str:
        if dt is None:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M")

    def _post_to_item(self, post: dict[str, Any], user_sid: str = "", with_content: bool = False) -> dict[str, Any]:
        summary = post["content"][:80] + ("…" if len(post["content"]) > 80 else "")
        cat = post["category"]
        item = {
            "post_id": post["id"],
            "title": post["title"],
            "category": cat,
            "category_label": FORUM_CATEGORIES.get(cat, cat),
            "author_nickname": post["author_nickname"],
            "author_sid": post["author_sid"],
            "like_count": post["like_count"],
            "comment_count": post["comment_count"],
            "view_count": post["view_count"],
            "is_pinned": post["is_pinned"],
            "is_liked": self._repo.has_liked(post["id"], user_sid),
            "is_favorited": self._repo.has_favorited(post["id"], user_sid),
            "created_at": self._fmt_time(post["created_at"]),
            "summary": summary,
        }
        if with_content:
            item["content"] = post["content"]
            item["updated_at"] = self._fmt_time(post["updated_at"])
        return item

    def _comment_to_item(self, comment: dict[str, Any], user_sid: str = "") -> dict[str, Any]:
        return {
            "comment_id": comment["id"],
            "post_id": comment["post_id"],
            "content": "[已删除]" if comment["is_removed"] else comment["content"],
            "author_nickname": comment["author_nickname"],
            "author_sid": comment["author_sid"],
            "like_count": comment["like_count"],
            "is_liked": self._repo.has_comment_liked(comment["id"], user_sid),
            "reply_to_id": comment.get("reply_to_id"),
            "reply_to_nickname": comment.get("reply_to_nickname"),
            "is_removed": comment["is_removed"],
            "created_at": self._fmt_time(comment["created_at"]),
        }

    def list_posts(
        self,
        category: str | None = None,
        sort: str = "latest",
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        user_sid: str = "",
    ) -> dict[str, Any]:
        total, posts = self._repo.list_posts(category, sort, page, page_size, keyword)
        items = [self._post_to_item(p, user_sid) for p in posts]
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
            "categories": FORUM_CATEGORIES,
        }

    def get_post_detail(self, post_id: int, user_sid: str = "") -> dict[str, Any]:
        post = self._repo.get_post(post_id)
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        self._repo.increment_view(post_id)
        post = self._repo.get_post(post_id)  # 刷新 view_count
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        return self._post_to_item(post, user_sid, with_content=True)

    def create_post(
        self,
        title: str,
        content: str,
        category: str,
        session_sid: str,
        nickname: str,
    ) -> dict[str, Any]:
        if not title or len(title.strip()) < 2:
            raise BadRequestError("标题至少 2 个字符", detail={"code": "TITLE_TOO_SHORT"})
        if len(title) > TITLE_MAX_LEN:
            raise BadRequestError(f"标题不超过 {TITLE_MAX_LEN} 字", detail={"code": "TITLE_TOO_LONG"})
        if not content or len(content.strip()) < 5:
            raise BadRequestError("正文至少 5 个字符", detail={"code": "CONTENT_TOO_SHORT"})
        if len(content) > CONTENT_MAX_LEN:
            raise BadRequestError(f"正文不超过 {CONTENT_MAX_LEN} 字", detail={"code": "CONTENT_TOO_LONG"})
        if category and category not in ALLOWED_CATEGORIES:
            category = "general"
        if not session_sid:
            raise BadRequestError("请先刷新页面获取会话 ID", detail={"code": "SESSION_REQUIRED"})
        nick = nickname.strip() or "匿名同学"
        if len(nick) > 32:
            nick = nick[:32]

        post = self._repo.create_post(
            title=title.strip(),
            content=content.strip(),
            category=category or "general",
            author_sid=session_sid,
            author_nickname=nick,
        )
        return {"post_id": post["id"]}

    def create_comment(
        self,
        post_id: int,
        content: str,
        session_sid: str,
        nickname: str,
        reply_to_id: int | None = None,
    ) -> dict[str, Any]:
        post = self._repo.get_post(post_id)
        if not post:
            raise NotFoundError("帖子不存在", detail={"code": "POST_NOT_FOUND"})
        if not content or not content.strip():
            raise BadRequestError("评论内容不能为空", detail={"code": "COMMENT_EMPTY"})
        if len(content) > COMMENT_MAX_LEN:
            raise BadRequestError(f"评论不超过 {COMMENT_MAX_LEN} 字", detail={"code": "COMMENT_TOO_LONG"})
        if not session_sid:
            raise BadRequestError("请先刷新页面获取会话 ID", detail={"code": "SESSION_REQUIRED"})

        reply_to_nickname = None
        if reply_to_id:
            total, comments = self._repo.list_comments(post_id)
            for c in comments:
                if c["id"] == reply_to_id:
                    reply_to_nickname = c["author_nickname"]
                    break

        nick = nickname.strip() or "匿名同学"
        if len(nick) > 32:
            nick = nick[:32]

        comment = self._repo.create_comment(
            post_id=post_id,
            content=content.strip(),
            author_sid=session_sid,
            author_nickname=nick,
            reply_to_id=reply_to_id,
            reply_to_nickname=reply_to_nickname,
        )
        return {"comment_id": comment["id"]}

    def list_comments(self, post_id: int, user_sid: str = "") -> dict[str, Any]:
        total, comments = self._repo.list_comments(post_id)
        items = [self._comment_to_item(c, user_sid) for c in comments]
        return {"total": total, "items": items}

    def toggle_like(self, post_id: int, user_sid: str) -> dict[str, Any]:
        if not user_sid:
            raise BadRequestError("请先刷新页面获取会话 ID", detail={"code": "SESSION_REQUIRED"})
        liked, count = self._repo.toggle_like(post_id, user_sid)
        return {"liked": liked, "like_count": count}

    def toggle_comment_like(self, comment_id: int, user_sid: str) -> dict[str, Any]:
        if not user_sid:
            raise UnauthorizedError("请先登录后再点赞", detail={"code": "AUTH_REQUIRED"})
        liked, count = self._repo.toggle_comment_like(comment_id, user_sid)
        return {"liked": liked, "like_count": count}

    def toggle_favorite(self, post_id: int, user_sid: str) -> dict[str, Any]:
        if not user_sid:
            raise BadRequestError("请先刷新页面获取会话 ID", detail={"code": "SESSION_REQUIRED"})
        favorited = self._repo.toggle_favorite(post_id, user_sid)
        return {"favorited": favorited}

    def delete_post(self, post_id: int, user_sid: str) -> None:
        if not user_sid:
            raise BadRequestError("请先刷新页面获取会话 ID", detail={"code": "SESSION_REQUIRED"})
        self._repo.delete_post(post_id, user_sid)

    def update_post(
        self,
        post_id: int,
        user_sid: str,
        *,
        title: str,
        content: str,
        category: str,
    ) -> dict[str, Any]:
        if not user_sid:
            raise BadRequestError("请先登录后再编辑", detail={"code": "AUTH_REQUIRED"})
        if not title.strip() or len(title.strip()) < 2:
            raise BadRequestError("标题至少 2 个字符", detail={"code": "TITLE_TOO_SHORT"})
        if len(title) > TITLE_MAX_LEN:
            raise BadRequestError(f"标题不超过 {TITLE_MAX_LEN} 字", detail={"code": "TITLE_TOO_LONG"})
        if not content.strip() or len(content.strip()) < 5:
            raise BadRequestError("正文至少 5 个字符", detail={"code": "CONTENT_TOO_SHORT"})
        if len(content) > CONTENT_MAX_LEN:
            raise BadRequestError(f"正文不超过 {CONTENT_MAX_LEN} 字", detail={"code": "CONTENT_TOO_LONG"})
        if category not in ALLOWED_CATEGORIES:
            category = "general"
        return self._repo.update_post(
            post_id,
            user_sid,
            title=title.strip(),
            content=content.strip(),
            category=category,
        )

    def admin_delete_post(self, post_id: int) -> None:
        self._repo.admin_delete_post(post_id)

    def delete_comment(self, comment_id: int, user_sid: str) -> None:
        if not user_sid:
            raise BadRequestError("请先登录后再操作", detail={"code": "AUTH_REQUIRED"})
        self._repo.delete_comment(comment_id, user_sid)

    def admin_delete_comment(self, comment_id: int) -> None:
        self._repo.admin_delete_comment(comment_id)

    def set_pinned(self, post_id: int, pinned: bool) -> None:
        self._repo.set_pinned(post_id, pinned)

    def create_report(
        self,
        *,
        target_type: str,
        target_id: int,
        reporter_sid: str,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        if not reporter_sid:
            raise BadRequestError("请先登录后再举报", detail={"code": "AUTH_REQUIRED"})
        return self._repo.create_report(
            target_type=target_type,
            target_id=target_id,
            reporter_sid=reporter_sid,
            reason=reason.strip()[:64],
            detail=detail.strip()[:1000],
        )

    def list_reports(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._repo.list_reports(status=status, limit=limit)

    def resolve_report(self, report_id: int, status: str, resolver: str) -> dict[str, Any]:
        return self._repo.resolve_report(report_id, status, resolver)

    # ---- 个人主页 ----

    def user_posts(self, user_sid: str, page: int = 1, page_size: int = 20, viewer_sid: str = "") -> dict[str, Any]:
        total, posts = self._repo.user_posts(user_sid, page, page_size)
        items = [self._post_to_item(p, viewer_sid) for p in posts]
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    def user_favorites(self, user_sid: str, page: int = 1, page_size: int = 20, viewer_sid: str = "") -> dict[str, Any]:
        total, posts = self._repo.user_favorites(user_sid, page, page_size)
        items = [self._post_to_item(p, viewer_sid) for p in posts]
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    def user_comments(
        self, user_sid: str, page: int = 1, page_size: int = 20, viewer_sid: str = ""
    ) -> dict[str, Any]:
        total, comments = self._repo.user_comments(user_sid, page, page_size)
        items = [self._comment_to_item(c, viewer_sid) for c in comments]
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    def user_stats(self, user_sid: str) -> dict[str, Any]:
        return self._repo.user_stats(user_sid)


# ============================================================
#  单例获取（服务注入模式，与 feedback/profile 一致）
# ============================================================

_instance: CommunityService | None = None
_test_instance: CommunityService | None = None


def get_community_service(db=None) -> CommunityService:
    """返回测试注入实例；未注入时要求调用方传入请求级数据库会话。"""
    if _instance is not None:
        return _instance
    if db is None:
        raise RuntimeError("get_community_service() 需要请求级 db 会话")
    return CommunityService(SqlCommunityRepository(db))


def set_community_service(svc: CommunityService | None) -> None:
    global _instance
    _instance = svc


def get_test_community_service() -> CommunityService:
    global _test_instance
    if _test_instance is None:
        repo = InMemoryCommunityRepository()
        _test_instance = CommunityService(repo)
    return _test_instance
