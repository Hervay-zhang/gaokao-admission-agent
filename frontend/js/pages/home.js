/**
 * pages/home.js —— 首页
 * Hero + 数据亮点 + 快捷试问 + 功能亮点 + 四步流程 + 精选案例 + 信任背书
 */

import { h } from "../ui.js";
import { icon } from "../components/Icon.js";
import { renderHero } from "../components/Hero.js";
import { navigate } from "../router.js";
import { mockQuickQuestions } from "../mock.js";
import {
  destroyConsultationTicker,
  renderConsultationTicker,
} from "../components/ConsultationTicker.js";
import { api } from "../api.js";

export default async function home(mount) {
  const tickerRoot = h("div", { id: "consultation-ticker-root" });
  const statsRoot = statsBar();
  const showcaseRoot = showcaseSection();
  mount.replaceChildren(
    renderHero(),
    statsRoot,
    tickerRoot,
    quickAskSection(),
    featuresSection(),
    flowSection(),
    showcaseRoot,
    trustSection(),
  );
  await Promise.allSettled([
    renderConsultationTicker(tickerRoot),
    loadStats(statsRoot),
    loadShowcase(showcaseRoot),
  ]);
  return { destroy: destroyConsultationTicker };
}

/** 数据亮点条（增强信任感） */
function statsBar() {
  return h("section", { class: "stats-bar" }, [
    h("div", { class: "stats-bar__inner" }, [
      statItem("—", "已收录院校"),
      statItem("—", "投档线记录"),
      statItem("—", "参考数据年份"),
      statItem("—", "社区主题"),
    ]),
  ]);
}

function statItem(num, label) {
  return h("div", { class: "stats-bar__item" }, [
    h("div", { class: "stats-bar__num" }, num),
    h("div", { class: "stats-bar__label" }, label),
  ]);
}

async function loadStats(host) {
  try {
    const data = await api.stats();
    const years = (data.ref_years || []).length
      ? `${data.ref_years[0]}-${data.ref_years[data.ref_years.length - 1]}`
      : "—";
    host.querySelector(".stats-bar__inner")?.replaceChildren(
      statItem(Number(data.universities || 0).toLocaleString("zh-CN"), "已收录院校"),
      statItem(Number(data.admission_lines || 0).toLocaleString("zh-CN"), "投档线记录"),
      statItem(years, "参考数据年份"),
      statItem(Number(data.community_posts || 0).toLocaleString("zh-CN"), "社区主题"),
    );
  } catch {
    // 保留占位，不用静态数字冒充真实数据。
  }
}

/** 快捷试问（一键唤起 AI 助手） */
function quickAskSection() {
  return h("section", { class: "container quick-ask" }, [
    h("h2", { class: "section-title" }, "试试这样问升学助手"),
    h("div", { class: "quick-ask__chips" },
      mockQuickQuestions.map((q) =>
        h("button", {
          class: "btn btn--ghost quick-ask__btn",
          onclick: () => navigate(`/chat?q=${encodeURIComponent(q)}`),
        }, q)
      )
    ),
  ]);
}

/** 功能亮点卡片 */
function featuresSection() {
  // icon 取 components/Icon.js 的线描名（不用 emoji：跨系统渲染不一致，且与站点调性冲突）
  const features = [
    { icon: "radar", title: "升学评估", path: "/assessment", desc: "几分钟填完基本情况，看清自己的位置与可选范围" },
    { icon: "network", title: "冲稳保路径", path: "/plan", desc: "按近三年投档数据排档，冲多高、保到哪都有出处" },
    { icon: "school", title: "院校专业库", path: "/schools", desc: "已收录 1,095 所院校，可结合 15,868 条投档线按位次检索" },
    { icon: "book", title: "真实案例集", path: "/cases", desc: "用结构化脱敏案例还原不同分数段的决策路径" },
    { icon: "chat", title: "升学社区", path: "/community", desc: "围绕志愿填报、专业选择与备考经验交流讨论" },
    { icon: "mentor", title: "专家咨询", path: "/expert", desc: "预约资深规划师一对一咨询，按家庭情况聊" },
  ];
  return h("section", { class: "container features" }, [
    h("h2", { class: "section-title" }, "这里能做什么"),
    h("p", { class: "section-subtitle" }, "从评估到填报，每条推荐都能翻回投档数据原文"),
    h("div", { class: "features__grid" },
      features.map((f) =>
        h("div", { class: "feature-card card", onclick: () => navigate(f.path) }, [
          h("div", { class: "feature-card__icon", html: icon(f.icon, { size: 26 }) }),
          h("div", { class: "feature-card__title" }, f.title),
          h("div", { class: "feature-card__desc" }, f.desc),
        ])
      )
    ),
  ]);
}

/** 四步流程展示 */
function flowSection() {
  const steps = [
    { n: "01", t: "输入分数位次", d: "填写省份、选科与成绩，系统换算全省位次" },
    { n: "02", t: "生成冲稳保方案", d: "基于近三年真实投档数据，给出三档院校" },
    { n: "03", t: "看懂为什么推荐", d: "每条推荐附数据依据与风险解释，可追问" },
    { n: "04", t: "沉淀志愿表", d: "建档、排序、导出分享" },
  ];
  return h("section", { class: "container flow-section" }, [
    h("h2", { class: "section-title" }, "四步走完一次志愿规划"),
    h("div", { class: "flow-section__grid" },
      steps.map((s) =>
        h("div", { class: "flow-card card" }, [
          h("div", { class: "flow-card__num" }, s.n),
          h("div", { class: "flow-card__title" }, s.t),
          h("div", { class: "flow-card__desc" }, s.d),
        ])
      )
    ),
  ]);
}

/** 精选案例展示 */
function showcaseSection() {
  const grid = h("div", { class: "showcase__grid" }, [
    h("div", { class: "showcase-card card" }, [
      h("div", { class: "showcase-card__title" }, "正在读取最新案例…"),
    ]),
  ]);
  return h("section", { class: "container showcase" }, [
    h("h2", { class: "section-title" }, "规划路径示例"),
    h("p", { class: "section-subtitle" }, "读取案例中心最新生成的脱敏案例，实际方案以个人画像为准"),
    grid,
    h("div", { class: "showcase__action" }, [
      h("button", { class: "btn btn--ghost", onclick: () => navigate("/cases") }, "查看全部案例 →"),
    ]),
  ]);
}

async function loadShowcase(host) {
  const grid = host.querySelector(".showcase__grid");
  if (!grid) return;
  try {
    const data = await api.listCases({ page: 1, page_size: 3 });
    const cases = (data.cases || data.items || []).slice(0, 3);
    if (!cases.length) {
      grid.replaceChildren(h("div", { class: "card-list__empty" }, "案例正在整理中。"));
      return;
    }
    grid.replaceChildren(...cases.map((c) =>
      h("div", { class: "showcase-card card", onclick: () => navigate(`/cases/${c.slug}`) }, [
        h("div", { class: "showcase-card__head" }, [
          h("span", { class: "case-card__type" }, c.path_type || "真实案例"),
          h("span", { class: "showcase-card__score" }, `${c.subject_type || "—"} · ${c.score_band || "—"}分`),
        ]),
        h("h3", { class: "showcase-card__title" }, c.title || "升学规划案例"),
        h("p", { class: "showcase-card__summary" },
          `位次段 ${c.rank_band || "—"} · 验证年 ${c.valid_year || "—"} · 推荐 ${c.n_reco || 0} 条`),
      ])
    ));
  } catch {
    grid.replaceChildren(h("div", { class: "card-list__empty" }, "案例暂时无法加载。"));
  }
}

/** 信任背书区 */
function trustSection() {
  const items = [
    { icon: "shield", title: "数据真实可信", desc: "所有院校投档数据均来自教育考试院官方发布，可追溯至具体年份与批次" },
    { icon: "branch", title: "推荐讲得出依据", desc: "每条推荐都带分档依据与风险提示，不做黑箱推荐" },
    { icon: "heart", title: "核心功能免费", desc: "评估画像、院校检索、路径报告等核心功能长期免费" },
  ];
  return h("section", { class: "container trust" }, [
    h("h2", { class: "section-title" }, "为什么值得信赖"),
    h("div", { class: "trust__grid" },
      items.map((it) =>
        h("div", { class: "trust-card" }, [
          h("div", { class: "trust-card__icon", html: icon(it.icon, { size: 28 }) }),
          h("div", { class: "trust-card__title" }, it.title),
          h("div", { class: "trust-card__desc" }, it.desc),
        ])
      )
    ),
  ]);
}
