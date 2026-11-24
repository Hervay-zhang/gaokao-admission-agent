/**
 * pages/shareView.js —— 只读分享页（T28）
 *
 * 路由 `#/share/:token`：
 *   - `GET /api/share/{token}` 取**脱敏**只读快照（分数段/位次段）；
 *   - **只读**：本页不渲染任何编辑/删除/反馈控件，也不发起任何写请求；
 *   - 过期（410 SHARE_EXPIRED）→ 提示链接已过期；不存在（404）→ 提示链接无效。
 *
 * 红线：分享页不含明文分数/位次与个人身份；不可编辑；不可反馈写库。
 */

import { h } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { navigate } from "../router.js";
import { api } from "../api.js";

const TIER_LABEL = { chong: "冲", wen: "稳", bao: "保" };
const TIER_CLASS = { chong: "is-chong", wen: "is-wen", bao: "is-bao" };

export default async function shareView(mount, { param } = {}) {
  const token = param || "";
  const host = h("div", { id: "share-host" }, [h("div", { class: "card-list__empty" }, "加载中…")]);

  mount.replaceChildren(
    pageShell({
      eyebrow: "分享预览",
      title: "志愿表（只读）",
      subtitle: "他人分享给你的志愿表快照，仅供查看。",
      actions: h("button", { class: "btn btn--ghost", onclick: () => navigate("/assessment") }, "我也要测一测"),
      body: [host],
    })
  );

  if (!token) {
    host.replaceChildren(h("div", { class: "chat-notice chat-notice--error" }, "分享链接不完整。"));
    return;
  }

  try {
    const snap = await api.readShare(token);
    render(snap, host);
  } catch (e) {
    const code = (e.payload && e.payload.code) || "";
    if (e.status === 410 || code === "SHARE_EXPIRED") {
      host.replaceChildren(
        h("div", { class: "chat-notice chat-notice--warn" },
          "该分享链接已过期，请联系分享者重新生成。")
      );
    } else {
      host.replaceChildren(
        h("div", { class: "chat-notice chat-notice--error" },
          `分享链接无效或已撤销（${e.message}）。`)
      );
    }
  }
}

function render(snap, host) {
  const cand = snap.candidate || {};
  const band = [
    snap.province || "",
    cand.subject_type ? `${cand.subject_type}类` : "",
    cand.score_bucket ? `${cand.score_bucket} 分` : "",
    cand.rank_bucket ? `位次段 ${cand.rank_bucket}` : "",
  ].filter(Boolean).join(" · ");

  const rows = (snap.items || []).map((it) =>
    h("div", { class: "plan-row plan-row--readonly" }, [
      h("span", { class: "plan-row__seq" }, String(it.seq)),
      h("div", { class: "plan-row__body" }, [
        h("div", { class: "plan-row__name" }, [
          `${it.university_name}（专业组 ${it.group_code ?? "—"}）`,
          it.user_adjusted ? h("span", { class: "plan-row__flag" }, "已手动调整") : null,
        ]),
        h("div", { class: "plan-row__meta" },
          it.notes ? `备注：${it.notes}` : `系统档位 ${TIER_LABEL[it.system_tier] || "—"}`),
      ]),
      h("span", { class: `case-card__type ${TIER_CLASS[it.tier] || ""}` }, TIER_LABEL[it.tier] || "—"),
    ])
  );

  host.replaceChildren(
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, `${snap.plan_name}（${snap.year} 年）`),
      h("p", { class: "note-card__text" }, band || "评估信息未提供"),
      h("div", { class: "case-meta-grid" }, [
        h("span", {}, `共 ${snap.count} 个院校专业组`),
        h("span", {}, `容量 ${snap.capacity}`),
        h("span", {}, `有效期至 ${snap.expires_at}`),
      ]),
    ]),
    h("div", { class: "plan-capacity" }, [
      h("span", { class: "plan-capacity__label" }, "只读预览：不可编辑、不可修改，也不收集任何反馈。"),
    ]),
    snap.items && snap.items.length
      ? h("div", { class: "plan-list" }, rows)
      : h("div", { class: "card-list__empty" }, "该志愿表暂无条目。"),
    h("div", { class: "chat-disclaimer" }, snap.disclaimer || "本方案仅供参考，不作为最终填报承诺。"),
  );
}
