/**
 * PageShell.js —— 统一页面外壳
 *
 * 目的：保证**骨架跨页一致**（T06 红线：允许内容占位，但导航/Hero/三栏骨架
 * 不允许占位后变形）。所有业务页都用同一外壳渲染「面包屑 + 衬线标题 + 副标题 + 内容区」。
 *
 * 用法：
 *   import { pageShell, placeholderCard } from "../components/PageShell.js";
 *   export default async function report(mount) {
 *     mount.replaceChildren(pageShell({ title: "路径报告", subtitle: "…", body: [ … ] }));
 *   }
 */

import { h } from "../ui.js";

/**
 * @param {object} opts
 * @param {string} opts.title     页面标题（衬线）
 * @param {string} [opts.subtitle] 副标题/说明
 * @param {string} [opts.eyebrow] 眉标（小字，如「第 1 步 / 共 3 步」）
 * @param {Array|Node} [opts.body] 内容区
 * @param {Array|Node} [opts.actions] 标题右侧操作区
 * @returns {HTMLElement}
 */
export function pageShell({ title, subtitle = "", eyebrow = "", body = [], actions = null } = {}) {
  const head = h("header", { class: "page-head" }, [
    eyebrow ? h("div", { class: "page-head__eyebrow" }, eyebrow) : null,
    h("div", { class: "page-head__row" }, [
      h("h1", { class: "page-head__title" }, title),
      actions ? h("div", { class: "page-head__actions" }, actions) : null,
    ]),
    subtitle ? h("p", { class: "page-head__subtitle" }, subtitle) : null,
  ]);

  return h("div", { class: "page container" }, [
    head,
    h("div", { class: "page__body" }, body),
  ]);
}

/**
 * 占位卡：明确标注「待 Txx 落地」，避免把占位当完成。
 * @param {string} text 说明文字（须写明后续任务卡编号）
 * @param {string} [title]
 */
export function placeholderCard(text, title = "功能建设中") {
  return h("div", { class: "placeholder-card", "data-placeholder": "true" }, [
    h("div", { class: "placeholder-card__title" }, title),
    h("div", { class: "placeholder-card__text" }, text),
  ]);
}
