/**
 * FeedbackBar.js —— 反馈控件（T22）
 *
 * 职责
 * ----
 * 1. 仅在流式回答**结束后**由 ChatStream 挂载（done 事件），生成过程中不出现；
 * 2. 👍 一键提交；👎 先选**固定枚举**原因（推荐不准/数据错误/回答跑题/其他）再提交；
 * 3. 「纠错」按钮展开可选填文本（≤2000 字）提交 correction；
 * 4. 一条输出仅一次反馈：提交成功（或后端 409 判重）后整个控件**锁定**，不可再改；
 * 5. 不采集任何身份信息（仅匿名 session_sid + 后端返回的 message_id）。
 *
 * 红线
 * ----
 * - 点踩原因必须走固定枚举，杜绝自由文本注入面（与后端 ALLOWED_REASONS 一致）；
 * - 无 message_id（无会话落库）时不上报，前端直接提示，不伪造请求。
 * - 409 FEEDBACK_DUPLICATE → 提示「该回答已反馈过」并锁定，不重复入库。
 */

import { h } from "../ui.js";
import { api } from "../api.js";
import { icon } from "./Icon.js";

/** 点踩原因枚举（与后端 schemas/feedback.py DOWN_REASONS 严格一致）。 */
export const DOWN_REASONS = ["推荐不准", "数据错误", "回答跑题", "其他"];

/**
 * 创建反馈条。
 *
 * @param {object} opts
 * @param {number|null} opts.messageId done 事件返回的消息 id（可为 null）
 * @param {string} opts.sessionSid 匿名会话 ID
 * @param {string} [opts.dimension] 反馈维度：answer（对话回答）/ recommend（报告推荐）
 * @param {boolean} [opts.allowNoMessage] 无 message_id 时是否仍上报（报告页维荐无消息落库）
 * @param {Function} opts.onSubmitted (state: "ok"|"duplicate"|"failed", tip: string) => void
 * @returns {{el: HTMLElement, lock: Function, isLocked: Function}}
 */
export function createFeedbackBar({
  messageId,
  sessionSid,
  dimension = "answer",
  allowNoMessage = false,
  onSubmitted,
} = {}) {
  let locked = false;

  const upBtn = h("button", { class: "chat-feedback__btn", "data-vote": "up", title: "有帮助" }, [
    h("span", { class: "chat-feedback__icon", html: icon("thumbUp", { size: 14 }) }),
    "有帮助",
  ]);
  const downBtn = h("button", { class: "chat-feedback__btn", "data-vote": "down", title: "不准确" }, [
    h("span", { class: "chat-feedback__icon", html: icon("thumbDown", { size: 14 }) }),
    "不准确",
  ]);
  const correctBtn = h("button", { class: "chat-feedback__btn chat-feedback__btn--text", title: "提供正确信息" }, "纠错");

  // 👎 展开面板：原因下拉（固定枚举）+ 提交
  const reasonSelect = h("select", { class: "chat-feedback__select" }, [
    h("option", { value: "" }, "请选择原因"),
    ...DOWN_REASONS.map((r) => h("option", { value: r }, r)),
  ]);
  const downPanel = h(
    "div",
    { class: "chat-feedback__panel chat-feedback__panel--down" },
    [reasonSelect, h("button", { class: "btn btn--sm btn--primary chat-feedback__submit" }, "提交点踩")]
  );

  // 纠错面板：可选填文本（≤2000 字）
  const correctInput = h("textarea", {
    class: "chat-feedback__textarea",
    rows: 2,
    maxlength: 2000,
    placeholder: "正确信息是什么？（选填，帮助改进）",
  });
  const correctPanel = h(
    "div",
    { class: "chat-feedback__panel chat-feedback__panel--correct" },
    [correctInput, h("button", { class: "btn btn--sm btn--primary chat-feedback__submit" }, "提交纠错")]
  );

  const tip = h("div", { class: "chat-feedback__tip" }, "");
  const el = h("div", { class: "chat-feedback" }, [upBtn, downBtn, correctBtn, downPanel, correctPanel, tip]);

  /** 锁定整个控件（提交成功 / 判重后调用）。 */
  function lock(state, text) {
    locked = true;
    el.classList.add("is-locked");
    el.querySelectorAll("button, select, textarea").forEach((n) => (n.disabled = true));
    downPanel.classList.remove("is-open");
    correctPanel.classList.remove("is-open");
    tip.textContent = text;
    tip.classList.add("is-visible");
    if (state === "ok") el.classList.add("is-ok");
    onSubmitted && onSubmitted(state, text);
  }

  async function submit(payload) {
    if (locked) return;
    if (messageId == null && !allowNoMessage) {
      // 无会话消息 id（匿名纯流式），后端无快照可挂——不上报，明确告知
      lock("failed", "该回答未落库，暂不支持反馈。");
      return;
    }
    try {
      await api.submitFeedback({ message_id: messageId, session_sid: sessionSid, ...payload });
      lock("ok", "感谢反馈，我们会持续改进。");
    } catch (e) {
      if (e.status === 409) {
        lock("duplicate", "该回答已反馈过，感谢支持。");
      } else {
        tip.textContent = `提交失败：${e.message}`;
        tip.classList.add("is-visible");
        onSubmitted && onSubmitted("failed", e.message);
      }
    }
  }

  el.addEventListener("click", (e) => {
    if (locked) return;
    const btn = e.target.closest("button");
    if (!btn) return;

    if (btn === upBtn) {
      submit({ kind: "up", dimension });
      return;
    }
    if (btn === downBtn) {
      downPanel.classList.toggle("is-open");
      correctPanel.classList.remove("is-open");
      return;
    }
    if (btn === correctBtn) {
      correctPanel.classList.toggle("is-open");
      downPanel.classList.remove("is-open");
      return;
    }
    if (btn.closest(".chat-feedback__panel--down")) {
      const reason = reasonSelect.value;
      if (!reason) {
        tip.textContent = "请先选择点踩原因。";
        tip.classList.add("is-visible");
        return;
      }
      submit({ kind: "down", dimension, reason });
      return;
    }
    if (btn.closest(".chat-feedback__panel--correct")) {
      const text = correctInput.value.trim();
      if (!text) {
        tip.textContent = "请填写纠错内容。";
        tip.classList.add("is-visible");
        return;
      }
      submit({ kind: "correction", dimension, correction: text });
      return;
    }
  });

  return { el, lock, isLocked: () => locked };
}
