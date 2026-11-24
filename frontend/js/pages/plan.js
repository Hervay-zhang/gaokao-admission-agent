/**
 * pages/plan.js —— 志愿表（多方案 / 排序 / 删除 / 容量 / 一键加入）
 *
 * T06 骨架 → T26 全量接线：
 *   - 从报告页「加入志愿表」暂存草稿（state.planDraft）→ 本页消费；
 *   - 多方案切换；新增/删除方案；
 *   - 条目上下移动排序（持久化 seq）+ 改档位（system_tier 只读）+ 删除；
 *   - 容量计数（按省配置，未核实省份显式标注）；
 *   - 所有变更带乐观锁 version：409 → 提示并自动刷新（不静默、不覆盖）。
 *
 * 红线：用户调整不得覆盖 system_tier（后端保证，前端只展示）；超额由后端拦截并提示。
 */

import { h, toast } from "../ui.js";
import { icon } from "../components/Icon.js";
import { pageShell, placeholderCard } from "../components/PageShell.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { exportPlanImage } from "../components/exportImage.js";
import { getState, takePlanDraft, updateProfile } from "../state.js";
import { navigate } from "../router.js";
import { api } from "../api.js";
import { getCurrentUser } from "../auth.js";

const TIER_OPTIONS = [
  { key: "chong", label: "冲" },
  { key: "wen", label: "稳" },
  { key: "bao", label: "保" },
];
const TIER_LABEL = { chong: "冲", wen: "稳", bao: "保" };

let plansCache = [];
let currentPlanId = null;

export default async function plan(mount) {
  const { profile } = getState();
  let profileId = profile.profileId;

  if (!getCurrentUser()) {
    navigate("/login?redirect=/plan");
    return;
  }
  if (!profileId) {
    try {
      const data = await api.myProfile();
      if (data.profile?.id) {
        profileId = data.profile.id;
        updateProfile({ profileId });
      }
    } catch {
      /* 没有服务端档案时继续显示建档引导 */
    }
  }

  const board = h("div", { id: "plan-board" });
  const actionsHost = h("div", { class: "plan-actions" });

  mount.replaceChildren(
    pageShell({
      eyebrow: "第 3 步 / 共 3 步",
      title: "志愿表",
      subtitle: "把心仪的院校专业组加入志愿表，按梯度排序，确认后导出分享。",
      actions: actionsHost,
      body: [board],
    })
  );

  if (!profileId) {
    actionsHost.replaceChildren(
      h("button", { class: "btn btn--primary", onclick: () => navigate("/assessment") }, "去生成方案")
    );
    board.replaceChildren(
      placeholderCard(
        "还没有评估信息：先完成升学评估，系统会按你的位次排冲稳保三档，再挑院校加进来。",
        "尚未建档"
      )
    );
    return;
  }

  await loadAndRender({ profileId, board, actionsHost });
}

async function loadAndRender({ profileId, board, actionsHost }) {
  board.replaceChildren(h("div", { class: "card-list__empty" }, "加载中…"));
  try {
    const resp = await api.listPlans(profileId);
    plansCache = resp.plans || [];
  } catch (e) {
    board.replaceChildren(h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${e.message}`));
    return;
  }

  // 消费报告页草稿：无方案则先建，再逐条加入
  const draft = takePlanDraft();
  if (draft.length) {
    try {
      if (!plansCache.length) {
        const created = await api.createPlan({
          profile_id: profileId, name: "我的志愿表", year: getState().currentYear || 2026,
        });
        plansCache = [created];
      }
      currentPlanId = currentPlanId || plansCache[0].id;
      let version = (plansCache.find((p) => p.id === currentPlanId) || plansCache[0]).version;
      for (const item of draft) {
        const updated = await api.addPlanItem(currentPlanId, version, item);
        version = updated.version;
      }
      toast(`已加入 ${draft.length} 个院校专业组`, "success");
    } catch (e) {
      toast(`加入志愿表失败：${e.message}`, "error");
    }
    return loadAndRender({ profileId, board, actionsHost });
  }

  renderBoard({ profileId, board, actionsHost });
}

function renderBoard({ profileId, board, actionsHost }) {
  if (!plansCache.length) {
    actionsHost.replaceChildren(
      h("button", { class: "btn btn--primary", onclick: () => createPlan(profileId, board, actionsHost) }, "新建方案")
    );
    board.replaceChildren(
      placeholderCard(
        "还没有志愿表：去「路径报告」把心仪的院校专业组加入，或点右上角新建一个空方案。",
        "暂无方案"
      )
    );
    return;
  }

  if (!currentPlanId || !plansCache.some((p) => p.id === currentPlanId)) {
    currentPlanId = plansCache[0].id;
  }
  const plan = plansCache.find((p) => p.id === currentPlanId);

  // 多方案切换（下拉）
  const switcher = h("select", { class: "filter-bar__input plan-switcher" },
    plansCache.map((p) => h("option", { value: String(p.id), selected: p.id === currentPlanId }, `${p.name}（${p.count}）`))
  );
  switcher.addEventListener("change", () => {
    currentPlanId = Number(switcher.value);
    renderBoard({ profileId, board, actionsHost });
  });

  actionsHost.replaceChildren(
    switcher,
    h("button", { class: "btn btn--ghost", onclick: () => createPlan(profileId, board, actionsHost) }, "新建方案"),
    h("button", { class: "btn btn--ghost", onclick: () => removePlan(plan.id, board, actionsHost) }, "删除方案"),
    h("button", { class: "btn btn--ghost", onclick: () => doExport(plan) }, "导出图片"),
    h("button", { class: "btn btn--ghost", onclick: () => doShare(plan) }, "生成分享链接"),
    plan.share_token
      ? h("button", { class: "btn btn--ghost", onclick: () => doRevoke(plan) }, "撤销分享")
      : null,
    h("button", { class: "btn btn--primary", onclick: () => navigate("/report") }, "去报告加选")
  );

  // 容量条
  const full = plan.count >= plan.capacity;
  const capBar = h("div", { class: "plan-capacity" }, [
    h("span", { class: "plan-capacity__num" }, `${plan.count} / ${plan.capacity}`),
    h("span", { class: "plan-capacity__label" }, "个院校专业组"),
    plan.capacity_verified
      ? null
      : h("span", { class: "plan-capacity__flag" }, `（${plan.province || "该省"}容量按默认值，待核实）`),
    full ? h("span", { class: "plan-capacity__warn" }, "已达容量上限，请先删除后再添加") : null,
  ]);

  board.replaceChildren(
    capBar,
    plan.items.length
      ? h("div", { class: "plan-list" }, plan.items.map((it, idx) => itemRow(it, idx, plan, board, actionsHost)))
      : h("div", { class: "card-list__empty" }, "志愿表为空：去「路径报告」把院校专业组加入进来。"),
    h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT),
  );
}

function itemRow(it, idx, plan, board, actionsHost) {
  const tierSelect = h("select", { class: "plan-row__tier" },
    TIER_OPTIONS.map((t) => h("option", { value: t.key, selected: it.tier === t.key }, t.label))
  );
  tierSelect.addEventListener("change", () =>
    mutate(board, actionsHost, () =>
      api.updatePlanItem(plan.id, it.id, { version: plan.version, tier: tierSelect.value })
    )
  );

  return h("div", { class: "plan-row" }, [
    h("span", { class: "plan-row__seq" }, String(idx + 1)),
    h("div", { class: "plan-row__body" }, [
      h("div", { class: "plan-row__name" }, [
        `${it.university_name}（专业组 ${it.group_code ?? "—"}）`,
        it.user_adjusted ? h("span", { class: "plan-row__flag" }, "已手动调整") : null,
      ]),
      h("div", { class: "plan-row__meta" },
        `系统档位 ${TIER_LABEL[it.system_tier] || "—"}｜当前档位 ${TIER_LABEL[it.tier] || "—"}`),
    ]),
    tierSelect,
    h("div", { class: "plan-row__ops" }, [
      h("button", { class: "icon-btn", title: "上移", disabled: idx === 0, onclick: () => move(plan, idx, -1, board, actionsHost) }, "↑"),
      h("button", { class: "icon-btn", title: "下移", disabled: idx === plan.items.length - 1, onclick: () => move(plan, idx, 1, board, actionsHost) }, "↓"),
      h("button", { class: "icon-btn icon-btn--danger", title: "删除", onclick: () =>
        mutate(board, actionsHost, () => api.deletePlanItem(plan.id, it.id, plan.version)) }, [
        h("i", { class: "icon-slot", html: icon("trash", { size: 14 }) }),
      ]),
    ]),
  ]);
}

/** 上/下移：交换顺序后整体重排（持久化）。 */
function move(plan, idx, delta, board, actionsHost) {
  const ids = plan.items.map((it) => it.id);
  const j = idx + delta;
  if (j < 0 || j >= ids.length) return;
  [ids[idx], ids[j]] = [ids[j], ids[idx]];
  return mutate(board, actionsHost, () => api.reorderPlan(plan.id, plan.version, ids));
}

/** 统一变更入口：409 乐观锁冲突 → 提示并刷新；其它错误 toast。 */
async function mutate(board, actionsHost, fn) {
  try {
    const updated = await fn();
    const pos = plansCache.findIndex((p) => p.id === updated.id);
    if (pos >= 0) plansCache[pos] = updated;
    renderBoard({ profileId: updated.profile_id, board, actionsHost });
  } catch (e) {
    if (e.status === 409) {
      toast("志愿表已被其他操作修改，已为你刷新最新数据", "error");
      const { profile } = getState();
      await loadAndRender({ profileId: profile.profileId, board, actionsHost });
      return;
    }
    const code = (e.payload && e.payload.detail && e.payload.detail.code) || "";
    if (code === "PLAN_CAPACITY_EXCEEDED") {
      toast(e.payload.message || "志愿表已满", "error");
      return;
    }
    toast(`操作失败：${e.message}`, "error");
  }
}

async function createPlan(profileId, board, actionsHost) {
  const name = (window.prompt("方案名称", "我的志愿表") || "").trim() || "我的志愿表";
  try {
    const created = await api.createPlan({ profile_id: profileId, name, year: getState().currentYear || 2026 });
    plansCache.push(created);
    currentPlanId = created.id;
    toast("已创建方案", "success");
    renderBoard({ profileId, board, actionsHost });
  } catch (e) {
    toast(`创建失败：${e.message}`, "error");
  }
}

async function removePlan(planId, board, actionsHost) {
  if (!window.confirm("确定删除该志愿表？该操作不可恢复。")) return;
  const { profile } = getState();
  try {
    await api.deletePlan(planId);
    plansCache = plansCache.filter((p) => p.id !== planId);
    currentPlanId = plansCache.length ? plansCache[0].id : null;
    toast("已删除方案", "success");
    renderBoard({ profileId: profile.profileId, board, actionsHost });
  } catch (e) {
    toast(`删除失败：${e.message}`, "error");
  }
}

/** 导出长图（本地 Canvas 生成；图中只含分数段/位次段）。 */
async function doExport(plan) {
  if (!plan.items.length) {
    toast("志愿表为空，无需导出", "error");
    return;
  }
  const { profile } = getState();
  try {
    await exportPlanImage({
      plan,
      candidate: {
        subject_type: profile.subjectType || "",
        score_bucket: profile.scoreBucket || band(profile.score, 10),
        rank_bucket: profile.rankBucket || band(profile.rank, 1000),
      },
    });
    toast("图片已生成，请查看浏览器下载", "success");
  } catch (e) {
    toast(`导出失败：${e.message}`, "error");
  }
}

/** 本地兜底：由明文分数/位次换算展示用区间（仅用于导出图，不落库）。 */
function band(value, step) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  const lo = Math.floor(n / step) * step;
  return `${lo}-${lo + step - 1}`;
}

/** 生成只读分享链接（服务端脱敏快照，默认 7 天）。 */
async function doShare(plan) {
  if (!plan.items.length) {
    toast("志愿表为空，无法分享", "error");
    return;
  }
  const ttl = h("select", { class: "field-select" }, [
    h("option", { value: "1" }, "1 天"),
    h("option", { value: "7", selected: true }, "7 天"),
    h("option", { value: "30" }, "30 天"),
  ]);
  const backdrop = h("div", { class: "modal-backdrop" });
  const modal = h("div", { class: "modal share-dialog" });
  const resultHost = h("div", { class: "share-dialog__result" });
  const close = () => {
    backdrop.remove();
    modal.remove();
  };
  const create = async () => {
    try {
      const res = await api.createShare(plan.id, Number(ttl.value));
      const url = `${location.origin}${location.pathname}${res.share_path}`;
      await import("../../vendor/qrcode-generator.js");
      const qrCode = window.qrcode(0, "M");
      qrCode.addData(url);
      qrCode.make();
      const qr = qrCode.createDataURL(6, 2);
      resultHost.replaceChildren(
        h("div", { class: "share-dialog__qr" }, h("img", { src: qr, alt: "分享链接二维码" })),
        h("input", { class: "field-input", readonly: true, value: url }),
        h("div", { class: "share-dialog__hint" }, `有效期至 ${res.expires_at}`),
        h("button", {
          class: "btn btn--primary",
          type: "button",
          onclick: async () => {
            try {
              await navigator.clipboard?.writeText(url);
              toast("分享链接已复制", "success");
            } catch {
              window.prompt("请复制分享链接：", url);
            }
          },
        }, "复制链接"),
      );
      plan.share_token = res.token;
      plan.share_expires_at = res.expires_at;
    } catch (e) {
      toast(`生成分享链接失败：${e.message}`, "error");
    }
  };
  modal.replaceChildren(
    h("div", { class: "modal__header" }, [
      h("h3", { class: "modal__title" }, "生成只读分享链接"),
      h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
    ]),
    h("div", { class: "modal__body" }, [
      h("div", { class: "field-group" }, [
        h("label", { class: "field-label" }, "链接有效期"),
        ttl,
      ]),
      resultHost,
    ]),
    h("div", { class: "modal__footer" }, [
      h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "关闭"),
      h("button", { class: "btn btn--primary", type: "button", onclick: create }, "生成链接与二维码"),
    ])
  );
  document.body.appendChild(backdrop);
  document.body.appendChild(modal);
}

async function doRevoke(plan) {
  if (!plan.share_token) return;
  if (!window.confirm("撤销后，该分享链接会立即失效。确定撤销吗？")) return;
  try {
    await api.revokeShare(plan.share_token);
    toast("分享链接已撤销", "success");
    location.reload();
  } catch (e) {
    toast(`撤销失败：${e.message}`, "error");
  }
}
