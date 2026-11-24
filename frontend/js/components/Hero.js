/**
 * Hero.js —— 首页 Hero 区（对齐见径首屏）
 * 状态徽章 + 两行衬线大标题（关键词朱砂下划线）+ 粗体关键词副文案 + 双按钮
 */

import { h } from "../ui.js";
import { navigate } from "../router.js";

export function renderHero() {
  return h("section", { class: "hero hero--visual" }, [
    h("div", { class: "container hero__inner" }, [
      // 状态徽章
      h("div", { class: "hero__badge" }, [
        h("span", { class: "hero__badge-dot" }),
        "2027 高考志愿规划系统 · 已上线",
      ]),
      // 两行衬线大标题（关键词用 em 加朱砂下划线）
      h("h1", { class: "hero__title", html:
        "看见现实的刻度，<br>规划人生的<em>旷野</em>。" }),
      // 副文案（粗体关键词）
      h("p", { class: "hero__desc", html:
        "不只是一次志愿填报。把 <strong>分数与位次</strong>、<strong>选科组合</strong> 和家里的实际顾虑放进来，" +
        "对着近三年真实投档数据，排一份看得懂、也调得动的 <strong>冲稳保</strong> 方案。" }),
      // 双按钮
      h("div", { class: "hero__actions" }, [
        h("button", { class: "btn btn--primary", onclick: () => navigate("/assessment") }, ["开始升学评估 ", h("span", {}, "→")]),
        h("button", { class: "btn btn--ghost", onclick: () => navigate("/cases") }, "查看真实规划案例"),
      ]),
    ]),
  ]);
}
