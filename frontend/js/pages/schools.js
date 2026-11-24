/**
 * pages/schools.js —— 院校与专业库（列表检索）
 *
 * T24 落地：真实后端检索（关键词 / 层次标签 / 院校地域 / 分页），**筛选条件进 URL**
 * （`location.hash` query），刷新/分享不丢状态；勾选 ≤3 所进对比页。
 *
 * 红线：数据全部来自库内；无数据维度显式标注（院校「待补」徽章）。
 */

import { h, toast } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { api } from "../api.js";
import { setQuery, navigate } from "../router.js";
import { getState } from "../state.js";
import { PROVINCES } from "../config.js";
import { cardListSkeleton } from "../components/CardList.js";

const TAG_OPTIONS = ["", "985", "211", "双一流"];
const LEVEL_OPTIONS = ["", "本科", "专科"];
const PAGE_SIZE = 12;

/** 对比篮（模块级，仅本次会话内存态；≤3 所）。 */
const compareBasket = new Set();
let schoolsMetaPromise = null;

function loadSchoolsMeta() {
  if (!schoolsMetaPromise) {
    schoolsMetaPromise = api.schoolsMeta().catch(() => ({ uni_provinces: [] }));
  }
  return schoolsMetaPromise;
}

export default async function schools(mount, { query = {} } = {}) {
  const state = getState();
  const profile = state.profile || {};
  const provinceName = provinceNameOf(profile.province);

  // 从 URL 读取筛选（刷新不丢）
  const filter = {
    keyword: query.keyword || "",
    tag: query.tag || "",
    level: query.level || "",
    uni_province: query.uni_province || "",
    subject_type: query.subject_type || profile.subjectType || "",
    page: Number(query.page) || 1,
    province: provinceName,
  };

  const listHost = h("div", { id: "schools-list" }, [cardListSkeleton(6)]);
  const metaPromise = loadSchoolsMeta();

  /** 把当前筛选写进 URL 并重渲染列表。 */
  function applyFilter(patch = {}, { resetPage = true } = {}) {
    Object.assign(filter, patch);
    if (resetPage) filter.page = 1;
    setQuery({ ...filter });
  }

  // ---- 筛选栏 ----
  const keywordInput = h("input", {
    class: "filter-bar__input",
    type: "search",
    placeholder: "搜索院校名称 / 代码",
    value: filter.keyword,
  });
  keywordInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyFilter({ keyword: keywordInput.value.trim() });
  });
  keywordInput.addEventListener("change", () => applyFilter({ keyword: keywordInput.value.trim() }));

  const tagChips = h("div", { class: "chip-row" }, TAG_OPTIONS.map((t) =>
    h("button", {
      class: `chip${filter.tag === t ? " is-active" : ""}`,
      type: "button",
      "data-tag": t,
    }, t || "全部层次")
  ));
  tagChips.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-tag]");
    if (btn) applyFilter({ tag: btn.dataset.tag });
  });

  const levelSelect = h("select", { class: "filter-bar__select" }, LEVEL_OPTIONS.map((l) =>
    h("option", { value: l, selected: filter.level === l }, l || "全部学历")
  ));
  levelSelect.addEventListener("change", () => applyFilter({ level: levelSelect.value }));

  const provinceSelect = h("select", { class: "filter-bar__select" }, [
    h("option", { value: "", selected: !filter.uni_province }, "全部地域"),
  ]);
  provinceSelect.addEventListener("change", () => applyFilter({ uni_province: provinceSelect.value }));

  const subjectSelect = h("select", { class: "filter-bar__select" }, ["", "物理", "历史"].map((s) =>
    h("option", { value: s, selected: filter.subject_type === s }, s || "不限科类")
  ));
  subjectSelect.addEventListener("change", () => applyFilter({ subject_type: subjectSelect.value }));

  const filters = h("div", { class: "filter-bar" }, [
    keywordInput,
    tagChips,
    h("div", { class: "filter-bar__row" }, [levelSelect, provinceSelect, subjectSelect]),
  ]);

  // ---- 对比篮浮条 ----
  const basketBar = h("div", { class: "compare-bar", hidden: true });
  function refreshBasket() {
    const n = compareBasket.size;
    basketBar.hidden = n === 0;
    basketBar.replaceChildren(
      h("span", {}, `已选 ${n} 所（最多 3 所）`),
      h("button", { class: "btn btn--sm btn--primary", onclick: goCompare }, "开始对比"),
      h("button", { class: "btn btn--sm", onclick: () => { compareBasket.clear(); refreshBasket(); rerenderRows(); } }, "清空")
    );
  }
  function goCompare() {
    if (compareBasket.size < 2) {
      toast("请至少选择 2 所院校进行对比", "warn");
      return;
    }
    navigate(`/schools/compare?codes=${[...compareBasket].join(",")}&province=${encodeURIComponent(provinceName)}&subject_type=${encodeURIComponent(filter.subject_type)}`);
  }

  let lastItems = [];
  function rerenderRows() {
    renderRows(listHost, lastItems, filter, compareBasket, refreshBasket);
  }

  // ---- 加载列表 ----
  async function load() {
    listHost.replaceChildren(cardListSkeleton(6));
    try {
      const data = await api.listSchools({
        keyword: filter.keyword,
        tag: filter.tag,
        level: filter.level,
        uni_province: filter.uni_province,
        subject_type: filter.subject_type,
        province: provinceName,
        page: filter.page,
        page_size: PAGE_SIZE,
      });
      lastItems = data.items;
      rerenderRows();
      renderPager(listHost, data, filter, applyFilter);
      refreshBasket();
    } catch (e) {
      listHost.replaceChildren(
        h("div", { class: "chat-notice chat-notice--error" }, `院校数据加载失败：${e.message}`)
      );
    }
  }

  mount.replaceChildren(
    pageShell({
      eyebrow: "院校与专业库",
      title: "院校与专业库",
      subtitle: `基于近三年真实投档数据，按省份、科类与层次筛选可报院校（当前生源省份：${provinceName}）。`,
      body: [filters, basketBar, listHost],
    })
  );

  void metaPromise.then((meta) => {
    provinceSelect.append(...(meta.uni_provinces || []).map((p) =>
      h("option", { value: p, selected: filter.uni_province === p }, p)
    ));
  });

  await load();
  return { reload: load };
}

/** 渲染院校卡片行。 */
function renderRows(host, items, filter, basket, onToggle) {
  if (!items || !items.length) {
    host.replaceChildren(h("div", { class: "card-list__empty" }, "没有符合条件的院校，试试放宽筛选。"));
    return;
  }
  const rows = items.map((u) => {
    const checked = basket.has(u.code);
    const checkbox = h("input", {
      type: "checkbox", class: "school-row__check", checked: checked,
      onclick: (e) => {
        e.stopPropagation();
        if (basket.has(u.code)) basket.delete(u.code);
        else if (basket.size < 3) basket.add(u.code);
        else { e.target.checked = false; toast("最多对比 3 所院校", "warn"); }
        onToggle();
      },
    });
    const tagChips = (u.tags || []).map((t) => h("span", { class: "card__tag" }, t));
    if (u.meta_status === "待补") tagChips.push(h("span", { class: "card__tag card__tag--warn" }, "元数据待补"));

    return h("div", {
      class: "card card--row school-row",
      onclick: () => navigate(`/schools/${u.code}?province=${encodeURIComponent(filter.province || "广东")}&subject_type=${encodeURIComponent(filter.subject_type || "")}`),
    }, [
      checkbox,
      h("div", { class: "card__body" }, [
        h("div", { class: "card__title" }, [`${u.name}（${u.code}）`, ...tagChips]),
        h("div", { class: "card__meta" },
          `${u.province || "地域待补"} · ${u.school_type || "类型待补"} · ${u.level} · ` +
          (u.latest_min_rank != null
            ? `${u.latest_year} 年最低位次 ${u.latest_min_rank}`
            : "近三年位次数据待补") +
          ` · ${u.group_count} 个专业组`),
      ]),
      h("span", { class: "card__arrow", "aria-hidden": "true" }, "›"),
    ]);
  });
  host.replaceChildren(
    h("div", { class: "card-list" }, rows),
  );
}

/** 分页控件。 */
function renderPager(host, data, filter, applyFilter) {
  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (totalPages <= 1) {
    return;
  }
  const pager = h("div", { class: "pager" }, [
    h("button", {
      class: "btn btn--sm", disabled: data.page <= 1,
      onclick: () => applyFilter({ page: data.page - 1 }, { resetPage: false }),
    }, "上一页"),
    h("span", { class: "pager__info" }, `第 ${data.page} / ${totalPages} 页 · 共 ${data.total} 所`),
    h("button", {
      class: "btn btn--sm", disabled: data.page >= totalPages,
      onclick: () => applyFilter({ page: data.page + 1 }, { resetPage: false }),
    }, "下一页"),
  ]);
  host.appendChild(pager);
}

function provinceNameOf(key) {
  return PROVINCES[key]?.name || PROVINCES.guangdong.name;
}
