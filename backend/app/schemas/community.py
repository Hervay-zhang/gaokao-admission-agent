"""社区/论坛 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 板块分类（与社区定位匹配：志愿填报相关话题）
FORUM_CATEGORIES = {
    "general": "综合讨论",
    "volunteer": "志愿填报",
    "major": "专业选择",
    "school": "院校交流",
    "score": "分数位次",
    "experience": "经验分享",
    "parent": "家长交流",
}

ALLOWED_CATEGORIES = frozenset(FORUM_CATEGORIES.keys())

# 内容长度限制
TITLE_MAX_LEN = 120
CONTENT_MAX_LEN = 5000
COMMENT_MAX_LEN = 2000


# ============================================================
#  请求
# ============================================================


class PostCreateRequest(BaseModel):
    """发帖请求。"""

    title: str = Field(..., min_length=2, max_length=TITLE_MAX_LEN, description="标题")
    content: str = Field(..., min_length=5, max_length=CONTENT_MAX_LEN, description="正文")
    category: str = Field(default="general", max_length=32, description="板块")
    session_sid: str = Field(default="", max_length=64, description="匿名会话 ID")
    nickname: str = Field(default="匿名同学", max_length=32, description="发帖昵称")


class PostListQuery(BaseModel):
    """帖子列表查询参数。"""

    category: str | None = Field(default=None, max_length=32, description="按板块筛选")
    sort: str = Field(default="latest", description="排序：latest / hot / most_commented")
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")
    keyword: str | None = Field(default=None, max_length=60, description="搜索关键词")


class CommentCreateRequest(BaseModel):
    """发表评论请求。"""

    content: str = Field(..., min_length=1, max_length=COMMENT_MAX_LEN, description="评论内容")
    session_sid: str = Field(default="", max_length=64, description="匿名会话 ID")
    nickname: str = Field(default="匿名同学", max_length=32, description="评论者昵称")
    reply_to_id: int | None = Field(default=None, description="回复的评论 ID")


class LikeToggleRequest(BaseModel):
    """点赞/取消点赞请求。"""

    session_sid: str = Field(..., max_length=64, description="用户匿名会话 ID")


class FavoriteToggleRequest(BaseModel):
    """收藏/取消收藏请求。"""

    session_sid: str = Field(..., max_length=64, description="用户匿名会话 ID")


class PinnedUpdateRequest(BaseModel):
    """管理员置顶设置请求。"""

    pinned: bool = Field(..., description="是否置顶")


class PostUpdateRequest(BaseModel):
    """作者编辑帖子。"""

    title: str = Field(..., min_length=2, max_length=TITLE_MAX_LEN)
    content: str = Field(..., min_length=5, max_length=CONTENT_MAX_LEN)
    category: str = Field(default="general", max_length=32)


class CommunityReportCreateRequest(BaseModel):
    """举报帖子或评论。"""

    target_type: str = Field(..., pattern="^(post|comment)$")
    target_id: int = Field(..., ge=1)
    reason: str = Field(..., min_length=2, max_length=64)
    detail: str = Field(default="", max_length=1000)


class CommunityReportResolveRequest(BaseModel):
    status: str = Field(..., pattern="^(resolved|dismissed)$")


# ============================================================
#  响应
# ============================================================


class PostItem(BaseModel):
    """帖子列表项。"""

    post_id: int
    title: str
    category: str
    category_label: str
    author_nickname: str
    author_sid: str
    like_count: int
    comment_count: int
    view_count: int
    is_pinned: bool
    is_liked: bool = False
    is_favorited: bool = False
    created_at: str
    summary: str = ""


class PostDetail(PostItem):
    """帖子详情。"""

    content: str
    updated_at: str


class PostListResponse(BaseModel):
    """帖子列表响应。"""

    total: int
    page: int
    page_size: int
    items: list[PostItem]
    categories: dict[str, str] = {}


class CommentItem(BaseModel):
    """评论项。"""

    comment_id: int
    post_id: int
    content: str
    author_nickname: str
    author_sid: str
    like_count: int
    is_liked: bool = False
    reply_to_id: int | None = None
    reply_to_nickname: str | None = None
    is_removed: bool = False
    created_at: str


class CommentListResponse(BaseModel):
    """评论列表响应。"""

    total: int
    items: list[CommentItem]


class PostCreateResponse(BaseModel):
    """发帖回执。"""

    ok: bool = True
    post_id: int


class CommentCreateResponse(BaseModel):
    """评论回执。"""

    ok: bool = True
    comment_id: int


class LikeToggleResponse(BaseModel):
    """点赞切换回执。"""

    ok: bool = True
    liked: bool
    like_count: int


class FavoriteToggleResponse(BaseModel):
    """收藏切换回执。"""

    ok: bool = True
    favorited: bool


class PinnedUpdateResponse(BaseModel):
    """置顶切换回执。"""

    ok: bool = True
    pinned: bool


class CommunityReportItem(BaseModel):
    id: int
    target_type: str
    target_id: int
    reason: str
    detail: str = ""
    status: str
    resolver: str | None = None
    created_at: str
    resolved_at: str | None = None


class CommunityReportListResponse(BaseModel):
    total: int
    items: list[CommunityReportItem]
