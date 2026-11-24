/**
 * ChatStream.js —— AI 对话流式渲染组件（T19）
 *
 * 职责
 * ----
 * 把后端 SSE（meta / token / citation / ping / done / error）渲染成**打字机效果**：
 *   1. 发送后先出**骨架屏**（`is-pending`）；
 *   2. 收到首个 token → 移除骨架，逐 token **追加**渲染（增量，非一次性）；
 *   3. 收到 citation → 在气泡下方追加**来源引用**（编号 + 标题 + 链接）；
 *   4. done → 展示**反馈控件**（点赞/点踩，T22 接线）与免责声明；
 *   5. 自动滚动到底部。
 *
 * 红线
 * ----
 * - **增量渲染**：每个 token 追加，绝不整段替换（保证视觉上的打字机）；
 * - **引用来自后端**：前端不自行拼 URL，只用后端 `citation` 事件的数据；
 * - **无依据明确告知**：`fallback=true` 时给醒目提示，不伪装成正式回答。
 *
 * 与「预约专家」的关系：本组件是**免费 AI 助手**的对话流，与付费真人专家服务完全独立。
 */

import { icon } from "./Icon.js";
import { h } from "../ui.js";
import { api, connectSSE } from "../api.js";
import { DISCLAIMER_TEXT } from "./DisclaimerBar.js";
import { createFeedbackBar } from "./FeedbackBar.js";

/** T21：断线续写最大重试次数与间隔（毫秒，可配置）。 */
const MAX_RESUME_RETRIES = 3;
const RETRY_DELAYS_MS = [500, 1000, 2000];
const SPECIAL_SOURCE_MARKERS = [
  { match: "依学考", label: "依学考招生" },
  { match: "高职院校依据", label: "高职分类招生" },
  { match: "学业水平考试", label: "学考招生" },
  { match: "专升本", label: "专升本招生" },
  { match: "春季高考", label: "春季高考招生" },
  { match: "3+证书", label: "3+证书招生" },
  { match: "退役", label: "退役士兵专项" },
  { match: "艺术", label: "艺术类招生" },
  { match: "体育", label: "体育类招生" },
  { match: "教师专项", label: "教师专项" },
  { match: "卫生专项", label: "卫生专项" },
];

/**
 * 从用户问题中提取降级兜底所需参数（分数 + 科类）。
 * 科类规则：问题含「历史」→ 历史类；否则默认物理类（后端会再校验）。
 * @returns {{score:number, subject_type:string}|null} 提取不到分数返回 null
 */
export function extractFallbackParams(question) {
  const m = (question || "").match(/(\d{2,3})\s*分/);
  if (!m) return null;
  const score = Number(m[1]);
  if (!(score >= 100 && score <= 750)) return null; // 100–750 合法区间（防「50分」误判）
  const subjectType = /历史/.test(question) ? "历史" : "物理";
  return { score, subject_type: subjectType };
}

/**
 * 把专家规则模式兜底结果渲染为结构化表格（降级态与正常态 UI 区分：醒目标识）。
 * @param {HTMLElement} bubble 消息气泡
 * @param {object} result /api/recommend/fallback 返回值
 */
function renderFallbackTable(bubble, result) {
  const rows = [];
  for (const tier of result.tiers || []) {
    for (const it of tier.rows || []) {
      rows.push(
        h("tr", {}, [
          h("td", { class: `tier-tag tier-tag--${it.tier}` }, it.tier_label || tier.label),
          h("td", {}, `${it.university_name}`),
          h("td", {}, String(it.group_code)),
          h("td", {}, it.ref_rank != null ? String(it.ref_rank) : "数据待补"),
          h("td", {}, it.latest_rank != null ? String(it.latest_rank) : "—"),
        ])
      );
    }
  }
  const table = h("div", { class: "fallback-table-wrap is-degraded" }, [
    h("div", { class: "fallback-table__badge" }, [h("i", { class: "icon-slot", html: icon("branch", { size: 13 }) }), "专家规则模式"]),
    h("table", { class: "fallback-table" }, [
      h("thead", {}, [
        h("tr", {}, [h("th", {}, "档位"), h("th", {}, "院校"), h("th", {}, "组"), h("th", {}, "参考位次"), h("th", {}, "最新位次")]),
      ]),
      h("tbody", {}, rows),
    ]),
    h("div", { class: "fallback-table__note" }, result.caliber_text || ""),
  ]);
  bubble.querySelector(".chat-msg__bubble").appendChild(table);
}

/**
 * 创建一个对话渲染器。
 *
 * @param {object} [opts]
 * @param {string} [opts.sessionId] 会话 ID（匿名 token；不传则组件内部生成）
 * @returns {object} 渲染器
 */
export function createChatStream({ sessionId, initialMessages = [], onDone } = {}) {
  const sid = sessionId || `anon-${Math.random().toString(36).slice(2, 10)}`;
  const list = h("div", { class: "chat-list", id: "chat-list" });

  let active = null; // 当前 SSE 连接

  function welcomeMessage() {
    return h("div", { class: "chat-msg chat-msg--assistant" }, [
      h("div", { class: "chat-msg__avatar", "aria-hidden": "true" }, "志"),
      h("div", { class: "chat-msg__content" }, [
        h("div", { class: "chat-msg__meta" }, [
          h("strong", {}, "升学助手"),
          h("span", {}, "刚刚"),
        ]),
        h("div", { class: "chat-msg__bubble" },
          "有问题直接问：某分数能报哪些学校、冲稳保怎么划分、平行志愿怎么填。涉及政策的地方会标出处。"),
      ]),
    ]);
  }

  /** 追加一条消息气泡。 */
  function pushMessage(role, text = "") {
    const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    const bubble = h("div", { class: `chat-msg chat-msg--${role}` }, [
      h("div", { class: "chat-msg__avatar", "aria-hidden": "true" }, role === "user" ? "我" : "志"),
      h("div", { class: "chat-msg__content" }, [
        h("div", { class: "chat-msg__meta" }, [
          h("strong", {}, role === "user" ? "我" : "升学助手"),
          h("span", {}, time),
        ]),
        h("div", { class: "chat-msg__bubble" }, text ? [document.createTextNode(text)] : []),
      ]),
    ]);
    list.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function scrollToBottom() {
    // 平滑滚动到底部（容器或页面）
    try {
      list.scrollTop = list.scrollHeight;
    } catch {
      /* 忽略 */
    }
  }

  /** 在气泡内**追加**文本（打字机）。 */
  function appendText(bubble, text) {
    const target = bubble.querySelector(".chat-msg__bubble");
    target.appendChild(document.createTextNode(text));
    scrollToBottom();
  }

  function messageContent(bubble) {
    return bubble.querySelector(".chat-msg__content");
  }

  /** 添加来源引用块。 */
  function addCitations(bubble, citations) {
    if (!citations || !citations.length) return;
    const unique = [];
    const seen = new Set();
    for (const citation of [...citations].sort((a, b) => Number(a.index || 0) - Number(b.index || 0))) {
      const key = citation.doc_id || citation.url || citation.title;
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(citation);
    }

    const sourceNode = (citation) => {
      const title = citation.title || "未知文档";
      const special = SPECIAL_SOURCE_MARKERS.find((marker) => title.includes(marker.match));
      const meta = [
        citation.doc_type || "政策文件",
        citation.year ? `${citation.year} 年` : "",
        special ? special.label : "普通高考适用",
      ].filter(Boolean);
      const body = h("span", { class: "chat-source__body" }, [
        h("strong", { class: "chat-source__title" }, title),
        h("span", { class: "chat-source__meta" }, meta.join(" · ")),
      ]);
      const content = [
        h("span", { class: "chat-source__index" }, `[${citation.index}]`),
        body,
        h("span", { class: "chat-source__arrow", "aria-hidden": "true" }, "↗"),
      ];
      return citation.url
        ? h("a", {
            class: "chat-source",
            href: citation.url,
            target: "_blank",
            rel: "noopener noreferrer",
          }, content)
        : h("div", { class: "chat-source" }, content);
    };

    const box = h("div", { class: "chat-citations" }, [
      h("div", { class: "chat-citations__head" }, [
        h("strong", { class: "chat-citations__title" }, "来源依据"),
        h("span", { class: "chat-citations__count" }, `${unique.length} 条`),
      ]),
      h("div", { class: "chat-citations__list" }, unique.map(sourceNode)),
    ]);
    messageContent(bubble).appendChild(box);
    scrollToBottom();
  }

  /** 添加反馈控件（T22：真实接口接线，一条输出仅一次反馈）。 */
  function addFeedback(bubble, meta) {
    const bar = createFeedbackBar({
      messageId: meta && meta.message_id != null ? meta.message_id : null,
      sessionSid: sid,
    });
    messageContent(bubble).appendChild(bar.el);
    scrollToBottom();
  }

  /** 添加降级提示（无依据/越界）。 */
  function addNotice(bubble, text, kind = "warn") {
    messageContent(bubble).appendChild(h("div", { class: `chat-notice chat-notice--${kind}` }, text));
    scrollToBottom();
  }

  /** 追加免责声明。 */
  function addDisclaimer(bubble) {
    if ((bubble.innerText || "").includes(DISCLAIMER_TEXT)) return;
    messageContent(bubble).appendChild(h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT));
    scrollToBottom();
  }

  /**
   * 发送一条消息并流式接收回复（T21：断线自动续写 + 降级专家规则模式）。
   * @param {string} question
   */
  function send(question) {
    const text = (question || "").trim();
    if (!text) return;

    pushMessage("user", text);

    // 骨架屏：先占位，首 token 到达后移除
    const bubble = pushMessage("assistant");
    bubble.classList.add("is-pending");
    bubble.querySelector(".chat-msg__bubble").appendChild(
      h("span", { class: "chat-typing" }, "•••")
    );

    let firstToken = false;
    let retries = 0;
    let conn = null;

    const onFirstToken = () => {
      if (firstToken) return;
      firstToken = true;
      bubble.classList.remove("is-pending");
      const b = bubble.querySelector(".chat-msg__bubble");
      b.replaceChildren(); // 清掉占位
    };

    // ---- 降级：3 次续写失败 / 服务端终判 → 专家规则模式（不依赖 LLM）----
    const degrade = async () => {
      onFirstToken();
      const notice =
        "AI 线路拥挤，已为您切换至专家规则模式。以下为基于历史投档数据的结构化结果。";
      addNotice(bubble, notice, "warn");
      const params = extractFallbackParams(text);
      if (!params) {
        addNotice(
          bubble,
          "当前问题无法提取分数信息，暂不能生成兜底推荐表。您可以到「开始测评」填写分数获得结构化推荐。",
          "warn"
        );
        addDisclaimer(bubble);
        return;
      }
      try {
        const result = await api.recommendFallback(params);
        renderFallbackTable(bubble, result);
        addDisclaimer(bubble);
      } catch (e) {
        addNotice(bubble, `兜底服务暂不可用：${e.message}`, "error");
        addDisclaimer(bubble);
      }
    };

    const handlers = {
      onMeta: () => {
        /* stream_id 由 connectSSE 内部跟踪（getLastEventId/getStreamId） */
      },
      onToken: (t) => {
        onFirstToken();
        if (t) appendText(bubble, t);
      },
      onCitation: (cites) => addCitations(bubble, cites),
      onDone: (meta) => {
        onFirstToken();
        if (meta && meta.fallback) {
          addNotice(bubble, "库内暂未收录该问题的官方依据，以上为兜底提示。", "warn");
        }
        if (meta && meta.grounded === false && !meta.fallback) {
          addNotice(bubble, "本条回复未使用库内依据。", "warn");
        }
        addFeedback(bubble, meta);
        addDisclaimer(bubble);
        onDone && onDone(meta);
      },
      onError: (err, { fromServer } = {}) => {
        onFirstToken();
        if (fromServer) {
          // 服务端终判（error 事件）：续写无意义，直接降级
          degrade();
          return;
        }
        // 网络异常：凭 streamId/lastEventId 断点续写（同一 stream 继续，非重新生成）
        const streamId = conn && conn.getStreamId();
        const lastId = conn ? conn.getLastEventId() : -1;
        if (streamId && lastId >= 0 && retries < MAX_RESUME_RETRIES) {
          retries += 1;
          addNotice(bubble, `连接中断，正在恢复连接…（第 ${retries}/${MAX_RESUME_RETRIES} 次）`, "warn");
          setTimeout(() => {
            conn = connectSSE(
              `/api/chat/stream/${encodeURIComponent(streamId)}/resume`,
              { last_event_id: lastId, session_id: sid },
              handlers
            );
          }, RETRY_DELAYS_MS[retries - 1] ?? 1000);
          return;
        }
        // 重试耗尽：降级专家规则模式（不依赖 LLM）
        degrade();
      },
      onRateLimited: ({ retryAfter, reason }) => {
        onFirstToken();
        const wait = retryAfter > 0 ? `请 ${retryAfter} 秒后再试` : "请稍后再试";
        addNotice(bubble, `请求过于频繁：${reason}（${wait}）`, "error");
      },
    };

    conn = connectSSE("/api/chat/stream", { q: text, session_id: sid }, handlers);
    active = conn;
  }

  /** 中止当前流；返回是否真的打断了一次进行中的回答。 */
  function abort() {
    if (!active) return false;
    active.abort();
    return true;
  }

  function clear() {
    abort();
    list.replaceChildren(welcomeMessage());
  }

  function renderHistory(items) {
    abort();
    list.replaceChildren();
    for (const item of items || []) {
      if (!item || !["user", "assistant"].includes(item.role)) continue;
      const bubble = pushMessage(item.role, item.content || "");
      if (Array.isArray(item.citations) && item.citations.length) {
        addCitations(bubble, item.citations);
      }
    }
    if (!list.children.length) list.appendChild(welcomeMessage());
  }

  renderHistory(initialMessages);

  return { list, send, abort, clear, sessionId: sid };
}

/**
 * 组装「对话区」DOM（列表 + 输入框）。
 * @param {object} [opts]
 */
export function chatStreamBox({ sessionId, initialMessages = [], onDone } = {}) {
  const stream = createChatStream({ sessionId, initialMessages, onDone });

  const input = h("textarea", {
    class: "chat-input__field",
    id: "chat-input",
    rows: 1,
    placeholder: "输入你的问题，例如：广东物理类 600 分能报什么？",
    onkeydown: (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    },
    oninput: (e) => {
      e.target.style.height = "auto";
      e.target.style.height = `${Math.min(e.target.scrollHeight, 132)}px`;
    },
  });

  function submit() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    stream.send(text);
  }

  const box = h("div", { class: "chat-box" }, [
    stream.list,
    h("div", { class: "chat-composer" }, [
      h("div", { class: "chat-input" }, [
        input,
        h("button", {
          class: "btn btn--primary chat-input__send",
          type: "button",
          onclick: submit,
          "aria-label": "发送消息",
        }, "发送"),
      ]),
      h("div", { class: "chat-composer__meta" }, "回答会标注政策出处，可点开原文核对"),
    ]),
  ]);

  return { box, stream, submit, clear: stream.clear };
}
