/**
 * ui.js —— 轻量 DOM 渲染工具（无框架）
 *
 * 提供 h() 创建元素、clear() 清空、以及格式化辅助。
 * 所有组件通过此模块构建 DOM，避免把逻辑塞进 HTML。
 */

/**
 * 创建 DOM 元素。
 * @param {string} tag 标签名，可带类名，如 "div.card__title"
 * @param {object} [attrs] 属性/事件（on* 为事件监听，其余为 attribute）
 * @param {Array|string|Node} [children] 子节点
 * @returns {HTMLElement}
 */
export function h(tag, attrs = {}, children = []) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");

  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      el.className = [el.className, value].filter(Boolean).join(" ");
    } else if (key === "html") {
      el.innerHTML = value;
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }

  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** 清空元素子节点。 */
export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** 安全转义（注：内部不用 v-html 场景，尽量避免 innerHTML）。 */
export function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/** 数字千分位。 */
export function formatNumber(n) {
  if (n === null || n === undefined || n === "") return "—";
  return Number(n).toLocaleString("zh-CN");
}

/** 分数→档位描述（前端不计算，仅展示后端口径；此处用于占位）。 */
export function tierLabel(tier) {
  return { chong: "冲", wen: "稳", bao: "保" }[tier] || tier;
}

/** 区块标题（衬线）。 */
export function sectionTitle(text) {
  return h("h2", { class: "section-title", style: "font-family:var(--font-serif);color:var(--color-ink);margin:0 0 var(--space-4);" }, text);
}

/** 简易 toast 提示。 */
export function toast(message, type = "info") {
  const el = h("div", {
    class: `toast toast--${type}`,
    style:
      "position:fixed;top:80px;left:50%;transform:translateX(-50%);z-index:999;" +
      "padding:10px 20px;border-radius:999px;background:rgba(30,58,95,.94);color:#fff;" +
      "font-size:14px;box-shadow:0 8px 24px rgba(30,58,95,.3);transition:opacity .3s;",
  }, message);
  document.body.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, 2200);
}
