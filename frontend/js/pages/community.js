/**
 * pages/community.js —— 社区首页（帖子列表）
 *
 * 结构：顶部板块标签 + 排序/搜索 + 帖子卡片流 + 发帖按钮
 */

import { h, toast, formatNumber } from "../ui.js";
import { icon } from "../components/Icon.js";
import { api } from "../api.js";
import { getState, ensureSessionId, ensureNickname } from "../state.js";
import { setQuery, parseQuery, navigate } from "../router.js";
import { getCurrentUser, isAdmin } from "../auth.js";

const SORTS = [
  { key: "latest", label: "最新" },
  { key: "hot", label: "热门" },
  { key: "most_commented", label: "热议" },
];

export default async function community(mount, { query } = {}) {
  const state = getState();
  const sessionId = ensureSessionId();
  if (typeof window !== "undefined") window.__sessionId = sessionId;

  const q = parseQuery();
  let activeCategory = q.category || "all";
  let activeSort = q.sort || "latest";
  let keyword = q.keyword || "";
  let currentPage = Number(q.page) || 1;
  let postsData = null;
  let categories = {};
  let loading = true;

  const render = () => {
    mount.replaceChildren(
      h("div", { class: "container community-page" }, [
        // 页头
        h("section", { class: "community-hero" }, [
          h("div", { class: "community-header" }, [
            h("div", { class: "community-header__text" }, [
              h("span", { class: "community-header__eyebrow" }, "真实经验 · 匿名交流"),
              h("h1", { class: "community-header__title" }, "升学社区"),
              h("p", { class: "community-header__subtitle" },
                "与家长同学交流志愿填报经验，分享你的故事与困惑"),
            ]),
            h("button", {
              class: "btn btn--primary community-header__btn",
              onclick: openCreateModal,
            }, "＋ 发布帖子"),
          ]),
          h("div", { class: "community-hero__stats" }, [
            h("div", {}, [h("strong", {}, String(postsData?.total ?? "—")), h("span", {}, "社区主题")]),
            h("div", {}, [h("strong", {}, String(Object.keys(categories).length || "—")), h("span", {}, "讨论板块")]),
            h("div", {}, [h("strong", {}, "匿名"), h("span", {}, "隐私保护")]),
          ]),
        ]),

        // 板块标签 + 搜索 + 排序
        h("div", { class: "community-toolbar community-toolbar--panel" }, [
          buildCategoryTabs(),
          h("div", { class: "community-toolbar__right" }, [
            buildSearchBox(),
            buildSortTabs(),
          ]),
        ]),

        // 帖子列表
        buildPostList(),

        // 分页
        postsData && postsData.total > postsData.page_size ? buildPagination() : null,
      ])
    );
  };

  function buildCategoryTabs() {
    const tabs = [{ key: "all", label: "全部" }, ...Object.entries(categories).map(([k, v]) => ({ key: k, label: v }))];
    return h("div", { class: "category-tabs" },
      tabs.map((t) =>
        h("button", {
          class: `category-tabs__item${activeCategory === t.key ? " is-active" : ""}`,
          onclick: () => {
            activeCategory = t.key;
            currentPage = 1;
            updateQuery();
            loadPosts();
          },
        }, t.label)
      )
    );
  }

  function buildSortTabs() {
    return h("div", { class: "sort-tabs" },
      SORTS.map((s) =>
        h("button", {
          class: `sort-tabs__item${activeSort === s.key ? " is-active" : ""}`,
          onclick: () => {
            activeSort = s.key;
            currentPage = 1;
            updateQuery();
            loadPosts();
          },
        }, s.label)
      )
    );
  }

  function buildSearchBox() {
    const input = h("input", {
      class: "search-input",
      type: "text",
      placeholder: "搜索帖子…",
      value: keyword,
      onkeydown: (e) => {
        if (e.key === "Enter") {
          keyword = input.value.trim();
          currentPage = 1;
          updateQuery();
          loadPosts();
        }
      },
    });
    return h("div", { class: "search-box" }, [
      input,
      h("span", { class: "search-box__icon" }, "⌕"),
    ]);
  }

  function buildPostList() {
    if (loading) {
      return h("div", { class: "post-list-skeleton" },
        Array.from({ length: 5 }, (_, i) =>
          h("div", { class: "post-card post-card--skeleton" }, [
            h("div", { class: "post-card__skeleton-title" }),
            h("div", { class: "post-card__skeleton-meta" }),
          ])
        )
      );
    }

    if (!postsData || postsData.items.length === 0) {
      return h("div", { class: "empty-state" }, [
        h("div", { class: "empty-state__icon" }, "◇"),
        h("div", { class: "empty-state__title" }, "暂无帖子"),
        h("div", { class: "empty-state__desc" },
          keyword ? "没有找到相关帖子，换个关键词试试" : "成为第一个发帖的人吧"),
      ]);
    }

    return h("div", { class: "post-list" },
      postsData.items.map((p) => buildPostCard(p))
    );
  }

  function buildPostCard(post) {
    return h("article", {
      class: `post-card${post.is_pinned ? " is-pinned" : ""}`,
      onclick: () => navigate(`/community/${post.post_id}`),
    }, [
      h("div", { class: "post-card__topline" }, [
        h("div", { class: "post-card__badges" }, [
          post.is_pinned ? h("span", { class: "post-card__pin" }, "置顶") : null,
          h("span", { class: "post-card__cat" }, post.category_label),
          isAdmin()
            ? h("button", {
                class: "post-card__admin-pin",
                type: "button",
                onclick: (e) => {
                  e.stopPropagation();
                  handlePin(post);
                },
              }, post.is_pinned ? "取消置顶" : "置顶")
            : null,
        ]),
        h("span", { class: "post-card__time" }, post.created_at),
      ]),
      h("h3", { class: "post-card__title" }, post.title),
      h("p", { class: "post-card__summary" }, post.summary),
      h("div", { class: "post-card__footer" }, [
        h("a", {
          class: "post-card__author",
          onclick: (e) => { e.stopPropagation(); navigate(`/user/${post.author_sid}`); },
          href: `#/user/${post.author_sid}`,
        }, [
          h("span", { class: "post-card__avatar" }, post.author_nickname.slice(0, 1)),
          h("span", {}, post.author_nickname),
        ]),
        h("span", { class: "post-card__stats" }, [
          h("span", { title: "浏览", class: "icon-inline" }, [
            h("i", { html: icon("eye", { size: 14 }) }), formatNumber(post.view_count),
          ]),
          h("span", { title: "评论", class: "icon-inline" }, [
            h("i", { html: icon("chat", { size: 14 }) }), formatNumber(post.comment_count),
          ]),
          h("span", {
            class: `icon-inline${post.is_liked ? " is-liked" : ""}`,
            title: "点赞",
            onclick: (e) => { e.stopPropagation(); handleLike(post); },
          }, [
            h("i", { html: icon("heart", { size: 14 }) }), formatNumber(post.like_count),
          ]),
        ]),
      ]),
    ]);
  }

  function buildPagination() {
    const total = postsData.total;
    const pageSize = postsData.page_size;
    const totalPages = Math.ceil(total / pageSize);
    const pages = [];
    for (let i = 1; i <= Math.min(totalPages, 7); i++) {
      if (totalPages > 7) {
        if (currentPage <= 4) {
          if (i === 7) { pages.push("…"); pages.push(totalPages); break; }
          pages.push(i);
        } else if (currentPage >= totalPages - 3) {
          if (i === 1) { pages.push(1); pages.push("…"); }
          else pages.push(totalPages - 7 + i);
        } else {
          if (i === 1) { pages.push(1); pages.push("…"); }
          else if (i === 7) { pages.push("…"); pages.push(totalPages); break; }
          else pages.push(currentPage - 4 + i);
        }
      } else {
        pages.push(i);
      }
    }

    return h("div", { class: "pagination" }, [
      h("button", {
        class: "pagination__btn",
        disabled: currentPage === 1,
        onclick: () => goToPage(currentPage - 1),
      }, "‹"),
      ...pages.map((p) =>
        p === "…"
          ? h("span", { class: "pagination__ellipsis" }, "…")
          : h("button", {
            class: `pagination__btn${p === currentPage ? " is-active" : ""}`,
            onclick: () => goToPage(p),
          }, String(p))
      ),
      h("button", {
        class: "pagination__btn",
        disabled: currentPage === totalPages,
        onclick: () => goToPage(currentPage + 1),
      }, "›"),
    ]);
  }

  async function handleLike(post) {
    if (!getCurrentUser()) {
      toast("请先登录后再点赞", "info");
      navigate("/login?redirect=/community");
      return;
    }
    try {
      const result = await api.toggleLike(post.post_id, sessionId);
      post.is_liked = result.liked;
      post.like_count = result.like_count;
      render();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function handlePin(post) {
    try {
      const result = await api.setPostPinned(post.post_id, !post.is_pinned);
      post.is_pinned = result.pinned;
      toast(result.pinned ? "已置顶" : "已取消置顶", "success");
      await loadPosts();
    } catch (e) {
      toast(`置顶设置失败：${e.message}`, "error");
    }
  }

  function goToPage(p) {
    currentPage = p;
    updateQuery();
    loadPosts();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateQuery() {
    const params = {};
    if (activeCategory !== "all") params.category = activeCategory;
    if (activeSort !== "latest") params.sort = activeSort;
    if (keyword) params.keyword = keyword;
    if (currentPage > 1) params.page = currentPage;
    setQuery(params);
  }

  async function loadPosts() {
    loading = true;
    render();
    try {
      const params = {
        page: currentPage,
        page_size: 20,
        sort: activeSort,
      };
      if (activeCategory !== "all") params.category = activeCategory;
      if (keyword) params.keyword = keyword;
      postsData = await api.listPosts(params);
    } catch (e) {
      toast("加载帖子失败：" + e.message, "error");
      postsData = { total: 0, items: [], page: currentPage, page_size: 20 };
    } finally {
      loading = false;
      render();
    }
  }

  async function loadCategories() {
    try {
      const data = await api.forumCategories();
      categories = data.categories || {};
    } catch (e) {
      categories = {
        general: "综合讨论", volunteer: "志愿填报", major: "专业选择",
        school: "院校交流", experience: "经验分享",
      };
    }
  }

  // ---- 发帖模态框 ----

  function openCreateModal() {
    if (!getCurrentUser()) {
      toast("请先登录后再发帖", "info");
      navigate("/login?redirect=/community");
      return;
    }
    const nickname = ensureNickname();

    const backdrop = h("div", { class: "modal-backdrop" });
    const modal = h("div", { class: "modal modal--lg" });

    const titleInput = h("input", { class: "field-input", type: "text", placeholder: "标题（2-120字）", maxlength: 120 });
    const contentInput = h("textarea", { class: "field-textarea", placeholder: "分享你的想法、经验或问题（5-5000字）", rows: 10, maxlength: 5000 });
    const catSelect = h("select", { class: "field-select" },
      Object.entries(categories).map(([k, v]) =>
        h("option", { value: k }, v)
      )
    );

    const submit = async () => {
      const title = titleInput.value.trim();
      const content = contentInput.value.trim();
      const category = catSelect.value;

      if (title.length < 2) { toast("标题至少 2 个字", "error"); return; }
      if (content.length < 5) { toast("正文至少 5 个字", "error"); return; }

      try {
        const result = await api.createPost({
          title, content, category,
          session_sid: sessionId,
          nickname,
        });
        toast("发布成功", "success");
        close();
        navigate(`/community/${result.post_id}`);
      } catch (e) {
        toast("发布失败：" + e.message, "error");
      }
    };

    const close = () => { backdrop.remove(); modal.remove(); };

    modal.replaceChildren(
      h("div", { class: "modal__header" }, [
        h("h3", { class: "modal__title" }, "发布新帖"),
        h("button", { class: "modal__close", onclick: close }, "×"),
      ]),
      h("div", { class: "modal__body" }, [
        h("div", { class: "field-group" }, [
          h("label", { class: "field-label" }, "板块"),
          catSelect,
        ]),
        h("div", { class: "field-group" }, [
          h("label", { class: "field-label" }, "标题"),
          titleInput,
        ]),
        h("div", { class: "field-group" }, [
          h("label", { class: "field-label" }, "正文"),
          contentInput,
        ]),
      ]),
      h("div", { class: "modal__footer" }, [
        h("button", { class: "btn btn--ghost", onclick: close }, "取消"),
        h("button", { class: "btn btn--primary", onclick: submit }, "发布"),
      ])
    );

    document.body.appendChild(backdrop);
    document.body.appendChild(modal);
  }

  // 初始化
  await loadCategories();
  await loadPosts();
}
