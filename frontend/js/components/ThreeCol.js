/**
 * ThreeCol.js —— 三栏布局（对齐见径「我的」页）
 *
 * 结构（硬性，不得改动）：
 *   ┌─────────────────────────────────────────────────────────┐
 *   │ .threecol                                                │
 *   │  ├─ .threecol__aside   左统计卡（大号细体数字）            │
 *   │  ├─ .threecol__menu    中垂直菜单卡（可横滑）              │
 *   │  └─ .threecol__main    右内容区（CardList）               │
 *   └─────────────────────────────────────────────────────────┘
 *
 * 响应式（T06-J6）：
 *   ≥1024px  三栏横排（统计列 | 菜单列 | 内容列）
 *   <768px   折叠为纵向流：统计**横排** → 菜单**横滑** → 列表**纵排**
 *
 * 用法：
 *   threeCol({ stats: [...], menu: [...], main: <Node> })
 */

import { h } from "../ui.js";
import { formatNumber } from "../ui.js";

/**
 * @param {object} opts
 * @param {Array<{label:string,value:string|number,unit?:string,note?:string}>} opts.stats 左统计项
 * @param {Array<{label:string,path?:string,icon?:string,onclick?:Function,active?:boolean}>} opts.menu 中菜单项
 * @param {Array|Node} opts.main 右内容
 * @returns {HTMLElement}
 */
export function threeCol({ stats = [], menu = [], main = [] } = {}) {
  return h("div", { class: "threecol" }, [
    // 左：统计卡
    h("aside", { class: "threecol__aside" }, [
      h("div", { class: "stat-card" },
        stats.map((s) =>
          h("div", { class: "stat-item" }, [
            h("div", { class: "stat-card__label" }, s.label),
            h("div", { class: "stat-card__value" }, [
              s.value === null || s.value === undefined || s.value === ""
                ? "—"
                : typeof s.value === "number" ? formatNumber(s.value) : String(s.value),
              s.unit ? h("span", { class: "stat-card__unit" }, s.unit) : null,
            ]),
            s.note ? h("div", { class: "stat-card__note" }, s.note) : null,
          ])
        )
      ),
    ]),
    // 中：垂直菜单卡
    h("nav", { class: "threecol__menu" }, [
      h("div", { class: "menu-card" },
        menu.map((m) =>
          h(m.path ? "a" : "button", {
            class: `menu-card__item${m.active ? " is-active" : ""}`,
            ...(m.path ? { href: `#${m.path}` } : { type: "button" }),
            onclick: m.onclick || null,
          }, [
            h("span", { class: "menu-card__icon" }, m.icon || "•"),
            h("span", { class: "menu-card__label" }, m.label),
          ])
        )
      ),
    ]),
    // 右：内容
    h("section", { class: "threecol__main" }, main),
  ]);
}
