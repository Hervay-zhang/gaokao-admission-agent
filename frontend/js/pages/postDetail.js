/**
 * pages/postDetail.js —— 帖子详情页
 *
 * 帖子内容 + 点赞/收藏 + 评论列表 + 评论输入
 */

import { h, toast, formatNumber } from "../ui.js";
import { icon } from "../components/Icon.js";
import { api } from "../api.js";
import { getState, ensureSessionId, ensureNickname } from "../state.js";
import { navigate } from "../router.js";
import { getAuthToken, getCurrentUser, isAdmin } from "../auth.js";

export default async function postDetail(mount, { param } = {}) {
  const postId = Number(param);
  const sessionId = ensureSessionId();
  if (typeof window !== "undefined") window.__sessionId = sessionId;

  let post = null;
  let comments = [];
  let loading = true;
  let commentText = "";
  let commentSubmitBtn = null;

  const render = () => {
    if (loading) {
      mount.replaceChildren(
        h("div", { class: "container post-detail" }, [
          h("div", { class: "post-detail__skeleton" }, [
            h("div", { class: "skeleton-line skeleton-line--lg" }),
            h("div", { class: "skeleton-line" }),
            h("div", { class: "skeleton-line" }),
            h("div", { class: "skeleton-line skeleton-line--short" }),
          ]),
        ])
      );
      return;
    }

    if (!post) {
      mount.replaceChildren(
        h("div", { class: "container" },
          h("div", { class: "empty-state" }, [
            h("div", { class: "empty-state__icon" }, "?"),
            h("div", { class: "empty-state__title" }, "帖子不存在"),
            h("div", { class: "empty-state__desc" }, "该帖子可能已被删除"),
            h("a", { class: "btn btn--primary", href: "#/community" }, "返回社区"),
          ])
        )
      );
      return;
    }

    mount.replaceChildren(
      h("div", { class: "container post-detail" }, [
        // 返回 + 面包屑
        h("div", { class: "post-detail__nav" }, [
          h("a", { class: "post-detail__back", href: "#/community" }, "← 返回社区"),
          h("span", { class: "post-detail__crumb" }, post.category_label),
          isAdmin()
            ? h("button", {
                class: "post-detail__admin-pin",
                type: "button",
                onclick: handlePin,
              }, post.is_pinned ? "取消置顶" : "设为置顶")
            : null,
        ]),

        h("div", { class: "post-detail__layout" }, [
          // 帖子主体
          h("article", { class: "post-detail__content" }, [
          post.is_pinned ? h("span", { class: "post-card__pin" }, "置顶") : null,
          h("h1", { class: "post-detail__title" }, post.title),
          h("div", { class: "post-detail__meta" }, [
            h("a", {
              class: "post-detail__author",
              href: `#/user/${post.author_sid}`,
              onclick: (e) => { e.preventDefault(); navigate(`/user/${post.author_sid}`); },
            }, [
              h("span", { class: "post-detail__avatar" }, post.author_nickname.slice(0, 1)),
              h("span", { class: "post-detail__author-name" }, post.author_nickname),
            ]),
            h("span", { class: "post-detail__time" }, post.created_at),
            h("span", { class: "post-detail__views" }, `浏览 ${formatNumber(post.view_count)}`),
          ]),
          h("div", { class: "post-detail__body" }, post.content),

          // 操作栏
          h("div", { class: "post-detail__actions" }, [
            h("button", {
              class: `action-btn${post.is_liked ? " is-liked" : ""}`,
              onclick: handleLike,
            }, [
              h("span", { class: "action-btn__icon", html: icon("heart", { size: 15 }) }),
              h("span", { class: "action-btn__text" }, `${formatNumber(post.like_count)} 点赞`),
            ]),
            h("button", {
              class: `action-btn${post.is_favorited ? " is-favorited" : ""}`,
              onclick: handleFavorite,
            }, [
              h("span", { class: "action-btn__icon", html: icon("star", { size: 15 }) }),
              h("span", { class: "action-btn__text" }, post.is_favorited ? "已收藏" : "收藏"),
            ]),
            h("button", {
              class: "action-btn",
              onclick: () => document.getElementById("comment-input")?.focus(),
            }, [
              h("span", { class: "action-btn__icon", html: icon("chat", { size: 15 }) }),
              h("span", { class: "action-btn__text" }, `${formatNumber(post.comment_count)} 评论`),
            ]),
            canEdit() ? h("button", {
              class: "action-btn",
              onclick: openEditPost,
            }, [
              h("span", { class: "action-btn__icon", html: icon("pencil", { size: 15 }) }),
              h("span", { class: "action-btn__text" }, "编辑"),
            ]) : null,
            h("button", {
              class: "action-btn",
              onclick: () => openReportModal("post", post.post_id),
            }, [
              h("span", { class: "action-btn__icon" }, "!"),
              h("span", { class: "action-btn__text" }, "举报"),
            ]),
            canDelete() ? h("button", {
              class: "action-btn action-btn--danger",
              onclick: handleDelete,
            }, [
              h("span", { class: "action-btn__icon", html: icon("trash", { size: 15 }) }),
              h("span", { class: "action-btn__text" }, "删除"),
            ]) : null,
            ]),
          ]),
          h("aside", { class: "post-detail__aside" }, [
            h("section", { class: "post-author-card" }, [
              h("div", { class: "post-author-card__avatar" }, post.author_nickname.slice(0, 1)),
              h("h3", {}, post.author_nickname),
              h("p", {}, "社区创作者"),
              h("a", {
                class: "btn btn--ghost btn--sm",
                href: `#/user/${post.author_sid}`,
              }, "查看主页"),
            ]),
            h("section", { class: "post-detail-facts" }, [
              h("div", {}, [h("strong", {}, formatNumber(post.view_count)), h("span", {}, "浏览")]),
              h("div", {}, [h("strong", {}, formatNumber(post.like_count)), h("span", {}, "点赞")]),
              h("div", {}, [h("strong", {}, formatNumber(post.comment_count)), h("span", {}, "评论")]),
            ]),
          ]),
        ]),

        // 评论区
        h("section", { class: "comments-section" }, [
          h("h2", { class: "comments-section__title" }, `评论 (${comments.length})`),

          // 评论输入
          h("div", { class: "comment-input-wrap" }, [
            h("textarea", {
              id: "comment-input",
              class: "comment-input",
              placeholder: "说点什么…",
              rows: 3,
              maxlength: 2000,
              oninput: (e) => {
                commentText = e.target.value;
                if (commentSubmitBtn) commentSubmitBtn.disabled = !commentText.trim();
              },
            }),
            h("div", { class: "comment-input__footer" }, [
              h("span", { class: "comment-input__count" }, `${commentText.length}/2000`),
              commentSubmitBtn = h("button", {
                class: "btn btn--primary btn--sm",
                onclick: submitComment,
                disabled: !commentText.trim(),
              }, "发表评论"),
            ]),
          ]),

          // 评论列表
          comments.length === 0
            ? h("div", { class: "empty-state empty-state--sm" }, [
                h("div", { class: "empty-state__title" }, "还没有评论"),
                h("div", { class: "empty-state__desc" }, "快来抢沙发吧～"),
              ])
            : h("div", { class: "comment-list" },
                comments.map((c) => buildCommentItem(c))
              ),
        ]),
      ])
    );
  };

  function buildCommentItem(c) {
    return h("div", { class: "comment-item" }, [
      h("div", { class: "comment-item__avatar" }, c.author_nickname.slice(0, 1)),
      h("div", { class: "comment-item__body" }, [
        h("div", { class: "comment-item__header" }, [
          h("a", {
            class: "comment-item__author",
            href: `#/user/${c.author_sid}`,
            onclick: (e) => { e.preventDefault(); navigate(`/user/${c.author_sid}`); },
          }, c.author_nickname),
          h("span", { class: "comment-item__time" }, c.created_at),
        ]),
        c.reply_to_nickname
          ? h("div", { class: "comment-item__reply-to" }, `回复 @${c.reply_to_nickname}`)
          : null,
        h("div", { class: "comment-item__content" }, c.is_removed ? "[已删除]" : c.content),
        h("div", { class: "comment-item__actions" }, [
          h("button", {
            class: `comment-item__action${c.is_liked ? " is-liked" : ""}`,
            onclick: () => handleCommentLike(c),
          }, [
            h("i", { class: "icon-inline", html: icon("heart", { size: 13 }) }),
            formatNumber(c.like_count),
          ]),
          h("button", {
            class: "comment-item__action",
            onclick: () => replyTo(c),
          }, "回复"),
          h("button", {
            class: "comment-item__action",
            onclick: () => openReportModal("comment", c.comment_id),
          }, "举报"),
          (canManageComment(c) ? h("button", {
            class: "comment-item__action comment-item__action--danger",
            onclick: () => handleDeleteComment(c),
          }, "删除") : null),
        ]),
      ]),
    ]);
  }

  function canDelete() {
    return post && (post.author_sid === sessionId || isAdmin());
  }

  function canEdit() {
    return post && post.author_sid === sessionId;
  }

  function canManageComment(c) {
    return c.author_sid === sessionId || isAdmin();
  }

  async function handleLike() {
    if (!getCurrentUser()) {
      toast("请先登录后再点赞", "info");
      navigate("/login?redirect=" + encodeURIComponent(location.hash.slice(1)));
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

  async function handleFavorite() {
    if (!getCurrentUser()) {
      toast("请先登录后再收藏", "info");
      navigate("/login?redirect=" + encodeURIComponent(location.hash.slice(1)));
      return;
    }
    try {
      const result = await api.toggleFavorite(post.post_id, sessionId);
      post.is_favorited = result.favorited;
      toast(result.favorited ? "已收藏" : "已取消收藏", "success");
      render();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function handleDelete() {
    if (!confirm("确定删除这篇帖子吗？")) return;
    try {
      if (post.author_sid === sessionId) await api.deletePost(post.post_id);
      else await api.adminDeleteCommunityPost(getAuthToken(), post.post_id);
      toast("已删除", "success");
      navigate("/community");
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }

  async function handleCommentLike(comment) {
    if (!getCurrentUser()) {
      toast("请先登录后再点赞", "info");
      navigate("/login?redirect=" + encodeURIComponent(location.hash.slice(1)));
      return;
    }
    try {
      const result = await api.toggleCommentLike(comment.comment_id, sessionId);
      comment.is_liked = result.liked;
      comment.like_count = result.like_count;
      render();
    } catch (e) {
      toast(`评论点赞失败：${e.message}`, "error");
    }
  }

  async function handleDeleteComment(comment) {
    if (!confirm("确定删除这条评论吗？")) return;
    try {
      if (comment.author_sid === sessionId) await api.deleteComment(comment.comment_id);
      else await api.adminDeleteCommunityComment(getAuthToken(), comment.comment_id);
      comments = comments.filter((item) => item.comment_id !== comment.comment_id);
      post.comment_count = Math.max(0, post.comment_count - 1);
      toast("评论已删除", "success");
      render();
    } catch (e) {
      toast(`删除评论失败：${e.message}`, "error");
    }
  }

  function openReportModal(targetType, targetId) {
    if (!getCurrentUser()) {
      toast("请先登录后再举报", "info");
      navigate("/login?redirect=" + encodeURIComponent(location.hash.slice(1)));
      return;
    }
    const backdrop = h("div", { class: "modal-backdrop" });
    const modal = h("div", { class: "modal" });
    const reason = h("select", { class: "field-select" }, [
      h("option", { value: "虚假信息" }, "虚假信息"),
      h("option", { value: "广告营销" }, "广告营销"),
      h("option", { value: "辱骂攻击" }, "辱骂攻击"),
      h("option", { value: "侵权内容" }, "侵权内容"),
      h("option", { value: "其他" }, "其他"),
    ]);
    const detail = h("textarea", {
      class: "field-textarea",
      rows: 3,
      maxlength: 1000,
      placeholder: "补充说明（选填）",
    });
    const close = () => {
      backdrop.remove();
      modal.remove();
    };
    modal.replaceChildren(
      h("div", { class: "modal__header" }, [
        h("h3", { class: "modal__title" }, "提交举报"),
        h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
      ]),
      h("div", { class: "modal__body" }, [
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "原因"), reason]),
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "说明"), detail]),
      ]),
      h("div", { class: "modal__footer" }, [
        h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "取消"),
        h("button", {
          class: "btn btn--primary",
          type: "button",
          onclick: async () => {
            try {
              await api.reportCommunity({
                target_type: targetType,
                target_id: targetId,
                reason: reason.value,
                detail: detail.value.trim(),
              });
              close();
              toast("举报已提交，管理员会尽快审核", "success");
            } catch (e) {
              toast(`举报失败：${e.message}`, "error");
            }
          },
        }, "提交"),
      ])
    );
    document.body.appendChild(backdrop);
    document.body.appendChild(modal);
  }

  function openEditPost() {
    if (!canEdit()) return;
    const backdrop = h("div", { class: "modal-backdrop" });
    const modal = h("div", { class: "modal modal--lg" });
    const title = h("input", { class: "field-input", value: post.title, maxlength: 120 });
    const content = h("textarea", { class: "field-textarea", rows: 10, value: post.content, maxlength: 5000 });
    const close = () => {
      backdrop.remove();
      modal.remove();
    };
    modal.replaceChildren(
      h("div", { class: "modal__header" }, [
        h("h3", { class: "modal__title" }, "编辑帖子"),
        h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
      ]),
      h("div", { class: "modal__body" }, [
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "标题"), title]),
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "正文"), content]),
      ]),
      h("div", { class: "modal__footer" }, [
        h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "取消"),
        h("button", {
          class: "btn btn--primary",
          type: "button",
          onclick: async () => {
            try {
              await api.updatePost(post.post_id, {
                title: title.value.trim(),
                content: content.value.trim(),
                category: post.category,
              });
              close();
              toast("帖子已更新", "success");
              await loadPost();
              render();
            } catch (e) {
              toast(`保存失败：${e.message}`, "error");
            }
          },
        }, "保存修改"),
      ])
    );
    document.body.appendChild(backdrop);
    document.body.appendChild(modal);
  }

  async function handlePin() {
    try {
      const result = await api.setPostPinned(post.post_id, !post.is_pinned);
      post.is_pinned = result.pinned;
      toast(result.pinned ? "已置顶" : "已取消置顶", "success");
      render();
    } catch (e) {
      toast(`置顶设置失败：${e.message}`, "error");
    }
  }

  let replyTarget = null;

  function replyTo(c) {
    replyTarget = c;
    const input = document.getElementById("comment-input");
    if (input) {
      input.placeholder = `回复 @${c.author_nickname}…`;
      input.focus();
    }
  }

  async function submitComment() {
    const text = commentText.trim();
    if (!text) return;
    if (!getCurrentUser()) {
      toast("请先登录后再评论", "info");
      navigate("/login?redirect=" + encodeURIComponent(location.hash.slice(1)));
      return;
    }

    try {
      await api.createComment(post.post_id, {
        content: text,
        session_sid: sessionId,
        nickname: ensureNickname(),
        reply_to_id: replyTarget ? replyTarget.comment_id : null,
      });
      commentText = "";
      replyTarget = null;
      toast("评论成功", "success");
      await loadComments();
      post.comment_count += 1;
      render();
      const input = document.getElementById("comment-input");
      if (input) input.placeholder = "说点什么…";
    } catch (e) {
      toast("评论失败：" + e.message, "error");
    }
  }

  async function loadPost() {
    try {
      post = await api.getPostDetail(postId);
    } catch (e) {
      if (e.status === 404) post = null;
      else {
        toast("加载失败：" + e.message, "error");
        post = null;
      }
    }
  }

  async function loadComments() {
    try {
      const data = await api.listComments(postId);
      comments = data.items || [];
    } catch (e) {
      comments = [];
    }
  }

  // 初始化
  loading = true;
  render();
  await loadPost();
  if (post) await loadComments();
  loading = false;
  render();
}
