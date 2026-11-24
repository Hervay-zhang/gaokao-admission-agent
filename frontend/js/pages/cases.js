/**
 * pages/cases.js —— 案例中心（脱敏真实案例 + 筛选）
 *
 * T06 骨架 → T27 全量接线：
 *   - `GET /api/cases` 取案例列表（真实投档数据生成、已脱敏）；
 *   - 筛选：科类 / 分数段 / 路径类型 / 叙事分层，条件写入 URL（刷新不丢、可分享）；
 *   - 卡片点击 → `#/cases/:slug` 详情。
 *
 * 红线：案例由后端从真实数据生成，前端**只展示不改写**；不做任何编造。
 */

import { h, toast } from "../ui.js";
import { icon } from "../components/Icon.js";
import { pageShell } from "../components/PageShell.js";
import { cardListSkeleton } from "../components/CardList.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { navigate, parseQuery, setQuery } from "../router.js";
import { api } from "../api.js";
import { isCaseFavorite, toggleCaseFavorite } from "../caseFavorites.js";

const PATH_TYPE_CLASS = {
  进取型: "is-chong",
  稳妥型: "is-wen",
  保底型: "is-bao",
  滑档预警: "is-alert",
};

export default async function cases(mount) {
  const query = parseQuery();
  const filter = {
    subject_type: query.subject_type || "",
    score_band: query.score_band || "",
    path_type: query.path_type || "",
    narrative_tier: query.narrative_tier || "",
  };

  const listHost = h("div", { id: "case-list" }, [cardListSkeleton(3)]);
  const filterHost = h("div", { class: "filter-bar case-filter-panel" });

  mount.replaceChildren(
    pageShell({
      eyebrow: "案例中心",
      title: "真实规划案例",
      subtitle: "看看和你情况相近的考生，系统给出的方案事后验证结果如何。",
      body: [filterHost, listHost, h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT)],
    })
  );

  let data = { cases: [], filters: {}, total: 0 };
  try {
    data = await api.listCases(filter);
  } catch (e) {
    listHost.replaceChildren(h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${e.message}`));
    return;
  }

  renderFilters({ filterHost, filters: data.filters || {}, filter, listHost });

  if (!data.cases.length) {
    listHost.replaceChildren(
      h("div", { class: "card-list__empty" }, "没有符合筛选条件的案例，试试放宽条件。")
    );
    return;
  }
  listHost.replaceChildren(h("div", { class: "case-grid" }, data.cases.map(caseCard)));
}

function renderFilters({ filterHost, filters, filter, listHost }) {
  const group = (label, key, options, toLabel) => {
    const chips = [""].concat(options || []).map((opt) =>
      h("button", {
        class: `chip chip--soft${filter[key] === opt ? " is-active" : ""}`,
        type: "button",
        "data-k": key, "data-v": opt,
      }, opt === "" ? `全部${label}` : (toLabel ? toLabel(opt) : opt))
    );
    return h("div", { class: "filter-group" }, [
      h("span", { class: "filter-group__label" }, label), h("div", { class: "filter-bar__chips" }, chips),
    ]);
  };

  filterHost.replaceChildren(
    h("div", { class: "filter-bar filter-bar--col" }, [
      group("科类", "subject_type", filters.subject_types),
      group("分数段", "score_band", filters.score_bands),
      group("路径类型", "path_type", filters.path_types),
      group("报告分层", "narrative_tier", filters.narrative_tiers,
        (t) => (t === "deep" ? "深度版" : "基础版")),
    ])
  );

  filterHost.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-k]");
    if (!btn) return;
    const k = btn.dataset.k;
    const v = btn.dataset.v;
    filter[k] = filter[k] === v ? "" : v;
    // 写入 URL（刷新/分享保持筛选）
    setQuery(Object.fromEntries(Object.entries(filter).filter(([, val]) => val)));
    listHost.replaceChildren(cardListSkeleton(3));
    try {
      const data = await api.listCases(filter);
      renderFilters({ filterHost, filters: data.filters || {}, filter, listHost });
      listHost.replaceChildren(
        data.cases.length
          ? h("div", { class: "case-grid" }, data.cases.map(caseCard))
          : h("div", { class: "card-list__empty" }, "没有符合筛选条件的案例，试试放宽条件。")
      );
    } catch (err) {
      listHost.replaceChildren(h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${err.message}`));
    }
  });
}

function caseCard(c) {
  const s = c.summaries || {};
  const bao = s.bao || { rec: 0, hit: 0 };
  const meta = {
    slug: c.slug,
    title: c.title,
    subject_type: c.subject_type,
    score_band: c.score_band,
    rank_band: c.rank_band,
    path_type: c.path_type,
  };
  const favBtn = h("button", {
    class: "case-card__fav",
    type: "button",
    "aria-label": isCaseFavorite(c.slug) ? "取消收藏" : "收藏案例",
    title: isCaseFavorite(c.slug) ? "已收藏" : "收藏案例",
    onclick: (ev) => {
      ev.stopPropagation();
      const { favorited } = toggleCaseFavorite(meta);
      updateFavBtn(favBtn, favorited);
      toast(favorited ? "已收藏案例" : "已取消收藏", favorited ? "success" : "info");
    },
  }, [h("i", { html: icon("star", { size: 14 }) })]);
  favBtn.classList.toggle("is-on", isCaseFavorite(c.slug));

  return h("article", {
    class: "case-card card",
    "data-tier": c.narrative_tier,
    onclick: () => navigate(`/cases/${c.slug}`),
  }, [
    h("div", { class: "case-card__head" }, [
      h("span", { class: `case-card__type ${PATH_TYPE_CLASS[c.path_type] || ""}` }, c.path_type),
      h("div", { class: "case-card__head-right" }, [
        h("span", { class: "case-card__tier" }, c.narrative_tier === "deep" ? "深度版" : "基础版"),
        favBtn,
      ]),
    ]),
    h("h3", { class: "case-card__title" }, c.title),
    h("div", { class: "case-card__metrics" }, [
      h("span", {}, [h("strong", {}, c.subject_type), h("small", {}, "科类")]),
      h("span", {}, [h("strong", {}, c.score_band), h("small", {}, "分数段")]),
      h("span", {}, [h("strong", {}, c.rank_band), h("small", {}, "位次段")]),
      h("span", {}, [h("strong", {}, String(c.valid_year)), h("small", {}, "验证年")]),
    ]),
    h("div", { class: "case-card__footer" }, [
      h("div", { class: "case-card__stats" }, [
        h("span", {}, `推荐 ${c.n_reco} 条`),
        h("span", {}, `保档 ${bao.rec} 条（命中 ${bao.hit}）`),
        c.chong_hit ? h("span", { class: "case-card__hit" }, "冲档命中") : null,
      ]),
      h("span", { class: "case-card__arrow", "aria-hidden": "true" }, "→"),
    ]),
  ]);
}

/** 刷新收藏按钮的展示状态（实心星 + 语义标签）。 */
function updateFavBtn(btn, on) {
  btn.innerHTML = icon("star", { size: 14 });
  btn.classList.toggle("is-on", on);
  btn.setAttribute("aria-label", on ? "取消收藏" : "收藏案例");
  btn.title = on ? "已收藏" : "收藏案例";
}
