/**
 * pages/userProfile.js —— 用户个人主页
 *
 * 左：用户信息卡 + 统计数字
 * 中：标签切换（发帖 / 收藏 / 评论）
 * 右：对应内容
 */

import { h, toast, formatNumber } from "../ui.js";
import { icon } from "../components/Icon.js";
import { api } from "../api.js";
import { getState, ensureSessionId } from "../state.js";
import { navigate } from "../router.js";

const TABS = [
  { key: "posts", label: "发布的帖子" },
  { key: "favorites", label: "收藏的帖子" },
  { key: "comments", label: "评论记录" },
];

export default async function userProfile(mount, { param } = {}) {
  const targetSid = param;
  const mySid = ensureSessionId();
  if (typeof window !== "undefined") window.__sessionId = mySid;

  const isMe = targetSid === mySid;
  let activeTab = "posts";
  let stats = { post_count: 0, comment_count: 0, favorite_count: 0 };
  let listData = { total: 0, items: [] };
  let loading = true;
  let currentPage = 1;

const profile = getState().profile;

  const render = () => {
    const displayName = isMe
      ? (profile.nickname || "我")
      : (stats.nickname || "匿名同学");
    mount.replaceChildren(
      h("div", { class: "container user-profile" }, [
        // 用户信息头部
        h("div", { class: "user-profile__header" }, [
          h("div", { class: "user-profile__avatar" }, displayName.slice(0, 1)),
          h("div", { class: "user-profile__info" }, [
            h("h1", { class: "user-profile__name" },
              isMe ? (profile.nickname || "我的主页") : displayName),
            h("div", { class: "user-profile__subtitle" },
              isMe ? "这是你的个人主页" : "TA的个人主页"),
            h("div", { class: "user-profile__stats-row" }, [
              h("div", { class: "user-profile__stat" }, [
                h("span", { class: "user-profile__stat-num" }, formatNumber(stats.post_count)),
                h("span", { class: "user-profile__stat-label" }, "帖子"),
              ]),
              h("div", { class: "user-profile__stat" }, [
                h("span", { class: "user-profile__stat-num" }, formatNumber(stats.comment_count)),
                h("span", { class: "user-profile__stat-label" }, "评论"),
              ]),
              h("div", { class: "user-profile__stat" }, [
                h("span", { class: "user-profile__stat-num" }, formatNumber(stats.favorite_count)),
                h("span", { class: "user-profile__stat-label" }, "收藏"),
              ]),
            ]),
          ]),
          isMe ? h("a", {
            class: "btn btn--ghost btn--sm",
            href: "#/me",
          }, "编辑资料") : null,
        ]),

        // 标签栏
        h("div", { class: "profile-tabs" },
          TABS.map((t) =>
            h("button", {
              class: `profile-tabs__item${activeTab === t.key ? " is-active" : ""}`,
              onclick: () => {
                activeTab = t.key;
                currentPage = 1;
                loadList();
              },
            }, t.label)
          )
        ),

        // 内容区
        buildContent(),
      ])
    );
  };

  function buildContent() {
    if (loading) {
      return h("div", { class: "profile-content" },
        Array.from({ length: 5 }, () =>
          h("div", { class: "post-card post-card--skeleton" }, [
            h("div", { class: "post-card__skeleton-title" }),
            h("div", { class: "post-card__skeleton-meta" }),
          ])
        )
      );
    }

    if (listData.total === 0 || !listData.items.length) {
      const emptyText = {
        posts: "还没有发布过帖子",
        favorites: "还没有收藏任何帖子",
        comments: "还没有发表过评论",
      }[activeTab] || "暂无数据";

      return h("div", { class: "profile-content" },
        h("div", { class: "empty-state" }, [
          h("div", { class: "empty-state__icon" }, "◇"),
          h("div", { class: "empty-state__title" }, emptyText),
          h("div", { class: "empty-state__desc" },
            activeTab === "posts" ? "去社区分享你的想法吧～" : "看看有什么感兴趣的内容"),
          activeTab === "posts"
            ? h("a", { class: "btn btn--primary", href: "#/community" }, "去社区看看")
            : null,
        ])
      );
    }

    if (activeTab === "comments") {
      return h("div", { class: "profile-content comment-list" },
        listData.items.map((c) =>
          h("div", { class: "comment-item comment-item--profile" }, [
            h("div", { class: "comment-item__body" }, [
              h("div", { class: "comment-item__header" }, [
                h("a", {
                  class: "comment-item__post-link",
                  href: `#/community/${c.post_id}`,
                  onclick: (e) => { e.preventDefault(); navigate(`/community/${c.post_id}`); },
                }, `帖子 #${c.post_id}`),
                h("span", { class: "comment-item__time" }, c.created_at),
              ]),
              h("div", { class: "comment-item__content" }, c.is_removed ? "[已删除]" : c.content),
            ]),
          ])
        )
      );
    }

    return h("div", { class: "profile-content post-list" },
      listData.items.map((p) => buildPostCard(p))
    );
  }

  function buildPostCard(post) {
    return h("article", {
      class: `post-card${post.is_pinned ? " is-pinned" : ""}`,
      onclick: () => navigate(`/community/${post.post_id}`),
    }, [
      h("div", { class: "post-card__cat" }, post.category_label || ""),
      h("h3", { class: "post-card__title" }, post.title),
      h("p", { class: "post-card__summary" }, post.summary),
      h("div", { class: "post-card__footer" }, [
        h("span", { class: "post-card__time" }, post.created_at),
        h("span", { class: "post-card__stats" }, [
          h("span", { class: "icon-inline", title: "浏览" }, [
            h("i", { html: icon("eye", { size: 14 }) }), formatNumber(post.view_count || 0),
          ]),
          h("span", { class: "icon-inline", title: "评论" }, [
            h("i", { html: icon("chat", { size: 14 }) }), formatNumber(post.comment_count || 0),
          ]),
          h("span", { class: "icon-inline", title: "点赞" }, [
            h("i", { html: icon("heart", { size: 14 }) }), formatNumber(post.like_count || 0),
          ]),
        ]),
      ]),
    ]);
  }

  async function loadStats() {
    try {
      stats = await api.userStats(targetSid);
    } catch (e) {
      stats = { post_count: 0, comment_count: 0, favorite_count: 0 };
    }
  }

  async function loadList() {
    loading = true;
    render();
    try {
      if (activeTab === "posts") {
        listData = await api.userPosts(targetSid, { page: currentPage, page_size: 20 });
      } else if (activeTab === "favorites") {
        if (!isMe) {
          listData = { total: 0, items: [] };
        } else {
          listData = await api.userFavorites(targetSid, { page: currentPage, page_size: 20 });
        }
      } else if (activeTab === "comments") {
        listData = await api.userComments(targetSid, { page: currentPage, page_size: 20 });
      }
    } catch (e) {
      listData = { total: 0, items: [] };
    } finally {
      loading = false;
      render();
    }
  }

  // 初始化
  loading = true;
  render();
  await loadStats();
  await loadList();
}
