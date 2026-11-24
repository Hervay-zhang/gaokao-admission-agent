/**
 * pages/chat.js —— 升学问答（SSE 流式 + 登录账号会话历史）。
 */

import { h, toast } from "../ui.js";
import { icon } from "../components/Icon.js";
import { mockQuickQuestions } from "../mock.js";
import { chatStreamBox } from "../components/ChatStream.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { api } from "../api.js";
import { getCurrentUser } from "../auth.js";
import { ensureSessionId } from "../state.js";
import { navigate, parseQuery } from "../router.js";

function newChatSessionId(username = "") {
  const prefix = username ? `u_${username}` : "anon";
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export default async function chat(mount) {
  const query = parseQuery();
  const user = getCurrentUser();
  let sessions = [];
  let activeSessionId = query.session || "";
  let initialMessages = [];

  if (user) {
    try {
      const data = await api.chatSessions();
      sessions = data.items || [];
      if (!activeSessionId) activeSessionId = sessions[0]?.sid || newChatSessionId(user.username);
      if (activeSessionId && sessions.some((item) => item.sid === activeSessionId)) {
        const detail = await api.chatSession(activeSessionId);
        initialMessages = detail.messages || [];
      }
    } catch (e) {
      toast(`会话历史加载失败：${e.message}`, "error");
      activeSessionId = activeSessionId || newChatSessionId(user.username);
    }
  } else {
    activeSessionId = ensureSessionId();
  }

  const { box, stream, submit, clear } = chatStreamBox({
    sessionId: activeSessionId,
    initialMessages,
    onDone: () => refreshSessions(),
  });
  const historyHost = h("div", { class: "chat-history-list" });

  function renderHistory() {
    if (!user) {
      historyHost.replaceChildren(
        h("div", { class: "chat-history-empty" }, "登录后可保存并跨设备查看历史会话。")
      );
      return;
    }
    if (!sessions.length) {
      historyHost.replaceChildren(h("div", { class: "chat-history-empty" }, "还没有历史会话。"));
      return;
    }
    historyHost.replaceChildren(...sessions.map((item) =>
      h("div", { class: `chat-history-item${item.sid === activeSessionId ? " is-active" : ""}` }, [
        h("button", {
          class: "chat-history-item__main",
          type: "button",
          onclick: () => navigate(`/chat?session=${encodeURIComponent(item.sid)}`),
        }, [
          h("strong", {}, item.title || "未命名会话"),
          h("span", {}, `${item.message_count || 0} 条 · ${item.updated_at || ""}`),
        ]),
        h("button", {
          class: "chat-history-item__op",
          type: "button",
          title: "重命名",
          onclick: async () => {
            const title = window.prompt("会话名称", item.title || "未命名会话");
            if (!title?.trim()) return;
            try {
              await api.renameChatSession(item.sid, title.trim());
              await refreshSessions();
            } catch (e) {
              toast(`重命名失败：${e.message}`, "error");
            }
          },
        }, [h("i", { class: "icon-slot", html: icon("pencil", { size: 15 }) })]),
        h("button", {
          class: "chat-history-item__op chat-history-item__op--danger",
          type: "button",
          title: "删除",
          onclick: async () => {
            if (!window.confirm("确定删除这条会话吗？")) return;
            try {
              await api.deleteChatSession(item.sid);
              if (item.sid === activeSessionId) {
                const next = sessions.find((session) => session.sid !== item.sid);
                navigate(next ? `/chat?session=${encodeURIComponent(next.sid)}` : "/chat");
              } else {
                await refreshSessions();
              }
            } catch (e) {
              toast(`删除失败：${e.message}`, "error");
            }
          },
        }, [h("i", { class: "icon-slot", html: icon("trash", { size: 15 }) })]),
      ])
    ));
  }

  async function refreshSessions() {
    if (!user) return;
    try {
      const data = await api.chatSessions();
      sessions = data.items || [];
      renderHistory();
    } catch {
      /* 历史列表刷新失败不影响当前对话 */
    }
  }

  const runPrompt = (question) => {
    const input = box.querySelector("#chat-input");
    if (input) input.value = question;
    submit();
  };

  mount.replaceChildren(
    h("div", { class: "chat-page" }, [
      h("div", { class: "container chat-page__inner" }, [
        h("header", { class: "chat-page__header" }, [
          h("div", { class: "chat-page__heading" }, [
            h("span", { class: "chat-page__eyebrow" }, "升学问答 · 免费"),
            h("h1", { class: "chat-page__title" }, "升学问答"),
            h("p", { class: "chat-page__subtitle" },
              "围绕政策、院校与志愿填报连续追问；回答里引用到的政策，都会标出出处。"),
          ]),
        ]),

        h("div", { class: "chat-workspace" }, [
          h("aside", { class: "chat-sidebar" }, [
            h("section", { class: "chat-assistant-card" }, [
              h("div", { class: "chat-assistant-card__avatar" }, "志"),
              h("div", {}, [
                h("h2", {}, "升学助手"),
                h("p", {}, "政策问答 · 院校对比 · 志愿填报"),
              ]),
            ]),
            h("section", { class: "chat-sidebar__section" }, [
              h("div", { class: "chat-sidebar__section-head" }, [
                h("h3", {}, "历史会话"),
                h("button", {
                  class: "btn btn--ghost btn--sm",
                  type: "button",
                  onclick: () => navigate(
                    user
                      ? `/chat?session=${encodeURIComponent(newChatSessionId(user.username))}`
                      : "/chat"
                  ),
                }, "新对话"),
              ]),
              historyHost,
            ]),
            h("section", { class: "chat-sidebar__section" }, [
              h("h3", {}, "可以这样问"),
              h("div", { class: "chat-prompt-list" },
                mockQuickQuestions.map((q) =>
                  h("button", {
                    class: "chat-prompt",
                    type: "button",
                    onclick: () => runPrompt(q),
                  }, [
                    h("span", { class: "chat-prompt__icon" }, "?"),
                    h("span", {}, q),
                  ])
                )
              ),
            ]),
          ]),

          h("section", { class: "chat-panel" }, [
            h("header", { class: "chat-panel__header" }, [
              h("div", { class: "chat-panel__identity" }, [
                h("span", { class: "chat-panel__avatar", html: icon("chat", { size: 17 }) }),
                h("div", {}, [
                  h("strong", {}, "升学助手"),
                  h("span", {}, "当前会话"),
                ]),
              ]),
              h("div", { class: "chat-panel__actions" }, [
                h("button", {
                  class: "btn btn--ghost btn--sm",
                  type: "button",
                  onclick: clear,
                }, "清空对话"),
                h("button", {
                  class: "btn btn--ghost btn--sm",
                  type: "button",
                  onclick: () => {
                    if (!stream.abort()) toast("当前没有正在进行的回答", "info");
                  },
                }, "停止回答"),
              ]),
            ]),
            box,
            h("div", { class: "chat-disclaimer chat-disclaimer--panel" }, DISCLAIMER_TEXT),
          ]),
        ]),
      ]),
    ])
  );

  renderHistory();
  if (query.q) {
    window.setTimeout(() => runPrompt(query.q), 80);
  }

  return { destroy: () => stream.abort(), submit };
}
