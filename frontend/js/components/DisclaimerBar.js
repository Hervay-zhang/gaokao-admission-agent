/**
 * DisclaimerBar.js —— 免责声明条（常驻，不可折叠、不可隐藏）
 * 红线：所有 AI 输出必须带此声明。
 */

import { h } from "../ui.js";

export const DISCLAIMER_TEXT = "本建议仅供参考，不作为最终填报承诺。请以官方发布的招生政策与数据为准。";

export function renderDisclaimerBar() {
  const root = document.getElementById("disclaimer-root");
  if (!root) return;
  root.replaceChildren(
    h("div", { class: "disclaimer-bar", role: "note" }, DISCLAIMER_TEXT)
  );
}
