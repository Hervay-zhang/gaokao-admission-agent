/**
 * FloatingAI.js —— 「升学问答」悬浮入口
 *
 * 定位：**免费的升学问答对话**，与「预约专家」（付费真人服务）完全独立。
 * 形态：朱砂红胶囊（白字 + 线描对话图标），常驻右下角，全站可见；
 *       进入问答页本身时自动隐藏（避免入口与当前页面重复）。
 * 为什么不放 ✦：星芒是 AI 产品的通用装饰符号，跨平台字形不稳定且与站点线描体系冲突。
 */

import { h } from "../ui.js";
import { icon } from "./Icon.js";
import { navigate } from "../router.js";

let panelOpen = false;

export function renderFloatingAI() {
  const root = document.getElementById("floating-ai-root");
  if (!root) return;
  root.replaceChildren(
    h("button", {
      class: "floating-ai",
      id: "floating-ai-btn",
      "aria-label": "打开升学问答",
      onclick: togglePanel,
    }, [
      h("span", { class: "floating-ai__icon", html: icon("chat", { size: 16 }) }),
      h("span", {}, "升学问答"),
    ])
  );
  syncVisibility();
  if (!visibilityBound) {
    window.addEventListener("hashchange", syncVisibility);
    visibilityBound = true;
  }
}

let visibilityBound = false;

/** 问答页本身已有完整对话界面，悬浮入口在该页隐藏。 */
function syncVisibility() {
  const root = document.getElementById("floating-ai-root");
  if (!root) return;
  const path = (window.location.hash || "").replace(/^#/, "");
  root.hidden = path === "/chat" || path.startsWith("/chat?");
}

function togglePanel() {
  panelOpen = !panelOpen;
  if (panelOpen) {
    navigate("/chat");
    panelOpen = false;
  }
}
