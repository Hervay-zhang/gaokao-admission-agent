/**
 * QuickLinks.js —— 页面底部「快捷链接区」（常驻 footer）
 *
 * 后端 `/api/quick-links` 动态读取（默认含阳光高考等官方平台）；后端不可达时
 * 用内置兜底链接渲染，保证离线也能访问官方高考平台。
 * 管理端可在 `#/admin` 增删改（写入 quick_links 表）。
 */

import { h } from "../ui.js";
import { api } from "../api.js";

/** 分组展示名（与后端 ALLOWED_GROUPS 对齐）。 */
const GROUP_LABELS = {
  gaokao: "高考平台",
  official: "官方入口",
  tool: "实用工具",
};

/** 后端不可达时的兜底链接（与迁移 0007 默认数据一致，仅作离线兜底）。 */
const DEFAULT_LINKS = [
  { label: "阳光高考（教育部）", url: "https://gaokao.chsi.com.cn/", icon: "官", group: "gaokao" },
  { label: "学信网", url: "https://www.chsi.com.cn/", icon: "学", group: "gaokao" },
  { label: "广东省教育考试院", url: "https://eea.gd.gov.cn/", icon: "粤", group: "gaokao" },
  { label: "教育部官网", url: "https://www.moe.gov.cn/", icon: "教", group: "official" },
  { label: "中国教育考试网", url: "https://www.neea.edu.cn/", icon: "考", group: "official" },
];

const CORE_SERVICES = [
  { label: "升学评估", path: "#/assessment" },
  { label: "冲稳保路径报告", path: "#/report" },
  { label: "院校专业匹配", path: "#/schools" },
  { label: "升学社区交流", path: "#/community" },
];

const RESOURCE_LINKS = [
  { label: "院校与专业全景库", path: "#/schools" },
  { label: "升学规划案例集", path: "#/cases" },
  { label: "我的志愿表", path: "#/plan" },
  { label: "预约专家咨询", path: "#/expert" },
];

/**
 * 渲染快捷链接区（异步拉取后端；失败用兜底）。挂载点 #quick-links-root。
 */
export async function renderQuickLinks() {
  const root = document.getElementById("quick-links-root");
  if (!root) return;

  let links = DEFAULT_LINKS;
  try {
    const data = await api.quickLinks();
    if (data && Array.isArray(data.items) && data.items.length) {
      links = data.items.map((it) => ({
        label: it.label,
        url: it.url,
        icon: it.icon || "",
        group: it.group || "gaokao",
      }));
    }
  } catch (e) {
    console.warn("[quick-links] 后端不可达，使用内置兜底链接：", e.message);
  }

  const externalLinks = links.map((link) => ({
    ...link,
    groupLabel: GROUP_LABELS[link.group] || link.group,
  }));

  const year = 2027;
  root.replaceChildren(
    h("section", { class: "quick-links", "aria-label": "快捷导航" }, [
      h("div", { class: "container quick-links__inner" }, [
        h("div", { class: "footer-columns" }, [
          h("section", { class: "footer-brand" }, [
            h("div", { class: "footer-brand__logo" }, [
              h("span", { class: "footer-brand__seal" }, "志"),
              h("div", {}, [
                h("span", { class: "footer-brand__name" }, "高考志愿助手"),
                h("span", { class: "footer-brand__kicker" }, "升学路径规划与数据参考"),
              ]),
            ]),
            h("p", { class: "footer-brand__desc" },
              "基于当前已接入的公开投档数据，为考生与家庭提供可解释、可追溯的升学决策参考。"
            ),
            h("div", { class: "footer-brand__meta" }, [
              h("span", { class: "footer-brand__meta-item" }, "官方数据口径"),
              h("span", { class: "footer-brand__meta-item" }, "位次换算"),
              h("span", { class: "footer-brand__meta-item" }, "风险分层"),
            ]),
          ]),

          h("nav", { class: "footer-nav-col", "aria-label": "核心服务" }, [
            h("h3", { class: "footer-nav-col__title" }, "核心服务"),
            h("ul", { class: "footer-nav-col__list" },
              CORE_SERVICES.map((s) =>
                h("li", { class: "footer-nav-col__item" }, [
                  h("a", { class: "footer-nav-col__link", href: s.path }, [
                    h("span", {}, s.label),
                    h("span", { class: "footer-nav-col__arrow", "aria-hidden": "true" }, "→"),
                  ]),
                ])
              )
            ),
          ]),

          h("nav", { class: "footer-nav-col", "aria-label": "站内资源" }, [
            h("h3", { class: "footer-nav-col__title" }, "站内资源"),
            h("ul", { class: "footer-nav-col__list" },
              RESOURCE_LINKS.map((r) =>
                h("li", { class: "footer-nav-col__item" }, [
                  h("a", { class: "footer-nav-col__link", href: r.path }, [
                    h("span", {}, r.label),
                    h("span", { class: "footer-nav-col__arrow", "aria-hidden": "true" }, "→"),
                  ]),
                ])
              )
            ),
          ]),

          h("nav", { class: "footer-nav-col footer-nav-col--external", "aria-label": "官方平台" }, [
            h("h3", { class: "footer-nav-col__title" }, "官方平台"),
            h("ul", { class: "footer-nav-col__list" },
              externalLinks.map((it) =>
                h("li", { class: "footer-nav-col__item" }, [
                  h("a", {
                    class: "footer-nav-col__link footer-nav-col__link--external",
                    href: it.url,
                    target: "_blank",
                    rel: "noopener noreferrer",
                  }, [
                    it.icon ? h("span", { class: "footer-nav-col__icon" }, it.icon) : null,
                    h("span", { class: "footer-nav-col__label" }, it.label),
                    h("span", { class: "footer-nav-col__arrow", "aria-hidden": "true" }, "↗"),
                  ]),
                ])
              )
            ),
          ]),
        ]),

        h("div", { class: "footer-bottom" }, [
          h("span", { class: "footer-bottom__copy" }, `© ${year} 高考志愿助手`),
          h("span", { class: "footer-bottom__source" },
            "数据来源：教育部阳光高考平台 · 各省教育考试院"),
          h("span", { class: "footer-bottom__tagline" }, "公开数据口径 · 推荐依据可追溯"),
        ]),
      ]),
    ])
  );

  const floatingAI = document.getElementById("floating-ai-btn");
  if (floatingAI && "IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      ([entry]) => {
        floatingAI.classList.toggle("floating-ai--footer-visible", entry.isIntersecting);
      },
      { threshold: 0.03 }
    );
    observer.observe(root);
  }
}
