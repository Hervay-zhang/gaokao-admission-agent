/**
 * main.js —— 前端入口（ES Module）
 * 初始化：等高线背景 → 顶部导航 → 路由 → 悬浮 AI → 免责条
 */

import { renderContourBg } from "./components/ContourBg.js";
import { renderNavBar } from "./components/NavBar.js";
import { renderDisclaimerBar } from "./components/DisclaimerBar.js";
import { renderQuickLinks } from "./components/QuickLinks.js";
import { renderFloatingAI } from "./components/FloatingAI.js";
import { startRouter } from "./router.js";
import { ensureSessionId } from "./state.js";
import { api } from "./api.js";

async function bootstrap() {
  // 装饰与常驻元素
  renderContourBg();
  renderNavBar();
  renderDisclaimerBar();
  renderQuickLinks();   // 底部快捷链接（默认含阳光高考等官方平台；后端不可达时走兜底）
  renderFloatingAI();

  // 会话
  ensureSessionId();

  // 路由（会渲染首页）
  startRouter();

  // 后端健康探测（非阻塞；失败仅告警，不影响页面）
  probeBackend();
}

async function probeBackend() {
  try {
    const health = await api.health();
    console.info("[main] 后端已连接：", health.config || health);
  } catch (e) {
    console.warn("[main] 后端未连接（前端仍可离线浏览）：", e.message);
  }
}

// DOM 就绪后启动
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
