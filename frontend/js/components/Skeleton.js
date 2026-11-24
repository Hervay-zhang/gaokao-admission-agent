/**
 * Skeleton.js —— 骨架屏（加载态占位）
 *
 * 红线：骨架屏只是**视觉加载态**，不代表「功能未实现」。
 *       功能未实现须用 `PageShell.placeholderCard` 显式标注。
 */

import { h } from "../ui.js";

/**
 * 条形骨架。
 * @param {object} [opts]
 * @param {string} [opts.width] CSS 宽度，默认 100%
 * @param {string} [opts.height] CSS 高度，默认 16px
 * @param {string} [opts.radius]
 */
export function skeletonBar({ width = "100%", height = "16px", radius = "var(--radius-sm)" } = {}) {
  return h("div", {
    class: "skeleton",
    style: `width:${width};height:${height};border-radius:${radius};`,
    "aria-hidden": "true",
  });
}

/**
 * 卡片骨架（标题 + 若干行 + 可选按钮）。
 * @param {object} [opts]
 * @param {number} [opts.lines]
 * @param {boolean} [opts.withButton]
 */
export function skeletonCard({ lines = 3, withButton = false } = {}) {
  const body = [
    skeletonBar({ width: "42%", height: "20px" }),
    ...Array.from({ length: lines }, (_, i) =>
      skeletonBar({ width: i % 2 === 0 ? "100%" : "86%", height: "13px" })
    ),
  ];
  if (withButton) body.push(skeletonBar({ width: "132px", height: "38px", radius: "999px" }));

  return h("div", { class: "card skeleton-card", "data-skeleton": "true", "aria-busy": "true" },
    h("div", { class: "skeleton-card__body", style: "display:flex;flex-direction:column;gap:12px;" }, body)
  );
}

/**
 * 一组骨架卡（列表加载态）。
 * @param {number} [count]
 */
export function skeletonList(count = 3) {
  return h("div", { class: "skeleton-list", style: "display:grid;gap:var(--space-4);" },
    Array.from({ length: count }, () => skeletonCard())
  );
}
