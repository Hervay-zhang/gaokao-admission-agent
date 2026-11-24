"""社区/论坛 API 测试（In-Memory 仓储）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.community import (
    CommunityService,
    InMemoryCommunityRepository,
    set_community_service,
)


@pytest.fixture(autouse=True)
def _setup():
    repo = InMemoryCommunityRepository()
    svc = CommunityService(repo)
    set_community_service(svc)
    # 种子数据
    svc.create_post("经验分享：580分如何捡漏一线城市双一流", "正文内容正文内容正文内容正文内容", "experience", "user_001", "学长甲")
    svc.create_post("志愿填报顺序有什么讲究？", "求问大佬们冲稳保怎么安排", "volunteer", "user_002", "迷茫高三")
    svc.create_post("计算机专业就业怎么样", "想报计算机但怕卷", "major", "user_003", "犹豫的同学")
    yield
    set_community_service(None)


client = TestClient(app)
SID = "test_user_999"
HEADERS = {"X-Session-Id": SID}


class TestPostList:
    def test_list_default(self):
        r = client.get("/api/community/posts")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert "categories" in data
        assert "general" in data["categories"]

    def test_list_by_category(self):
        r = client.get("/api/community/posts?category=volunteer")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["category"] == "volunteer"

    def test_list_pagination(self):
        r = client.get("/api/community/posts?page=1&page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_list_keyword(self):
        r = client.get("/api/community/posts?keyword=计算机")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1

    def test_list_invalid_category(self):
        r = client.get("/api/community/posts?category=nonexistent")
        assert r.status_code == 200
        assert r.json()["total"] == 3  # 非法 category 不过滤


class TestPostCreate:
    def test_create_success(self):
        r = client.post("/api/community/posts", json={
            "title": "新帖子标题测试",
            "content": "新帖子正文内容测试内容测试",
            "category": "general",
            "session_sid": "user_new",
            "nickname": "新用户",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["post_id"] > 0

    def test_create_title_too_short(self):
        # 全空格标题：业务层 strip 后不足 2 字，返回 400
        r = client.post("/api/community/posts", json={
            "title": "  ",
            "content": "足够长的正文内容测试",
            "category": "general",
            "session_sid": "user_new",
        })
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "TITLE_TOO_SHORT"

    def test_create_no_session(self):
        r = client.post("/api/community/posts", json={
            "title": "正常标题",
            "content": "正常正文内容",
            "category": "general",
            "session_sid": "",
        })
        # 未登录是认证问题 → 401（前端据此跳登录页），不是 400
        assert r.status_code == 401

    def test_production_requires_login(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "app_env", "production")
        r = client.post("/api/community/posts", json={
            "title": "生产环境发帖",
            "content": "未登录不允许直接发帖",
            "category": "general",
            "session_sid": "spoofed",
        })
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "AUTH_REQUIRED"


class TestPostDetail:
    def test_detail_success(self):
        r = client.get("/api/community/posts/1", headers=HEADERS)
        assert r.status_code == 200
        data = r.json()
        assert data["post_id"] == 1
        assert "content" in data
        assert data["view_count"] >= 1  # 浏览自增

    def test_detail_not_found(self):
        r = client.get("/api/community/posts/99999")
        assert r.status_code == 404


class TestLikeToggle:
    def test_like_toggle(self):
        # 第一次点赞
        r = client.post("/api/community/posts/1/like", json={"session_sid": SID}, headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["liked"] is True
        assert r.json()["like_count"] == 1

        # 第二次取消
        r2 = client.post("/api/community/posts/1/like", json={"session_sid": SID}, headers=HEADERS)
        assert r2.json()["liked"] is False
        assert r2.json()["like_count"] == 0

    def test_like_not_found(self):
        r = client.post("/api/community/posts/9999/like", json={"session_sid": SID})
        assert r.status_code == 404


class TestFavoriteToggle:
    def test_favorite_toggle(self):
        r = client.post("/api/community/posts/1/favorite", json={"session_sid": SID}, headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["favorited"] is True

        r2 = client.post("/api/community/posts/1/favorite", json={"session_sid": SID}, headers=HEADERS)
        assert r2.json()["favorited"] is False


class TestComment:
    def test_create_comment(self):
        r = client.post("/api/community/posts/1/comments", json={
            "content": "写得太好了！很有帮助",
            "session_sid": SID,
            "nickname": "测试用户",
        }, headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["comment_id"] > 0

    def test_list_comments(self):
        # 先发两条
        client.post("/api/community/posts/1/comments", json={
            "content": "第一条评论",
            "session_sid": "u1",
            "nickname": "A",
        })
        client.post("/api/community/posts/1/comments", json={
            "content": "第二条评论",
            "session_sid": "u2",
            "nickname": "B",
        })
        r = client.get("/api/community/posts/1/comments")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert data["items"][0]["content"] == "第一条评论"

    def test_comment_empty(self):
        # 全空格：业务层 strip 后视为空，返回 400
        r = client.post("/api/community/posts/1/comments", json={
            "content": "   ",
            "session_sid": SID,
        })
        assert r.status_code == 400


class TestDeletePost:
    def test_delete_by_author(self):
        r = client.delete("/api/community/posts/1", headers={"X-Session-Id": "user_001"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 再访问应该 404
        r2 = client.get("/api/community/posts/1")
        assert r2.status_code == 404

    def test_delete_not_author(self):
        r = client.delete("/api/community/posts/1", headers={"X-Session-Id": "other_user"})
        assert r.status_code == 400


class TestAdminPin:
    def test_admin_can_pin_and_unpin(self):
        headers = {"X-Admin-Demo-Role": "admin"}
        pinned = client.put("/api/community/posts/1/pin", json={"pinned": True}, headers=headers)
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True
        first = client.get("/api/community/posts").json()["items"][0]
        assert first["post_id"] == 1 and first["is_pinned"] is True

        unpinned = client.put("/api/community/posts/1/pin", json={"pinned": False}, headers=headers)
        assert unpinned.status_code == 200
        assert unpinned.json()["pinned"] is False

    def test_non_admin_cannot_pin(self):
        r = client.put("/api/community/posts/1/pin", json={"pinned": True})
        assert r.status_code == 404


class TestUserProfile:
    def test_user_posts(self):
        r = client.get("/api/community/user/user_001/posts")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1

    def test_user_stats(self):
        client.post("/api/community/posts/1/like", json={"session_sid": "u_stats"}, headers={"X-Session-Id": "u_stats"})
        client.post("/api/community/posts/1/favorite", json={"session_sid": "u_stats"})
        client.post("/api/community/posts/1/comments", json={
            "content": "一条评论",
            "session_sid": "u_stats",
            "nickname": "统计测试",
        })
        r = client.get("/api/community/user/u_stats/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["comment_count"] == 1
        assert data["favorite_count"] == 1
        assert data["post_count"] == 0

    def test_user_favorites(self):
        client.post("/api/community/posts/1/favorite", json={"session_sid": "u_fav"})
        client.post("/api/community/posts/2/favorite", json={"session_sid": "u_fav"})
        r = client.get("/api/community/user/u_fav/favorites")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_user_comments(self):
        client.post("/api/community/posts/1/comments", json={
            "content": "我的评论",
            "session_sid": "u_cmt",
            "nickname": "评论用户",
        })
        r = client.get("/api/community/user/u_cmt/comments")
        assert r.status_code == 200
        assert r.json()["total"] == 1


class TestCategories:
    def test_list_categories(self):
        r = client.get("/api/community/categories")
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert "volunteer" in cats
        assert "major" in cats
        assert len(cats) >= 5


class TestCommentLikeAndModeration:
    def test_comment_like_and_report_flow(self):
        created = client.post(
            "/api/community/posts/1/comments",
            json={
                "content": "待审核评论",
                "session_sid": SID,
                "nickname": "测试用户",
            },
        )
        assert created.status_code == 200
        comment_id = created.json()["comment_id"]

        liked = client.post(
            f"/api/community/comments/{comment_id}/like",
            json={"session_sid": SID},
        )
        assert liked.status_code == 200
        assert liked.json()["liked"] is True

        reported = client.post(
            "/api/community/reports",
            json={
                "target_type": "comment",
                "target_id": comment_id,
                "reason": "其他",
                "detail": "测试举报",
            },
            headers=HEADERS,
        )
        assert reported.status_code == 200
        report_id = reported.json()["id"]

        admin_headers = {"X-Admin-Demo-Role": "admin"}
        listed = client.get("/api/community/admin/reports?status=pending", headers=admin_headers)
        assert listed.status_code == 200
        assert any(item["id"] == report_id for item in listed.json()["items"])

        resolved = client.patch(
            f"/api/community/admin/reports/{report_id}",
            json={"status": "dismissed"},
            headers=admin_headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "dismissed"

        deleted = client.delete(f"/api/community/admin/comments/{comment_id}", headers=admin_headers)
        assert deleted.status_code == 200
