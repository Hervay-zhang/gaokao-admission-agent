/**
 * pages/report.js —— AI 路径报告（冲稳保三档 + 风险解释 + 家长版）
 *
 * T06 骨架 → T25 全量接线：
 *   - 读取本地画像（T23 建档）→ `GET /api/report` 取引擎产出的完整报告；
 *   - 三档（冲/稳/保）列表，每条标注**库内依据**（参考位次 / 最新年投档最低分与位次 / 逐年 evidence）；
 *   - 风险解释（禁概率数字）、家长版摘要（学生版/家长版切换）；
 *   - 缺年 / 选科待补 / 计划变化逐条显式标注（不静默）；
 *   - 免责声明随报告展示（红线）。
 *
 * 红线：报告解释全部来自数据（后端规则模板），前端**不做任何分数/位次计算**，只渲染。
 */

import { h, toast } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { cardListSkeleton } from "../components/CardList.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { createFeedbackBar } from "../components/FeedbackBar.js";
import { getState, addPlanDraft, inPlanDraft } from "../state.js";
import { getProvince } from "../config.js";
import { navigate } from "../router.js";
import { api } from "../api.js";
import { getCurrentUser } from "../auth.js";

const TIERS = [
  { key: "chong", label: "冲", desc: "略高于当前位次，有希望但不稳妥", color: "var(--color-tier-chong)" },
  { key: "wen", label: "稳", desc: "与当前位次匹配，录取把握较大", color: "var(--color-tier-wen)" },
  { key: "bao", label: "保", desc: "低于当前位次，作为兜底保障", color: "var(--color-tier-bao)" },
];

export default async function report(mount) {
  const { profile, currentYear } = getState();
  const hasScore = profile.score !== null && profile.score !== undefined && profile.score !== "";
  const prov = getProvince(profile.province || "guangdong");

  if (!hasScore) {
    mount.replaceChildren(
      pageShell({
        eyebrow: "第 2 步 / 共 3 步",
        title: "冲稳保路径报告",
        subtitle: "还没有评估数据，先花几分钟填一下基本情况。",
        actions: h("button", { class: "btn btn--primary", onclick: () => navigate("/assessment") },
          ["去完成评估 ", h("span", {}, "→")]),
        body: [
          h("div", { class: "card note-card" }, [
            h("div", { class: "note-card__title" }, "需要先填基本信息"),
            h("p", { class: "note-card__text" },
              "填写省份、科类、分数（或位次）后，系统会基于近三年真实投档数据为你推演冲稳保方案。"),
          ]),
          h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT),
        ],
      })
    );
    return;
  }

  // 报告容器（先挂骨架，异步填充）
  const reportBody = h("div", { id: "report-body" }, [cardListSkeleton(3)]);
  const actionsHost = h("div", { class: "report-actions" });

  const head = `${prov.name} · ${profile.subjectType}类 · ${profile.score} 分`
    + (profile.rank ? ` · 位次 ${profile.rank}` : "")
    + ` · 参考 ${currentYear || 2026} 年及往年投档数据`;

  mount.replaceChildren(
    pageShell({
      eyebrow: "第 2 步 / 共 3 步",
      title: "冲稳保路径报告",
      subtitle: head,
      actions: actionsHost,
      body: [reportBody],
    })
  );

  await loadReport({ prov, profile, currentYear, reportBody, actionsHost });
}

async function loadReport({ prov, profile, currentYear, reportBody, actionsHost }) {
  try {
    const data = await api.buildReport({
      score: profile.score,
      subject_type: profile.subjectType,
      electives: (profile.electives || []).join(","),
      province: prov.name,
      rank: profile.rank || undefined,
      current_year: currentYear || 2026,
      report_tier: "basic",
    });
    renderReport({ data, prov, reportBody, actionsHost });
    void saveHistory(data, prov, profile, currentYear);
  } catch (e) {
    // DATA_MISSING：业务正常但无数据（该省/年缺表）→ 明确提示，不伪造
    if (e.payload && e.payload.code === "DATA_MISSING") {
      reportBody.replaceChildren(
        h("div", { class: "chat-notice chat-notice--warn" },
          `库内暂缺该组合的推荐数据（${e.payload.message || "数据待补齐"}），未做任何推算。`),
      );
      return;
    }
    reportBody.replaceChildren(
      h("div", { class: "card note-card" }, [
        h("div", { class: "note-card__title" }, "生成失败"),
        h("p", { class: "note-card__text" }, `未能生成报告：${e.message || "网络异常"}`),
        h("div", { class: "score-input__actions" },
          h("button", { class: "btn btn--primary", onclick: () => location.reload() }, "重试")),
      ]),
    );
  }
}

function renderReport({ data, prov, reportBody, actionsHost }) {
  let parentMode = false;

  const contentHost = h("div", { id: "report-content" });

  function paint() {
    if (parentMode) {
      contentHost.replaceChildren(parentView(data));
    } else {
      contentHost.replaceChildren(studentView(data));
    }
  }

  // 顶部动作：家长版切换 + 生成志愿表（T26）
  const parentBtn = h("button", { class: "btn btn--ghost" }, "家长版");
  parentBtn.addEventListener("click", () => {
    parentMode = !parentMode;
    parentBtn.classList.toggle("is-active", parentMode);
    parentBtn.textContent = parentMode ? "学生版" : "家长版";
    paint();
  });
  actionsHost.replaceChildren(
    parentBtn,
    h("button", {
      class: "btn btn--primary",
      onclick: () => addAllRecommendations(data),
    }, "全部加入志愿表"),
    h("button", { class: "btn btn--ghost", onclick: () => navigate("/plan") },
      ["去志愿表 ", h("span", {}, "→")]),
  );

  paint();

  // 反馈位（T22 接口，维度 recommend；报告无会话消息落库 → 匿名上报）
  const { sessionId } = getState();
  const feedback = createFeedbackBar({
    messageId: null,
    sessionSid: sessionId || "",
    dimension: "recommend",
    allowNoMessage: true,
  });
  const feedbackHost = h("div", { class: "report-feedback" }, [
    h("div", { class: "report-feedback__label" }, "这份报告有帮助吗？"),
    feedback.el,
  ]);

  reportBody.replaceChildren(contentHost, feedbackHost);
}

async function saveHistory(data, prov, profile, currentYear) {
  if (!getCurrentUser()) return;
  const tierCounts = {};
  for (const tier of data.tiers || []) tierCounts[tier.tier] = tier.items?.length || 0;
  try {
    await api.saveReportHistory({
      profile_id: profile.profileId || null,
      title: `${prov.name} · ${profile.subjectType || ""}类冲稳保报告`,
      province: prov.name,
      subject_type: profile.subjectType || "",
      score: profile.score || null,
      rank: profile.rank || null,
      current_year: currentYear,
      tier_counts: tierCounts,
      summary: data.parent_summary || data.risk_explanation || "",
    });
  } catch {
    /* 历史保存失败不影响报告阅读 */
  }
}

function addAllRecommendations(data) {
  let added = 0;
  for (const tier of data.tiers || []) {
    for (const item of tier.items || []) {
      if (addPlanDraft({
        university_code: item.university_code,
        university_name: item.university_name,
        group_code: item.group_code,
        tier: item.tier || tier.tier,
      })) {
        added += 1;
      }
    }
  }
  if (!added) {
    toast("推荐院校已在志愿表草稿中", "info");
  } else {
    toast(`已加入 ${added} 个院校专业组`, "success");
  }
  navigate("/plan");
}

/** 学生版：完整三档 + 口径 + 风险解释。 */
function studentView(data) {
  const sections = TIERS.map((t) => {
    const group = (data.tiers || []).find((g) => g.tier === t.key) || { items: [], interval: "", count: 0 };
    return tierSection(t, group);
  });

  return h("div", {}, [
    caliberCard(data),
    ...sections,
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, "风险提示"),
      h("p", { class: "note-card__text" }, data.risk_explanation || "—"),
    ]),
    statsCard(data),
    h("div", { class: "chat-disclaimer" }, data.disclaimer || DISCLAIMER_TEXT),
    h("p", { class: "report-footnote" },
      "以上推荐均标注库内依据（年份 / 投档最低分 / 位次），可点击查看院校详情，或到升学助手里继续追问。"),
  ]);
}

/** 家长版：大字号、少术语、口语化摘要 + 三档精简。 */
function parentView(data) {
  const groups = data.tiers || [];
  const rows = [];
  for (const t of TIERS) {
    const g = groups.find((x) => x.tier === t.key) || { items: [] };
    rows.push(
      h("div", { class: "parent-tier" }, [
        h("span", { class: "tier-badge", style: `--tier-color:${t.color}` }, t.label),
        h("span", { class: "parent-tier__count" }, `${g.items.length} 所`),
        h("span", { class: "parent-tier__desc" }, t.desc),
      ])
    );
  }
  return h("div", { class: "parent-view" }, [
    h("div", { class: "card note-card parent-card" }, [
      h("div", { class: "note-card__title" }, "一句话总结"),
      h("p", { class: "parent-summary" }, data.parent_summary || "—"),
    ]),
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, "冲稳保各档院校数量"),
      ...rows,
    ]),
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, "需要注意的地方"),
      h("p", { class: "parent-summary" }, data.risk_explanation || "—"),
    ]),
    h("div", { class: "chat-disclaimer" }, data.disclaimer || DISCLAIMER_TEXT),
  ]);
}

/** 口径说明卡：先说人话，技术细则收进折叠区（可查证但不必砸在第一屏）。 */
function caliberCard(data) {
  return h("div", { class: "card note-card report-caliber" }, [
    h("div", { class: "note-card__title" }, "这份报告怎么算的"),
    h("p", { class: "note-card__text" },
      "以广东省近三年真实投档数据为底，年份越近权重越高；每所院校都标了参考位次与投档分数，"
      + "冲的幅度、保的底线都按位次区间划定，不预测录取概率。"),
    h("details", { class: "report-caliber__more" }, [
      h("summary", {}, "查看测算细则"),
      h("p", { class: "note-card__text" }, data.caliber_text || "—"),
      data.explanation ? h("p", { class: "note-card__text" }, data.explanation) : null,
    ]),
  ]);
}

/** 统计卡：总条数 / 未匹配 / 排除原因。 */
function statsCard(data) {
  const excluded = data.excluded || {};
  const exclText = Object.entries(excluded)
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ");
  return h("div", { class: "report-stats" }, [
    h("div", { class: "report-stats__item" }, [
      h("div", { class: "report-stats__num" }, String(data.tiers ? data.tiers.reduce((a, g) => a + g.items.length, 0) : 0)),
      h("div", { class: "report-stats__label" }, "推荐条数"),
    ]),
    h("div", { class: "report-stats__item" }, [
      h("div", { class: "report-stats__num" }, String((data.ref_years_used || []).length)),
      h("div", { class: "report-stats__label" }, "参考年份数"),
    ]),
    h("div", { class: "report-stats__item" }, [
      h("div", { class: "report-stats__num" }, String(data.unmatched_count || 0)),
      h("div", { class: "report-stats__label" }, "未匹配候选"),
    ]),
    exclText ? h("div", { class: "report-stats__excl" }, `排除统计：${exclText}`) : null,
  ]);
}

/** 单档分区：档位徽章 + 说明 + 卡片列表。 */
function tierSection(tier, group) {
  const items = group.items || [];
  return h("section", { class: "tier-section", "data-tier": tier.key }, [
    h("div", { class: "tier-section__head" }, [
      h("span", { class: "tier-badge", style: `--tier-color:${tier.color}` }, tier.label),
      // 只放人话描述；系数口径收进上方「查看测算细则」，不在列表头上重复一遍
      h("span", { class: "tier-section__desc" }, tier.desc),
    ]),
    ...notesFor(items),
    items.length
      ? cardList(items, { empty: `${tier.label}档暂无推荐` })
      : h("div", { class: "card-list__empty" }, `${tier.label}档暂无推荐（该档位无匹配院校）`),
  ]);
}

/** 卡片列表渲染（复用见径行式白卡 + 「加入志愿表」）。 */
function cardList(items, { empty }) {
  return h("div", { class: "card-list-wrap" }, [
    h("div", { class: "card-list" },
      items.map((it) =>
        h("div", {
          class: "card card--row",
          onclick: () => navigate(`/schools/${it.university_code}`),
        }, [
          h("div", { class: "card__body" }, [
            h("div", { class: "card__title" }, [
              `${it.university_name}（专业组 ${it.group_code}）`,
              ...levelTags(it.level_tags),
            ]),
            h("div", { class: "card__meta" }, itemMeta(it)),
          ]),
          addBtn(it),
          h("span", { class: "card__arrow", "aria-hidden": "true" }, "›"),
        ])
      )),
  ]);
}

/** 「985|211|双一流」→ 独立小标签（连着渲染会糊成一串数字）。 */
function levelTags(raw) {
  return String(raw || "")
    .split("|")
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => h("span", { class: "card__tag" }, t));
}

/** 「加入志愿表」按钮（草稿态，去志愿表页消费）。
 *
 * 用描边小按钮而非整行实心按钮：一屏十几行时，实心蓝会把列表压成按钮墙。
 */
function addBtn(it) {
  const inDraft = inPlanDraft(it.university_code, it.group_code);
  const btn = h("button", {
    class: `btn btn--sm btn--ghost card__add${inDraft ? " is-done" : ""}`,
  }, inDraft ? "已在草稿" : "加入");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const ok = addPlanDraft({
      university_code: it.university_code,
      university_name: it.university_name,
      group_code: it.group_code,
      tier: it.tier || "",
    });
    toast(ok ? "已加入志愿表" : "该院校专业组已在草稿中", ok ? "success" : "info");
    btn.textContent = "已在草稿";
    btn.classList.add("is-done");
  });
  return btn;
}

/** 单条元信息：参考位次 + 最新年投档 + 计划。 */
function itemMeta(it) {
  const parts = [];
  if (it.ref_rank != null) parts.push(`参考位次 ${it.ref_rank}`);
  if (it.latest_year != null && it.latest_rank != null) {
    parts.push(`${it.latest_year} 年投档最低 ${it.latest_rank} 位`);
  }
  if (it.ref_score != null) parts.push(`最低分 ${it.ref_score}`);
  if (it.plan_count != null) parts.push(`计划 ${it.plan_count} 人`);
  return parts.join(" · ") || "数据待补齐";
}

/** 单档的数据说明：按「缺年份 / 选科待补 / 计划变化」聚合，一档最多三句。
 *
 * 旧实现把每所院校的 ref_years_note 逐条列出，同一句「缺 2024 年数据，已做保守化修正」
 * 会重复十几次，把首屏刷成了告警墙。这里改为按年份统计后合成一句话。
 */
function notesFor(items) {
  const missingByYear = new Map();
  let missingSchools = 0;
  let requirementPending = 0;
  const planNotes = [];

  for (const it of items) {
    // 后端两种写法都要认：「缺 2024,2025）」与「缺 2024 年数据，已做保守化修正」
    const hit = /缺\s*(\d{4}(?:[,，、\s]*\d{4})*)/.exec(it.ref_years_note || "");
    if (hit) {
      missingSchools += 1;
      for (const year of hit[1].split(/[,，、\s]+/).filter(Boolean)) {
        missingByYear.set(year, (missingByYear.get(year) || 0) + 1);
      }
    }
    if (it.require_note) requirementPending += 1;
    // plan_note 里常把「缺 X 年数据，已做保守化修正」再抄一遍，缺年信息已在上面合并
    if (it.plan_note && /计划/.test(it.plan_note)) planNotes.push(it.plan_note);
  }

  const lines = [];
  if (missingSchools) {
    const detail = [...missingByYear.entries()]
      .map(([year, count]) => `缺 ${year} 年数据的 ${count} 所`)
      .join("、");
    lines.push(
      `本档 ${items.length} 所中有 ${missingSchools} 所参考年份不完整（${detail}），`
      + "已按可得年份保守修正，档位判断不受影响。"
    );
  }
  if (requirementPending) {
    lines.push(`${requirementPending} 所的再选科目要求待补，填报前请以当年招生计划为准。`);
  }
  for (const note of [...new Set(planNotes)].slice(0, 2)) lines.push(note);

  if (!lines.length) return [];
  return [
    h("div", { class: "chat-notice chat-notice--warn report-notes" },
      lines.map((line) => h("div", {}, line))),
  ];
}
