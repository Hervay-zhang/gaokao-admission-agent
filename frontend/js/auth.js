/**
 * auth.js —— 服务端登录态（登录/注册/登出、验证码）。
 */

const AUTH_KEY = "gaokao_auth_v1";

export function getCurrentUser() {
  try {
    const raw = localStorage.getItem(AUTH_KEY) || sessionStorage.getItem(AUTH_KEY);
    if (!raw) return null;
    const user = JSON.parse(raw);
    if (!user || !user.username || !user.role) return null;
    return user;
  } catch (e) {
    console.warn("[auth] 读取登录态失败", e);
    return null;
  }
}

function normalizeUser(user) {
  if (!user || typeof user !== "object") return null;
  return {
    ...user,
    avatarUrl: user.avatarUrl || user.avatar_url || "",
  };
}

function authStorage() {
  if (localStorage.getItem(AUTH_KEY)) return localStorage;
  if (sessionStorage.getItem(AUTH_KEY)) return sessionStorage;
  return null;
}

export async function login(username, password, { remember = true } = {}) {
  let user = null;
  let token = "";
  try {
    const resp = await fetch(`${window.__API_BASE__ ?? "http://127.0.0.1:8000"}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.trim(), password, remember }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail || {};
      return { ok: false, message: detail.message || data.message || "账号或密码错误" };
    }
    user = normalizeUser(data.user);
    token = data.token;
  } catch {
    // 后端不可达：如实告知，不做本地「假登录」（否则用户会以为已登录、数据却存不下来）
    return { ok: false, message: "暂时连不上服务器，请检查网络后重试" };
  }
  user = { ...user, token, loggedInAt: new Date().toISOString() };
  const storage = remember ? localStorage : sessionStorage;
  const otherStorage = remember ? sessionStorage : localStorage;
  otherStorage.removeItem(AUTH_KEY);
  storage.setItem(AUTH_KEY, JSON.stringify(user));
  window.dispatchEvent(new CustomEvent("auth:change", { detail: user }));
  return { ok: true, user };
}

/** 自助注册（考生/家长）：成功即自动登录，返回结构与 login 一致。 */
export async function register({ username, password, nickname = "", role = "student", captchaId = "", captchaCode = "" }) {
  try {
    const resp = await fetch(`${window.__API_BASE__ ?? "http://127.0.0.1:8000"}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.trim(),
        password,
        nickname,
        role,
        captcha_id: captchaId,
        captcha_code: captchaCode,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail || {};
      const message = detail.message || data.message || "注册失败，请稍后重试";
      // 429 限流：把建议等待秒数带给调用方（页面可提示「请 N 秒后再试」）
      return { ok: false, message, retryAfter: detail.retry_after || 0 };
    }
    const user = { ...normalizeUser(data.user), token: data.token, loggedInAt: new Date().toISOString() };
    localStorage.setItem(AUTH_KEY, JSON.stringify(user));
    sessionStorage.removeItem(AUTH_KEY);
    window.dispatchEvent(new CustomEvent("auth:change", { detail: user }));
    return { ok: true, user };
  } catch (e) {
    return { ok: false, message: `注册失败：${e.message}` };
  }
}

/**
 * 获取一张图形验证码（服务端只存答案，返回位图 data URL 供前端渲染）。
 * @returns {Promise<{ok: boolean, captchaId?: string, dataUrl?: string, message?: string}>}
 */
export async function fetchCaptcha() {
  try {
    const resp = await fetch(`${window.__API_BASE__ ?? "http://127.0.0.1:8000"}/api/auth/captcha`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail || {};
      return { ok: false, message: detail.message || "验证码获取失败" };
    }
    return { ok: true, captchaId: data.captcha_id, dataUrl: data.data_url, ttl: data.ttl };
  } catch (e) {
    return { ok: false, message: `验证码获取失败：${e.message}` };
  }
}

export function logout() {
  const current = getCurrentUser();
  if (current?.token) {
    fetch(`${window.__API_BASE__ ?? "http://127.0.0.1:8000"}/api/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${current.token}` },
    }).catch(() => {});
  }
  localStorage.removeItem(AUTH_KEY);
  sessionStorage.removeItem(AUTH_KEY);
  window.dispatchEvent(new CustomEvent("auth:change", { detail: null }));
}

export function updateCurrentUser(patch) {
  const storage = authStorage();
  if (!storage) return null;
  try {
    const current = JSON.parse(storage.getItem(AUTH_KEY) || "null");
    if (!current) return null;
    const updated = { ...current, ...patch };
    storage.setItem(AUTH_KEY, JSON.stringify(updated));
    window.dispatchEvent(new CustomEvent("auth:change", { detail: updated }));
    return updated;
  } catch (e) {
    console.warn("[auth] 更新本地用户失败", e);
    return null;
  }
}

export function isAdmin() {
  return getCurrentUser()?.role === "admin";
}

export function getAuthToken() {
  return getCurrentUser()?.token || "";
}

export function subscribeAuth(listener) {
  const handler = (event) => listener(event.detail || null);
  window.addEventListener("auth:change", handler);
  return () => window.removeEventListener("auth:change", handler);
}
