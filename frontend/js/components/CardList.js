/**
 * CardList.js —— 白卡列表（对齐见径「我的」页右侧列表）
 *
 * 结构（硬性）：每行 = 标题 + 元信息 + 右箭头 `›`；整体为白卡圆角容器。
 *
 * 用法：
 *   cardList([{ title, meta, tag, onClick, href }], { title: "我的报告" })
 */

import { h } from "../ui.js";

/**
 * @param {Array<{title:string, meta?:string, tag?:string, href?:string, onClick?:Function}>} items
 * @param {object} [opts]
 * @param {string} [opts.title] 列表标题（可选，衬线）
 * @param {string} [opts.empty] 空态文案
 * @returns {HTMLElement}
 */
export function cardList(items = [], { title = "", empty = "暂无数据" } = {}) {
  const rows = items.length
    ? items.map((it) =>
        h(it.href ? "a" : "div", {
          class: "card card--row",
          ...(it.href ? { href: it.href } : null),
          ...(it.onClick ? { onclick: it.onClick } : null),
        }, [
          h("div", { class: "card__body" }, [
            h("div", { class: "card__title" }, [
              it.title,
              it.tag ? h("span", { class: `card__tag${it.tagClass ? " " + it.tagClass : ""}` }, it.tag) : null,
            ]),
            it.meta ? h("div", { class: "card__meta" }, it.meta) : null,
          ]),
          h("span", { class: "card__arrow", "aria-hidden": "true" }, "›"),
        ])
      )
    : [h("div", { class: "card-list__empty" }, empty)];

  return h("div", { class: "card-list-wrap" }, [
    title ? h("h3", { class: "card-list__title" }, title) : null,
    h("div", { class: "card-list" }, rows),
  ]);
}

/**
 * 骨架屏列表（加载态）。
 * @param {number} [rows]
 */
export function cardListSkeleton(rows = 3) {
  return h("div", { class: "card-list", "data-skeleton": "true" },
    Array.from({ length: rows }, () => h("div", { class: "card card--row" }, [
      h("div", { class: "card__body" }, [
        h("div", { class: "skeleton skeleton--title" }),
        h("div", { class: "skeleton skeleton--meta" }),
      ]),
    ]))
  );
}
