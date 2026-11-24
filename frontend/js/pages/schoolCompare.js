/**
 * pages/schoolCompare.js —— 院校对比（T24，≤3 所）
 *
 * 路由 `#/schools/compare?codes=10001,10003&province=广东&subject_type=物理`
 * 展示：基本信息行 + 历年最低投档位次趋势（缺年留空，不补造）。
 *
 * 红线：数据全部来自库内；缺年不补造；投档线≠录取线。
 */

import { h } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { api } from "../api.js";
import { navigate } from "../router.js";

export default async function schoolCompare(mount, { query = {} } = {}) {
  const codes = (query.codes || "").split(",").map((c) => c.trim()).filter(Boolean);
  const province = query.province || "广东";
  const subjectType = query.subject_type || "";

  mount.replaceChildren(
    pageShell({
      eyebrow: "院校与专业库",
      title: "院校对比",
      subtitle: "加载中…",
      body: [h("div", { class: "skeleton-list" }, "加载对比数据…")],
    })
  );

  if (!codes.length) {
    mount.replaceChildren(
      pageShell({
        eyebrow: "院校与专业库",
        title: "院校对比",
        subtitle: "",
        body: [
          h("div", { class: "card-list__empty" }, "尚未选择院校，请先在院校库勾选 2–3 所。"),
          h("button", { class: "btn btn--primary", onclick: () => navigate("/schools") }, "去院校库选择"),
        ],
      })
    );
    return;
  }

  let data;
  try {
    data = await api.compareSchools(codes, { province, subject_type: subjectType });
  } catch (e) {
    mount.replaceChildren(
      pageShell({
        eyebrow: "院校与专业库",
        title: "院校对比",
        subtitle: "",
        body: [
          h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${e.message}`),
          h("button", { class: "btn", onclick: () => navigate("/schools") }, "返回院校库"),
        ],
      })
    );
    return;
  }

  const years = data.years || [];
  const rows = data.rows || [];

  // 表头：指标列 + 各院校
  const headRow = h("tr", {}, [h("th", {}, "指标"), ...rows.map((r) => h("th", {}, r.name))]);
  const bodyRows = [
    ["院校代码", ...rows.map((r) => r.code)],
    ["层次标签", ...rows.map((r) => (r.tags || []).join(" / ") || "—")],
    ["所在地", ...rows.map((r) => r.province || "待补")],
    ["类型", ...rows.map((r) => r.school_type || "待补")],
    ["专业组数", ...rows.map((r) => String(r.group_count))],
    ["最近一年最低位次", ...rows.map((r) => (r.latest_min_rank != null ? String(r.latest_min_rank) : "数据待补"))],
    ["数据完整度", ...rows.map((r) => (r.meta_status === "已核" ? "已核" : "待补"))],
    ...years.map((y, i) => [
      `${y} 年最低位次`,
      ...rows.map((r) => (r.rank_trend[i] != null ? String(r.rank_trend[i]) : "—")),
    ]),
  ].map((cells) =>
    h("tr", {}, cells.map((c, i) => h(i === 0 ? "th" : "td", { class: i === 0 ? "compare__label" : "" }, c)))
  );

  const table = h("div", { class: "fallback-table-wrap" }, [
    h("table", { class: "fallback-table compare-table" }, [
      h("thead", {}, headRow),
      h("tbody", {}, bodyRows),
    ]),
  ]);

  // 位次趋势小结（文字，明确「位次越小越好」）
  const trendNotes = rows.map((r) => {
    const vals = r.rank_trend.filter((v) => v != null);
    if (vals.length < 2) {
      return h("li", {}, `${r.name}：可用年份不足 2 年，暂不做趋势判断（不推算）。`);
    }
    const dir = vals[0] - vals[vals.length - 1]; // 位次减小 = 竞争加剧
    const txt = dir > 0 ? "位次收紧（竞争加剧）" : dir < 0 ? "位次放宽（竞争缓和）" : "位次基本持平";
    return h("li", {}, `${r.name}：${vals.map(String).join(" → ")}（${txt}，位次越小越优先）。`);
  });

  mount.replaceChildren(
    pageShell({
      eyebrow: "院校与专业库",
      title: "院校对比",
      subtitle: `生源省份 ${province}${subjectType ? " · " + subjectType : ""} · 共 ${rows.length} 所`,
      body: [
        table,
        h("h3", { class: "card-list__title" }, "位次趋势小结"),
        h("ul", { class: "note-card__list" }, trendNotes),
        h("div", { class: "chat-disclaimer" }, data.caliber_text || ""),
        h("div", { class: "page-head__actions" }, [
          h("button", { class: "btn", onclick: () => navigate("/schools") }, "← 返回院校库"),
        ]),
      ],
    })
  );
}
