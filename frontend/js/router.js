/**
 * router.js —— Hash 路由（无框架自研）
 *
 * 路由命名对齐见径导航：
 *   #/                 首页（Hero + 快捷试问）
 *   #/assessment       全景评估画像
 *   #/report           路径报告（冲稳保三档）
 *   #/schools          院校与专业库
 *   #/chat             AI 问答（非一级导航）
 *   #/cases            案例中心
 *   #/expert           预约专家（付费，非 AI 助手）
 *   #/plan             志愿表
 *   #/me               我的（三栏）
 *   #/community        社区（帖子列表）
 *   #/community/:id    帖子详情
 *   #/user/:sid        用户主页
 *   #/admin            管理后台
 */

import { setState } from "./state.js";
import { clear } from "./ui.js";

// 懒加载页面模块
const routes = {
  "/": () => import("./pages/home.js"),
  "/assessment": () => import("./pages/assessment.js"),
  "/report": () => import("./pages/report.js"),
  "/schools": () => import("./pages/schools.js"),
  "/schools/:id": () => import("./pages/schoolDetail.js"),
  "/schools/compare": () => import("./pages/schoolCompare.js"),
  "/chat": () => import("./pages/chat.js"),
  "/cases": () => import("./pages/cases.js"),
  "/cases/:id": () => import("./pages/caseDetail.js"),
  "/login": () => import("./pages/login.js"),
  "/expert": () => import("./pages/expert.js"),
  "/plan": () => import("./pages/plan.js"),
  "/share/:id": () => import("./pages/shareView.js"),
  "/me": () => import("./pages/me.js"),
  "/community": () => import("./pages/community.js"),
  "/community/:id": () => import("./pages/postDetail.js"),
  "/user/:id": () => import("./pages/userProfile.js"),
  "/admin": () => import("./pages/admin.js"),
};

let currentPath = "";
let currentCleanup = null;
let renderSeq = 0;
const root = () => document.getElementById("app");

/** 解析当前 hash 为路径（去查询串）。 */
export function parseHash() {
  const hash = window.location.hash || "#/";
  const path = hash.replace(/^#/, "") || "/";
  return path.split("?")[0] || "/";
}

/** 解析当前 hash 的查询参数（对象形式）。 */
export function parseQuery() {
  const hash = window.location.hash || "#/";
  const qs = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  const out = {};
  for (const [k, v] of new URLSearchParams(qs)) out[k] = v;
  return out;
}

/** 更新当前路由的查询串（不新增历史之外的副作用；筛选进 URL 用）。 */
export function setQuery(params) {
  const base = parseHash();
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  }
  const qs = usp.toString();
  const next = `#${base}${qs ? "?" + qs : ""}`;
  if (window.location.hash === next) return;
  window.location.hash = next;
}

/** 动态路由匹配：返回 { route, param }（如 #/schools/10001 → { route:"/schools/:code", param:"10001" }）。 */
export function matchRoute(path) {
  if (routes[path]) return { route: path, param: null };
  const segs = path.split("/").filter(Boolean);
  if (segs.length === 2) {
    const dynamic = `/${segs[0]}/:id`;
    if (routes[dynamic]) return { route: dynamic, param: decodeURIComponent(segs[1]) };
  }
  return null;
}

/** 导航到指定路径。 */
export function navigate(path) {
  if (parseHash() === path) {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    render();
  } else {
    window.location.hash = path;
  }
}

/** 渲染当前路由对应的页面。 */
export async function render() {
  const seq = ++renderSeq;
  const path = parseHash();
  const previousPath = currentPath;
  const shouldResetScroll = previousPath !== path;
  currentPath = path;
  setState({ route: path });
  if (shouldResetScroll) {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }

  const matched = matchRoute(path);
  const mount = root();
  if (!mount) return;
  if (typeof currentCleanup === "function") {
    try {
      currentCleanup();
    } catch (e) {
      console.warn("[router] 页面清理失败：", e);
    }
    currentCleanup = null;
  }
  clear(mount);

  if (!matched) {
    mount.appendChild(render404(path));
    return;
  }

  try {
    const mod = await routes[matched.route]();
    if (seq !== renderSeq) return;
    const page = mod.default || mod.render;
    if (typeof page === "function") {
      const lifecycle = await page(mount, { param: matched.param, query: parseQuery() });
      if (seq !== renderSeq) return;
      if (lifecycle && typeof lifecycle.destroy === "function") {
        currentCleanup = lifecycle.destroy;
      }
    } else {
      mount.appendChild(render404(path));
    }
  } catch (e) {
    console.error("[router] 页面加载失败：", e);
    mount.appendChild(renderError(path, e));
  } finally {
    if (shouldResetScroll && currentPath === path) {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }
  }
}

/** 启动路由（监听 hashchange）。 */
export function startRouter() {
  window.addEventListener("hashchange", render);
  if (!window.location.hash) window.location.hash = "#/";
  render();
}

function render404(path) {
  const div = document.createElement("div");
  div.className = "container";
  const box = document.createElement("div");
  box.className = "placeholder";
  box.style.margin = "48px auto";
  box.append("页面不存在：");
  const code = document.createElement("code");
  code.textContent = path;
  box.appendChild(code);
  box.appendChild(document.createElement("br"));
  box.appendChild(document.createElement("br"));
  const link = document.createElement("a");
  link.className = "btn btn--primary";
  link.href = "#/";
  link.textContent = "返回首页";
  box.appendChild(link);
  div.appendChild(box);
  return div;
}

function renderError(path, err) {
  const div = document.createElement("div");
  div.className = "container";
  const box = document.createElement("div");
  box.className = "placeholder";
  box.style.margin = "48px auto";
  box.append("页面加载失败：");
  const code = document.createElement("code");
  code.textContent = path;
  box.appendChild(code);
  box.appendChild(document.createElement("br"));
  const message = document.createElement("span");
  message.style.color = "var(--color-text-light)";
  message.textContent = err.message;
  box.appendChild(message);
  div.appendChild(box);
  return div;
}
