/**
 * api.js —— 后端接口封装（fetch）+ SSE 连接
 *
 * 统一处理：baseURL、错误、超时、request_id。
 * T01 仅健康检查；业务接口按后续任务卡逐步补充。
 * SSE 在 T19 接入真实端点，这里先提供可用的连接骨架（含断线重连与降级回调）。
 */

import { getAuthToken, getCurrentUser, logout } from "./auth.js";

/** 后端基地址（可用 window.__API_BASE__ 覆盖，便于容器/部署）。
 *
 * 注意：必须用 ?? 而非 ||——生产环境 __API_BASE__ 会被置为 ""（同域，由 nginx 反代 /api），
 * 空字符串是 falsy，若用 || 会误判为「未注入」而回退到 127.0.0.1:8000，导致线上请求打到访客本机。
 */
export const API_BASE = window.__API_BASE__ ?? "http://127.0.0.1:8000";

/** 统一请求封装。 */
async function request(path, { method = "GET", body, timeout = 15000, headers = {} } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const authToken = getAuthToken();
    const resp = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": genRequestId(),
        ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        ...headers,
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });

    const text = await resp.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }

    if (!resp.ok) {
      const err = new Error(data.message || `请求失败（${resp.status}）`);
      err.status = resp.status;
      err.payload = data;
      if (resp.status === 401 || data?.detail?.code === "AUTH_EXPIRED") {
        handleAuthExpired();
      }
      throw err;
    }
    return data;
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("请求超时，请检查网络或稍后重试");
    }
    console.warn(`[api] ${path} 调用失败：`, e.message);
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

let authRedirecting = false;

function handleAuthExpired() {
  if (authRedirecting) return;
  authRedirecting = true;
  try {
    logout();
  } catch {
    /* 清空本地态失败时也要继续跳转登录 */
  }
  const current = decodeURIComponent((location.hash || "").replace(/^#/, ""));
  if (!current.startsWith("/login")) {
    const redirect = encodeURIComponent(current || "/");
    window.location.hash = `#/login?redirect=${redirect}`;
  }
  window.setTimeout(() => {
    authRedirecting = false;
  }, 500);
}

function genRequestId() {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

/* ==================== 健康检查 ==================== */
export const api = {
  health: () => request("/health"),
  healthLLM: () => request("/api/health/llm"),
  healthDB: () => request("/api/health/db"),
  healthRedis: () => request("/api/health/redis"),
  healthRatelimit: () => request("/api/health/ratelimit"),
  stats: () => request("/api/stats", { timeout: 10000 }),
  /** 专家规则模式兜底表（T11/T21 降级出口，不依赖 LLM）。 */
  recommendFallback: (params) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join("&");
    return request(`/api/recommend/fallback${qs ? "?" + qs : ""}`, { timeout: 10000 });
  },
  /** T22：提交反馈（点赞/点踩/纠错）。409 = 该输出已反馈过。 */
  submitFeedback: (payload) =>
    request("/api/feedback", { method: "POST", body: payload, timeout: 10000 }),
  uploadAvatar: (dataUrl) =>
    request("/api/auth/avatar", { method: "POST", body: { data_url: dataUrl }, timeout: 20000 }),
  deleteAvatar: () => request("/api/auth/avatar", { method: "DELETE", timeout: 10000 }),
  changePassword: (payload) =>
    request("/api/auth/password", { method: "POST", body: payload, timeout: 10000 }),
  updateAccountProfile: (payload) =>
    request("/api/auth/profile", { method: "PATCH", body: payload, timeout: 10000 }),
  myProfile: () => request("/api/profile/mine", { timeout: 10000 }),
  /** T23：画像测评建档（脱敏落库；回执只含区间）。 */
  createProfile: (payload) =>
    request("/api/profile", { method: "POST", body: payload, timeout: 15000 }),
  /** T23：分数→位次 + 近三年等位分（位次直填优先）。 */
  queryRank: (params) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join("&");
    return request(`/api/rank${qs ? "?" + qs : ""}`, { timeout: 15000 });
  },
  /** T24：院校列表检索（筛选 + 分页）。 */
  listSchools: (params) => request(`/api/schools${toQuery(params)}`, { timeout: 15000 }),
  /** T24：院校库筛选项枚举。 */
  schoolsMeta: () => request("/api/schools/meta"),
  /** T24：院校详情（专业组历年投档）。 */
  schoolDetail: (code, params = {}) =>
    request(`/api/schools/${encodeURIComponent(code)}${toQuery(params)}`, { timeout: 15000 }),
  /** T24：院校对比（≤3 所）。 */
  compareSchools: (codes, params = {}) =>
    request(`/api/schools/compare${toQuery({ codes: codes.join(","), ...params })}`, { timeout: 15000 }),
  /** T25：AI 报告（冲稳保三档 + 解释 + 风险 + 家长版）。 */
  buildReport: (params) => request(`/api/report${toQuery(params)}`, { timeout: 30000 }),
  saveReportHistory: (payload) =>
    request("/api/report/history", { method: "POST", body: payload, timeout: 10000 }),
  reportHistory: () => request("/api/report/history", { timeout: 10000 }),
  reportHistoryDetail: (snapshotId) =>
    request(`/api/report/history/${encodeURIComponent(snapshotId)}`, { timeout: 10000 }),

  // ---- T26 志愿表 ----
  createPlan: (payload) => request("/api/plan", { method: "POST", body: payload, timeout: 10000 }),
  listPlans: (profileId) => request(`/api/plan${toQuery({ profile_id: profileId })}`, { timeout: 10000 }),
  myPlans: () => request("/api/plan/mine", { timeout: 10000 }),
  getPlan: (planId) => request(`/api/plan/${encodeURIComponent(planId)}`, { timeout: 10000 }),
  deletePlan: (planId) => request(`/api/plan/${encodeURIComponent(planId)}`, { method: "DELETE", timeout: 10000 }),
  addPlanItem: (planId, version, item) =>
    request(`/api/plan/${encodeURIComponent(planId)}/items${toQuery({ version })}`, {
      method: "POST", body: item, timeout: 10000,
    }),
  updatePlanItem: (planId, itemId, payload) =>
    request(`/api/plan/${encodeURIComponent(planId)}/items/${encodeURIComponent(itemId)}`, {
      method: "PATCH", body: payload, timeout: 10000,
    }),
  deletePlanItem: (planId, itemId, version) =>
    request(`/api/plan/${encodeURIComponent(planId)}/items/${encodeURIComponent(itemId)}${toQuery({ version })}`, {
      method: "DELETE", timeout: 10000,
    }),
  reorderPlan: (planId, version, itemIds) =>
    request(`/api/plan/${encodeURIComponent(planId)}/reorder`, {
      method: "POST", body: { version, item_ids: itemIds }, timeout: 10000,
    }),

  // ---- T27 案例中心 ----
  listCases: (params = {}) => request(`/api/cases${toQuery(params)}`, { timeout: 10000 }),
  caseDetail: (slug) => request(`/api/cases/${encodeURIComponent(slug)}`, { timeout: 10000 }),

  // ---- T28 导出与分享 ----
  createShare: (planId, ttlDays) =>
    request("/api/share", {
      method: "POST",
      body: ttlDays ? { plan_id: planId, ttl_days: ttlDays } : { plan_id: planId },
      timeout: 10000,
    }),
  readShare: (token) => request(`/api/share/${encodeURIComponent(token)}`, { timeout: 10000 }),
  revokeShare: (token) =>
    request(`/api/share/${encodeURIComponent(token)}`, { method: "DELETE", timeout: 10000 }),

  // ---- 社区/论坛 ----
  _communityHeaders: () => {
    const sid = (typeof window !== 'undefined' && window.__sessionId) || '';
    return sid ? { "X-Session-Id": sid } : {};
  },
  listPosts: (params = {}) =>
    request(`/api/community/posts${toQuery(params)}`, { headers: api._communityHeaders(), timeout: 10000 }),
  getPostDetail: (postId) =>
    request(`/api/community/posts/${postId}`, { headers: api._communityHeaders(), timeout: 10000 }),
  createPost: (payload) =>
    request("/api/community/posts", { method: "POST", body: payload, headers: api._communityHeaders(), timeout: 10000 }),
  toggleLike: (postId, sessionSid) =>
    request(`/api/community/posts/${postId}/like`, {
      method: "POST", body: { session_sid: sessionSid }, headers: api._communityHeaders(), timeout: 8000,
    }),
  toggleFavorite: (postId, sessionSid) =>
    request(`/api/community/posts/${postId}/favorite`, {
      method: "POST", body: { session_sid: sessionSid }, headers: api._communityHeaders(), timeout: 8000,
    }),
  toggleCommentLike: (commentId, sessionSid) =>
    request(`/api/community/comments/${commentId}/like`, {
      method: "POST",
      body: { session_sid: sessionSid },
      headers: api._communityHeaders(),
      timeout: 8000,
    }),
  listComments: (postId) =>
    request(`/api/community/posts/${postId}/comments`, { headers: api._communityHeaders(), timeout: 10000 }),
  createComment: (postId, payload) =>
    request(`/api/community/posts/${postId}/comments`, {
      method: "POST", body: payload, headers: api._communityHeaders(), timeout: 10000,
    }),
  updatePost: (postId, payload) =>
    request(`/api/community/posts/${postId}`, {
      method: "PUT", body: payload, headers: api._communityHeaders(), timeout: 10000,
    }),
  deleteComment: (commentId) =>
    request(`/api/community/comments/${commentId}`, {
      method: "DELETE", headers: api._communityHeaders(), timeout: 8000,
    }),
  reportCommunity: (payload) =>
    request("/api/community/reports", {
      method: "POST", body: payload, headers: api._communityHeaders(), timeout: 8000,
    }),
  deletePost: (postId) =>
    request(`/api/community/posts/${postId}`, { method: "DELETE", headers: api._communityHeaders(), timeout: 8000 }),
  setPostPinned: (postId, pinned, token = "") =>
    request(`/api/community/posts/${postId}/pin`, {
      method: "PUT",
      body: { pinned },
      headers: api._adminHeaders(token),
      timeout: 8000,
    }),
  forumCategories: () => request("/api/community/categories", { timeout: 8000 }),
  userPosts: (userSid, params = {}) =>
    request(`/api/community/user/${encodeURIComponent(userSid)}/posts${toQuery(params)}`, {
      headers: api._communityHeaders(), timeout: 10000,
    }),
  userFavorites: (userSid, params = {}) =>
    request(`/api/community/user/${encodeURIComponent(userSid)}/favorites${toQuery(params)}`, {
      headers: api._communityHeaders(), timeout: 10000,
    }),
  userComments: (userSid, params = {}) =>
    request(`/api/community/user/${encodeURIComponent(userSid)}/comments${toQuery(params)}`, {
      headers: api._communityHeaders(), timeout: 10000,
    }),
  userStats: (userSid) =>
    request(`/api/community/user/${encodeURIComponent(userSid)}/stats`, { headers: api._communityHeaders(), timeout: 8000 }),

  // ---- AI 会话历史 ----
  chatSessions: () => request("/api/chat/sessions", { timeout: 10000 }),
  chatSession: (sessionId) =>
    request(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, { timeout: 10000 }),
  renameChatSession: (sessionId, title) =>
    request(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH", body: { title }, timeout: 8000,
    }),
  deleteChatSession: (sessionId) =>
    request(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE", timeout: 8000,
    }),

  // ---- T31 管理后台（X-Admin-Token 鉴权；错误 Token 一律 404）----
  _adminHeaders: (token) => {
    const headers = { "X-Admin-Token": token };
    if (getCurrentUser()?.role === "admin") headers["X-Admin-Demo-Role"] = "admin";
    return headers;
  },
  adminOverview: (token) =>
    request("/api/admin/overview", { headers: api._adminHeaders(token), timeout: 15000 }),
  adminVersions: (token) =>
    request("/api/admin/versions", { headers: api._adminHeaders(token), timeout: 15000 }),
  adminSwitch: (token, payload) =>
    request("/api/admin/versions/switch", {
      method: "POST", body: payload, headers: api._adminHeaders(token), timeout: 15000,
    }),
  adminImportTrigger: (token) =>
    request("/api/admin/import", { method: "POST", headers: api._adminHeaders(token), timeout: 10000 }),
  adminImportStatus: (token) =>
    request("/api/admin/import", { headers: api._adminHeaders(token), timeout: 10000 }),
adminFeedback: (token, limit = 30) =>
    request(`/api/admin/feedback${toQuery({ limit })}`, { headers: api._adminHeaders(token), timeout: 15000 }),
  adminDisclaimer: (token) =>
    request("/api/admin/disclaimer", { headers: api._adminHeaders(token), timeout: 10000 }),
  consultations: () => request("/api/consultations", { timeout: 8000 }),
  adminUpdateConsultations: (token, items) =>
    request("/api/admin/consultations", {
      method: "PUT",
      body: { items },
      headers: api._adminHeaders(token),
      timeout: 10000,
    }),

  // ---- 页面底部快捷链接（公开读取 + 管理端增删改）----
  quickLinks: () => request("/api/quick-links", { timeout: 8000 }),
  adminQuickLinks: (token) =>
    request("/api/admin/quick-links", { headers: api._adminHeaders(token), timeout: 10000 }),
  adminCreateQuickLink: (token, payload) =>
    request("/api/admin/quick-links", {
      method: "POST", body: payload, headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminUpdateQuickLink: (token, linkId, payload) =>
    request(`/api/admin/quick-links/${encodeURIComponent(linkId)}`, {
      method: "PUT", body: payload, headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminDeleteQuickLink: (token, linkId) =>
    request(`/api/admin/quick-links/${encodeURIComponent(linkId)}`, {
      method: "DELETE", headers: api._adminHeaders(token), timeout: 10000,
    }),
  createExpertAppointment: (payload) =>
    request("/api/expert-appointments", { method: "POST", body: payload, timeout: 10000 }),
  adminExpertAppointments: (token, params = {}) =>
    request(`/api/admin/expert-appointments${toQuery(params)}`, {
      headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminUpdateExpertAppointment: (token, appointmentId, status) =>
    request(`/api/admin/expert-appointments/${encodeURIComponent(appointmentId)}`, {
      method: "PATCH", body: { status }, headers: api._adminHeaders(token), timeout: 10000,
    }),
  myExpertAppointments: () => request("/api/expert-appointments/mine", { timeout: 10000 }),
  adminUsers: (token, params = {}) =>
    request(`/api/admin/users${toQuery(params)}`, { headers: api._adminHeaders(token), timeout: 10000 }),
  adminUpdateUser: (token, username, payload) =>
    request(`/api/admin/users/${encodeURIComponent(username)}`, {
      method: "PATCH", body: payload, headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminUniversities: (token, params = {}) =>
    request(`/api/admin/universities${toQuery(params)}`, { headers: api._adminHeaders(token), timeout: 10000 }),
  adminUpdateUniversity: (token, code, payload) =>
    request(`/api/admin/universities/${encodeURIComponent(code)}`, {
      method: "PATCH", body: payload, headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminAuditLogs: (token, limit = 100) =>
    request(`/api/admin/audit-logs${toQuery({ limit })}`, { headers: api._adminHeaders(token), timeout: 10000 }),
  adminCommunityReports: (token, params = {}) =>
    request(`/api/community/admin/reports${toQuery(params)}`, { headers: api._adminHeaders(token), timeout: 10000 }),
  adminResolveCommunityReport: (token, reportId, status) =>
    request(`/api/community/admin/reports/${encodeURIComponent(reportId)}`, {
      method: "PATCH", body: { status }, headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminDeleteCommunityPost: (token, postId) =>
    request(`/api/community/admin/posts/${encodeURIComponent(postId)}`, {
      method: "DELETE", headers: api._adminHeaders(token), timeout: 10000,
    }),
  adminDeleteCommunityComment: (token, commentId) =>
    request(`/api/community/admin/comments/${encodeURIComponent(commentId)}`, {
      method: "DELETE", headers: api._adminHeaders(token), timeout: 10000,
    }),
};

/** 对象 → query string（过滤空值）。 */
function toQuery(params) {
  const qs = Object.entries(params || {})
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(Array.isArray(v) ? v.join(",") : v)}`)
    .join("&");
  return qs ? `?${qs}` : "";
}

/**
 * SSE 连接（T19 接入真实 /api/chat/stream）。
 *
 * 用 fetch + ReadableStream 手动实现（EventSource 只支持 GET 且不能带自定义头，
 * 但我们需要携带 X-Request-ID 并统一错误处理，故手写解析）。
 *
 * 支持的事件类型（与后端 app/agent/sse.py 一致）：
 *   meta（开始，含 stream_id/intent）/ token（增量文本）/ citation（引用）
 *   / ping（心跳，忽略）/ done（结束，含 message_id）/ error（错误）
 *
 * @param {string} path GET 路径（如 "/api/chat/stream"）
 * @param {object} query 查询参数（拼到 URL）
 * @param {object} handlers
 *   { onMeta, onToken, onCitation, onPing, onDone, onError, onFallback, onRateLimited }
 * @returns {{abort: Function}}
 */
export function connectSSE(path, query = {}, handlers = {}) {
  const controller = new AbortController();
  const { onMeta, onToken, onCitation, onPing, onDone, onError, onFallback, onRateLimited } = handlers;

  // 断点续写状态（T21）：客户端已收到的最后事件 id 与流 id
  let lastId = -1;
  let streamId = null;
  let sawDone = false;

  const qs = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
  const url = `${API_BASE}${path}${qs ? "?" + qs : ""}`;
  const authToken = getAuthToken();

  (async () => {
    try {
      const resp = await fetch(url, {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
          "X-Request-ID": genRequestId(),
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        signal: controller.signal,
      });

      // 429：限流（T20）——单独回调，携带 Retry-After，便于前端提示「稍后再试」
      if (resp.status === 429) {
        const retryAfter = Number(resp.headers.get("Retry-After")) || 0;
        let reason = "请求过于频繁，请稍后再试";
        try {
          const txt = await resp.text();
          const m = txt.match(/"message"\s*:\s*"([^"]+)"/);
          if (m) reason = m[1];
        } catch {
          /* 忽略解析失败，用默认文案 */
        }
        onRateLimited && onRateLimited({ retryAfter, reason });
        return;
      }

      if (!resp.ok || !resp.body) throw new Error(`SSE 建立失败（${resp.status}）`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE 事件以空行分隔
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const ev = parseSSEBlock(part);
          if (!ev) continue;
          if (ev.id != null) lastId = Number(ev.id);
          if (ev.event === "meta" && ev.data && typeof ev.data === "object" && ev.data.stream_id) {
            streamId = ev.data.stream_id;
          }
          dispatchSSE(ev, { onMeta, onToken, onCitation, onPing, onDone, onError }, () => {
            sawDone = true;
          });
        }
      }
      // 流正常结束但未收到 done（例如代理截断）→ 兜底触发 onDone
      if (!sawDone) onDone && onDone({});
    } catch (e) {
      if (e.name === "AbortError") return;
      console.warn("[api] SSE 异常：", e.message);
      // 网络异常（非服务端 error 事件）→ 上层可凭 streamId/lastId 断点续写（T21）
      onFallback && onFallback(e);
      onError && onError(e, { fromServer: false });
    }
  })();

  return {
    abort: () => controller.abort(),
    /** 断点续写用：已收到的最后一个事件 id（-1 表示尚未收到任何事件）。 */
    getLastEventId: () => lastId,
    /** 当前流 id（meta 事件后可用；null 表示尚未建立）。 */
    getStreamId: () => streamId,
    /** 流是否已收到 done（服务端终判）。 */
    isDone: () => sawDone,
  };
}

/** 解析一个 SSE 事件块 → { id, event, data(已 JSON 化) }。 */
function parseSSEBlock(block) {
  const lines = block.split("\n");
  let id = null;
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    data = raw;
  }
  return { id, event, data };
}

/** 按事件类型分派到对应回调。 */
function dispatchSSE(ev, cb, markDone) {
  const { event, data } = ev;
  switch (event) {
    case "meta":
      cb.onMeta && cb.onMeta(data);
      break;
    case "token":
      cb.onToken && cb.onToken(typeof data === "object" ? data.text ?? "" : String(data));
      break;
    case "citation":
      cb.onCitation && cb.onCitation(Array.isArray(data) ? data : [data]);
      break;
    case "ping":
      cb.onPing && cb.onPing(data);
      break;
    case "done":
      markDone();
      cb.onDone && cb.onDone(data);
      break;
    case "error":
      // 服务端终判 error 事件（fromServer=true）：上层据此决定降级而非续写
      cb.onError && cb.onError(new Error((data && data.message) || "服务异常"), { fromServer: true });
      break;
    default:
      // 未知事件：忽略（保证向后兼容）
      break;
  }
}
