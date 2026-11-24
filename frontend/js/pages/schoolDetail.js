/**
 * pages/schoolDetail.js —— 院校详情（T24）
 *
 * 路由 `#/schools/:code?province=&subject_type=`：展示院校基本信息 +
 * 按**专业组**聚合的历年投档线（位次趋势）+ 选科要求（缺失显式标「待补齐」）。
 *
 * 红线：数据全部来自库内；缺年不补造（显式标注）；投档线≠录取线（口径常驻）。
 */

import { h } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { api } from "../api.js";
import { navigate, parseQuery } from "../router.js";

export default async function schoolDetail(mount, { param, query = {} } = {}) {
  const code = param;
  const province = query.province || "广东";
  const subjectType = query.subject_type || "";

  mount.replaceChildren(
    pageShell({
      eyebrow: "院校与专业库",
      title: "院校详情",
      subtitle: "加载中…",
      body: [h("div", { class: "skeleton-list" }, "加载院校详情…")],
    })
  );

  let data;
  try {
    data = await api.schoolDetail(code, { province, subject_type: subjectType });
  } catch (e) {
    mount.replaceChildren(
      pageShell({
        eyebrow: "院校与专业库",
        title: "院校详情",
        subtitle: "",
        body: [
          h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${e.message}`),
          h("div", { class: "page-head__actions" },
            h("button", { class: "btn", onclick: () => navigate("/schools") }, "返回列表")),
        ],
      })
    );
    return;
  }

  const tagChips = (data.tags || []).map((t) => h("span", { class: "card__tag" }, t));
  if (data.meta_status === "待补") tagChips.push(h("span", { class: "card__tag card__tag--warn" }, "元数据待补"));

  const isDirectOfficial = data.admission_url_kind === "school";
  const buildOfficialLink = () => data.admission_url
    ? h("a", {
        class: "btn btn--primary uni-official-link",
        href: data.admission_url,
        target: "_blank",
        rel: "noopener noreferrer",
      }, [isDirectOfficial ? "学校招生官网 " : "阳光高考招生查询 ", h("span", {}, "↗")])
    : null;

  const header = h("section", { class: "uni-profile" }, [
    h("div", { class: "uni-profile__seal" }, data.name.slice(0, 1)),
    h("div", { class: "uni-profile__body" }, [
      h("div", { class: "uni-profile__title-row" }, [
        h("h2", {}, data.name),
        h("span", { class: "uni-profile__code" }, `院校代码 ${data.code}`),
        ...tagChips,
      ]),
      h("div", { class: "uni-profile__meta" }, [
        h("span", {}, data.province || "地域待补"),
        h("span", {}, data.school_type || "类型待补"),
        h("span", {}, data.level),
      ]),
      h("p", { class: "uni-profile__note" }, data.trend_note || ""),
    ]),
    h("div", { class: "uni-profile__actions" }, [
      buildOfficialLink(),
      h("span", { class: "uni-profile__source" },
        isDirectOfficial ? "学校官方招生网站" : "教育部阳光高考院校库"),
    ]),
  ]);

  const groups = (data.groups || []).length
    ? data.groups.map((g) => renderGroup(g))
    : [h("div", { class: "card-list__empty" },
        `库内暂未收录该院校在${province}${subjectType ? "·" + subjectType : ""}的投档数据。`)];

  const body = [
    header,
    h("div", { class: "tier-legend" }, [
      h("span", { class: "chip chip--soft is-active" }, `生源省份：${province}`),
      subjectType ? h("span", { class: "chip chip--soft" }, `科类：${subjectType}`) : null,
    ]),
    h("h3", { class: "card-list__title" }, "专业组历年投档"),
    ...groups,
    h("div", { class: "chat-disclaimer" }, data.caliber_text || ""),
    h("div", { class: "page-head__actions" }, [
      h("button", { class: "btn", onclick: () => navigate("/schools") }, "← 返回列表"),
      h("button", {
        class: "btn btn--primary",
        onclick: () => navigate(`/schools/compare?codes=${data.code}&province=${encodeURIComponent(province)}&subject_type=${encodeURIComponent(subjectType)}`),
      }, "加入对比"),
      buildOfficialLink(),
    ]),
  ];

  mount.replaceChildren(
    pageShell({
      eyebrow: "院校与专业库",
      title: data.name,
      subtitle: `院校代码 ${data.code} · ${data.province || "地域待补"} · ${data.level}`,
      actions: [
        h("button", {
          class: "btn btn--ghost",
          onclick: () => navigate("/schools"),
        }, "← 返回院校与专业库"),
      ],
      body,
    })
  );
}

/** 单个专业组卡：选科要求 + 历年投档表。 */
function renderGroup(g) {
  const rows = (g.lines || []).map((ln) =>
    h("tr", {}, [
      h("td", {}, String(ln.year)),
      h("td", {}, String(ln.group_code)),
      h("td", {}, ln.min_score != null ? String(ln.min_score) : "—"),
      h("td", {}, ln.min_rank != null ? String(ln.min_rank) : "—"),
      h("td", {}, ln.plan_count != null ? String(ln.plan_count) : "—"),
      h("td", {}, ln.admitted_count != null ? String(ln.admitted_count) : "—"),
    ])
  );
  return h("div", { class: "card group-card" }, [
    h("div", { class: "group-card__head" }, [
      h("span", { class: "card__tag" }, `${g.subject_type} · 专业组 ${g.group_code}`),
      g.require_status === "待补"
        ? h("span", { class: "card__tag card__tag--warn" }, "选科要求待补")
        : null,
    ]),
    h("div", { class: "score-input__hint" }, g.requirement_text),
    h("div", { class: "fallback-table-wrap" }, [
      h("table", { class: "fallback-table" }, [
        h("thead", {}, [
          h("tr", {}, [
            h("th", {}, "年份"), h("th", {}, "组"), h("th", {}, "投档最低分"),
            h("th", {}, "最低位次"), h("th", {}, "计划数"), h("th", {}, "投档人数"),
          ]),
        ]),
        h("tbody", {}, rows),
      ]),
    ]),
  ]);
}
