/**
 * pages/caseDetail.js —— 案例详情（脱敏真实案例）
 *
 * 路由 `#/cases/:slug`：
 *   - `GET /api/cases/{slug}` 取元数据 + 章节正文；
 *   - 章节为**机械解析**（按 `##` 切分），前端**原样展示**，不改写、不补写；
 *   - 表格行转为等宽文本块（避免引入 Markdown 依赖），引用行弱化显示。
 *
 * 红线：案例来自真实数据且已脱敏；本页不采集、不展示任何可识别个人信息。
 */

import { h, toast } from "../ui.js";
import { icon } from "../components/Icon.js";
import { pageShell } from "../components/PageShell.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { navigate } from "../router.js";
import { api } from "../api.js";
import { isCaseFavorite, toggleCaseFavorite } from "../caseFavorites.js";

export default async function caseDetail(mount, { param } = {}) {
  const slug = param || "";
  const body = h("div", { id: "case-detail" }, [h("div", { class: "card-list__empty" }, "加载中…")]);

  // 当前案例元信息（加载成功后填充），收藏按钮状态随其刷新
  let currentMeta = null;

  const favBtn = h("button", { class: "btn btn--ghost btn--fav", type: "button" }, "收藏案例");
  const updateFavBtn = () => {
    const on = !!(currentMeta && currentMeta.slug && isCaseFavorite(currentMeta.slug));
    favBtn.innerHTML = `${icon("star", { size: 14 })}<span>${on ? "已收藏" : "收藏案例"}</span>`;
    favBtn.classList.toggle("is-on", on);
  };
  favBtn.addEventListener("click", () => {
    if (!currentMeta || !currentMeta.slug) return;
    const { favorited } = toggleCaseFavorite(currentMeta);
    updateFavBtn();
    toast(favorited ? "已收藏，可在「我的 → 收藏案例」查看" : "已取消收藏", favorited ? "success" : "info");
  });

  mount.replaceChildren(
    pageShell({
      eyebrow: "案例中心",
      title: "案例详情",
      subtitle: "由真实投档数据自动生成并脱敏，仅供方法参考。",
      actions: [
        h("button", { class: "btn btn--ghost", onclick: () => navigate("/cases") }, "返回案例列表"),
        favBtn,
      ],
      body: [body],
    })
  );

  if (!slug) {
    body.replaceChildren(h("div", { class: "chat-notice chat-notice--error" }, "缺少案例编号。"));
    return;
  }

  try {
    const data = await api.caseDetail(slug);
    currentMeta = data.meta || {};
    updateFavBtn();
    render(data, body);
  } catch (e) {
    body.replaceChildren(
      h("div", { class: "chat-notice chat-notice--error" }, `加载失败：${e.message}`)
    );
  }
}

function render(data, body) {
  const m = data.meta || {};
  const sections = (data.sections || []).map((s) =>
    h("section", { class: "case-section" }, [
      h("div", { class: "case-section__heading" }, s.heading),
      ...renderLines(s.lines),
    ])
  );

  const meta = h("aside", { class: "case-detail__aside" }, [
    h("section", { class: "case-detail__meta-card" }, [
      h("span", { class: "case-type-badge" }, m.path_type || "真实案例"),
      h("h2", {}, m.title || "案例"),
      h("div", { class: "case-meta-grid" }, [
        h("span", {}, `科类：${m.subject_type || "—"}`),
        h("span", {}, `分数段：${m.score_band || "—"}`),
        h("span", {}, `位次段：${m.rank_band || "—"}`),
        h("span", {}, `报考年份：${m.valid_year || "—"}`),
        h("span", {}, `报告分层：${m.narrative_tier === "deep" ? "深度版" : "基础版"}`),
      ]),
    ]),
    h("div", { class: "chat-disclaimer" }, data.disclaimer || DISCLAIMER_TEXT),
  ]);

  body.replaceChildren(
    h("div", { class: "case-detail__layout" }, [
      meta,
      h("div", { class: "case-detail__main" }, sections),
    ])
  );
}

/** 章节行渲染：把案例中的 Markdown 表格/列表转成结构化 DOM，但不改写文本。 */
function renderLines(lines) {
  const nodes = [];
  const source = (lines || []).map((line) => String(line ?? ""));
  let i = 0;

  const flushParagraph = (buffer) => {
    if (buffer.length) {
      nodes.push(h("p", { class: "case-section__paragraph" }, buffer.join("\n")));
      buffer.length = 0;
    }
  };

  while (i < source.length) {
    const line = source[i].trim();
    const paragraph = [];

    if (!line) {
      i += 1;
      continue;
    }

    if (line.startsWith("### ")) {
      nodes.push(h("h4", { class: "case-subheading" }, line.slice(4)));
      i += 1;
      continue;
    }

    if (line.startsWith(">")) {
      nodes.push(h("blockquote", { class: "case-quote" }, line.replace(/^>\s?/, "")));
      i += 1;
      continue;
    }

    if (line.startsWith("|") && i + 1 < source.length && /^\|?[\s:|-]+\|?$/.test(source[i + 1].trim())) {
      const cells = splitTableRow(line);
      const rows = [];
      i += 2;
      while (i < source.length && source[i].trim().startsWith("|")) {
        rows.push(splitTableRow(source[i].trim()));
        i += 1;
      }
      nodes.push(
        h("div", { class: "case-table-wrap" },
          h("table", { class: "case-table" }, [
            h("thead", {}, h("tr", {}, cells.map((cell) => h("th", {}, cell)))),
            h("tbody", {}, rows.map((row) =>
              h("tr", {}, row.map((cell) => h("td", {}, cell)))
            )),
          ])
        )
      );
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < source.length && /^[-*]\s+/.test(source[i].trim())) {
        items.push(source[i].trim().replace(/^[-*]\s+/, ""));
        i += 1;
      }
      nodes.push(h("ul", { class: "case-list" }, items.map((item) => h("li", {}, item))));
      continue;
    }

    while (i < source.length) {
      const text = source[i].trim();
      if (!text || text.startsWith("### ") || text.startsWith(">") || /^[-*]\s+/.test(text)) break;
      if (text.startsWith("|") && i + 1 < source.length && /^\|?[\s:|-]+\|?$/.test(source[i + 1].trim())) break;
      paragraph.push(source[i]);
      i += 1;
    }
    flushParagraph(paragraph);
  }
  return nodes;
}

function splitTableRow(line) {
  return line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}
