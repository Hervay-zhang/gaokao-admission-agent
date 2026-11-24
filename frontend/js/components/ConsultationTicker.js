/**
 * ConsultationTicker.js —— 首页会移动的咨询悬浮窗
 *
 * 学生 / 家长可点击带链接的咨询进入对应页面；管理员可编辑内容与链接。
 */

import { h, toast } from "../ui.js";
import { api } from "../api.js";
import { getAuthToken, getCurrentUser } from "../auth.js";

let rootRef = null;
let messages = [];
let rotationTimer = null;
let currentIndex = 0;
let floatRaf = 0;
let floatState = null;
let floatingContainer = null;
let pointerPauseHandler = null;

/**
 * 位移写入间隔（毫秒），必须与 CSS 中 #consultation-ticker-root 的 transition 时长一致。
 *
 * 为什么不是每帧写 transform：逐帧修改 transform 会让整个盒子（含 box-shadow、
 * 圆角裁剪、边框）在每帧重新光栅化，Windows Chrome 上表现为持续闪烁。
 * 改成「低频写整数像素 + CSS transition 插值」后，平移由合成器直接搬运位图，
 * 不再触发重绘，视觉上依然连续漂移。
 */
const FLOAT_WRITE_INTERVAL = 120;

function stopFloating() {
  if (floatRaf) cancelAnimationFrame(floatRaf);
  floatRaf = 0;
  floatState = null;
}

function startFloating(root, container) {
  stopFloating();
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (reducedMotion) return;

  const rect = root.getBoundingClientRect();
  const margin = 12;
  const navBottom = 80;
  const bottomSafe = 150;
  const state = {
    x: 0,
    y: 0,
    vx: 0.026,
    vy: 0.018,
    baseX: rect.left,
    baseY: rect.top,
    width: rect.width,
    height: rect.height,
    last: performance.now(),
    lastWrite: 0,
    pauseUntil: 0,
  };
  const pauseBriefly = () => {
    state.pauseUntil = performance.now() + 1500;
  };
  floatingContainer = container;
  pointerPauseHandler = pauseBriefly;
  container.addEventListener("pointerenter", pauseBriefly);
  container.addEventListener("pointerdown", pauseBriefly);

  const updateBounds = () => {
    state.width = root.offsetWidth;
    state.height = root.offsetHeight;
    state.minX = margin - state.baseX;
    state.maxX = Math.max(state.minX, window.innerWidth - state.width - margin - state.baseX);
    state.minY = navBottom + margin - state.baseY;
    state.maxY = Math.max(
      state.minY,
      window.innerHeight - state.height - bottomSafe - state.baseY,
    );
    state.x = Math.min(state.maxX, Math.max(state.minX, state.x));
    state.y = Math.min(state.maxY, Math.max(state.minY, state.y));
  };
  updateBounds();
  floatState = state;

  const tick = (now) => {
    const dt = Math.min(40, Math.max(0, now - state.last));
    state.last = now;
    if (now >= state.pauseUntil) {
      state.x += state.vx * dt;
      state.y += state.vy * dt;
      if (state.x <= state.minX || state.x >= state.maxX) {
        state.x = Math.min(state.maxX, Math.max(state.minX, state.x));
        state.vx *= -1;
      }
      if (state.y <= state.minY || state.y >= state.maxY) {
        state.y = Math.min(state.maxY, Math.max(state.minY, state.y));
        state.vy *= -1;
      }
      // 低频写整像素（理由见 FLOAT_WRITE_INTERVAL 注释），插值交给 CSS transition。
      if (now - state.lastWrite >= FLOAT_WRITE_INTERVAL) {
        state.lastWrite = now;
        root.style.transform =
          `translate3d(${Math.round(state.x)}px, ${Math.round(state.y)}px, 0)`;
      }
    }
    floatRaf = requestAnimationFrame(tick);
  };
  floatRaf = requestAnimationFrame(tick);
}

export function destroyConsultationTicker() {
  clearInterval(rotationTimer);
  rotationTimer = null;
  stopFloating();
  if (floatingContainer && pointerPauseHandler) {
    floatingContainer.removeEventListener("pointerenter", pointerPauseHandler);
    floatingContainer.removeEventListener("pointerdown", pointerPauseHandler);
  }
  floatingContainer = null;
  pointerPauseHandler = null;
  rootRef = null;
}

if (typeof window !== "undefined") {
  window.addEventListener("pageshow", () => {
    if (floatState) floatState.pauseUntil = performance.now() + 400;
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && floatState) floatState.pauseUntil = performance.now() + 400;
  });
}

function normalizeItems(items) {
  return (items || [])
    .map((item) => ({
      content: String(item?.content || "").trim(),
      url: String(item?.url || "").trim(),
    }))
    .filter((item) => item.content);
}

function viewportNode(item, isAdmin) {
  const hasLink = Boolean(item?.url);
  const clickable = hasLink && !isAdmin;
  const children = [
    h("span", { class: "consultation-ticker__dot", "aria-hidden": "true" }),
    h("span", { class: "consultation-ticker__message" }, item?.content || "暂无咨询，管理员可点击编辑发布。"),
  ];
  if (clickable) {
    return h("a", {
      class: "consultation-ticker__viewport is-link",
      href: item.url,
      target: "_blank",
      rel: "noopener noreferrer",
      title: "点击查看详情",
    }, children);
  }
  return h("div", {
    class: `consultation-ticker__viewport${hasLink && isAdmin ? " has-link" : ""}`,
  }, children);
}

function openEditor() {
  const backdrop = h("div", { class: "modal-backdrop" });
  const modal = h("div", { class: "modal consultation-editor" });
  const rows = messages.length ? messages.map((item) => ({ ...item })) : [{ content: "", url: "" }];
  const rowsHost = h("div", { class: "consultation-editor__rows" });

  function renderRows() {
    rowsHost.replaceChildren(
      ...rows.map((item, index) =>
        h("div", { class: "consultation-editor__row" }, [
          h("div", { class: "consultation-editor__fields" }, [
            h("input", {
              class: "consultation-editor__input",
              type: "text",
              maxlength: 160,
              placeholder: "咨询内容",
              value: item.content,
              oninput: (e) => { rows[index].content = e.target.value; },
            }),
            h("input", {
              class: "consultation-editor__input consultation-editor__input--url",
              type: "url",
              maxlength: 512,
              placeholder: "https://链接（可选）",
              value: item.url,
              oninput: (e) => { rows[index].url = e.target.value; },
            }),
          ]),
          h("button", {
            class: "consultation-editor__remove",
            type: "button",
            title: "删除这条咨询",
            onclick: () => {
              rows.splice(index, 1);
              if (!rows.length) rows.push({ content: "", url: "" });
              renderRows();
            },
          }, "×"),
        ])
      )
    );
  }

  const close = () => {
    backdrop.remove();
    modal.remove();
  };
  const save = async () => {
    const items = rows
      .map((item) => ({ content: item.content.trim(), url: item.url.trim() }))
      .filter((item) => item.content);
    const invalid = items.find((item) => {
      if (!item.url) return false;
      try {
        const parsed = new URL(item.url);
        return !["http:", "https:"].includes(parsed.protocol);
      } catch {
        return true;
      }
    });
    if (invalid) {
      toast("链接需要填写完整的 http:// 或 https:// 地址", "error");
      return;
    }
    try {
      const result = await api.adminUpdateConsultations(getAuthToken(), items);
      messages = normalizeItems(result.items);
      close();
      await renderConsultationTicker(rootRef);
      toast("咨询浮窗已更新", "success");
    } catch (e) {
      toast(`保存失败：${e.message}`, "error");
    }
  };

  modal.replaceChildren(
    h("div", { class: "modal__header" }, [
      h("h3", { class: "modal__title" }, "编辑咨询悬浮窗"),
      h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
    ]),
    h("div", { class: "modal__body" }, [
      h("p", { class: "consultation-editor__hint" },
        "每条咨询可单独填写跳转链接；学生端和家长端点击悬浮窗即可打开。"),
      rowsHost,
      h("button", {
        class: "btn btn--ghost btn--sm consultation-editor__add",
        type: "button",
        onclick: () => {
          if (rows.length >= 10) {
            toast("最多添加 10 条咨询", "info");
            return;
          }
          rows.push({ content: "", url: "" });
          renderRows();
        },
      }, "添加一条"),
    ]),
    h("div", { class: "modal__footer" }, [
      h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "取消"),
      h("button", { class: "btn btn--primary", type: "button", onclick: save }, "保存并发布"),
    ])
  );
  renderRows();
  document.body.appendChild(backdrop);
  document.body.appendChild(modal);
}

function startRotation(container, isAdmin) {
  clearInterval(rotationTimer);
  if (messages.length <= 1) return;
  rotationTimer = setInterval(() => {
    currentIndex = (currentIndex + 1) % messages.length;
    const current = container.querySelector(".consultation-ticker__viewport");
    if (current) current.replaceWith(viewportNode(messages[currentIndex], isAdmin));
  }, 6500);
}

export async function renderConsultationTicker(root) {
  if (!root) return;
  rootRef = root;
  try {
    const data = await api.consultations();
    messages = normalizeItems(data.items);
  } catch (e) {
    console.warn("[consultation-ticker] 加载失败：", e.message);
    messages = [];
  }

  const user = getCurrentUser();
  const isAdmin = user?.role === "admin";
  const visible = messages.length > 0 || isAdmin;
  root.classList.toggle("is-admin", isAdmin);
  root.classList.toggle("is-hidden", !visible);
  if (!visible) {
    clearInterval(rotationTimer);
    stopFloating();
    return;
  }
  if (currentIndex >= messages.length) currentIndex = 0;

  const container = h("section", {
    class: "consultation-ticker",
    "aria-label": "移动咨询悬浮窗",
    "aria-live": "polite",
  }, [
    h("div", { class: "consultation-ticker__label" }, [
      h("span", { class: "consultation-ticker__bell", "aria-hidden": "true" }, "!"),
      h("span", {}, "咨询速递"),
    ]),
    viewportNode(messages[currentIndex], isAdmin),
    isAdmin
      ? h("button", {
          class: "consultation-ticker__edit",
          type: "button",
          onclick: openEditor,
        }, "编辑")
      : null,
  ]);

  root.replaceChildren(container);
  startRotation(container, isAdmin);
  if (isAdmin) {
    stopFloating();
    root.style.transform = "translate3d(0, 0, 0)";
  } else {
    startFloating(root, container);
  }
}
