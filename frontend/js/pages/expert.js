/**
 * pages/expert.js —— 预约专家（付费真人专家服务）
 *
 * 说明：**这是独立于 AI 助手的付费服务模块**，与免费 AI 对话互不牵扯。
 *       对齐见径「顾问服务」：三档定价 + 专家团队 + 预约咨询。
 * T06 只做展示骨架；预约与支付流程在 T24+ 落地。
 */

import { h, toast } from "../ui.js";
import { pageShell, placeholderCard } from "../components/PageShell.js";
import { api } from "../api.js";
import { getCurrentUser } from "../auth.js";
import { navigate } from "../router.js";

const PLANS = [
  {
    tier: "公益自检", price: "免费", priceValue: 0, desc: "先做规则自检，确认问题类型和咨询方向",
    points: [
      "志愿基础风险自检",
      "冲稳保逻辑说明",
      "1 条图文咨询回复",
      "推荐下一步准备清单",
    ],
  },
  {
    tier: "单次深度咨询", price: "¥699", priceValue: 699, desc: "90 分钟 1 对 1，解决一次完整志愿决策",
    points: [
      "包含公益自检全部内容",
      "90 分钟语音或视频咨询",
      "院校与专业初步匹配清单",
      "冲稳保梯度逐项诊断",
      "7 天内 2 次追问",
    ],
    featured: true,
  },
  {
    tier: "全程规划护航", price: "¥1,999", priceValue: 1999, desc: "从定位到填报持续陪跑，适合需要完整方案的家庭",
    points: [
      "包含单次深度咨询全部内容",
      "专属规划师持续跟进",
      "3 轮志愿方案推演与调整",
      "模拟填报与风险复核",
      "家长与学生联合沟通",
      "填报期优先答疑",
      "录取后入学准备建议",
    ],
  },
];

const EXPERTS = [
  { name: "李老师", title: "资深升学规划师", years: "12 年经验", field: "理工科方向" },
  { name: "王老师", title: "高考志愿研究员", years: "9 年经验", field: "文史科方向" },
  { name: "陈老师", title: "职业规划顾问", years: "8 年经验", field: "专业与职业匹配" },
];

export default async function expert(mount) {
  mount.replaceChildren(
    pageShell({
      eyebrow: "咨询与陪跑 · 与升学问答各自独立",
      title: "预约专家",
      subtitle: "从免费自检到全程陪跑，按家庭实际需要选择；与免费升学问答互不牵扯。",
      body: [
        h("div", { class: "expert-section" }, [
          h("h2", { class: "expert-section__title" }, "服务方案"),
          h("div", { class: "plan-grid" }, PLANS.map(planCard)),
        ]),
        h("div", { class: "expert-section" }, [
          h("h2", { class: "expert-section__title" }, "专家团队"),
          h("div", { class: "expert-grid" }, EXPERTS.map(expertCard)),
        ]),
        placeholderCard("提交后由管理员确认服务时间和后续安排；当前环境不直接发起支付。", "预约流程说明"),
      ],
    })
  );
}

function planCard(p) {
  return h("div", { class: `card plan-card${p.featured ? " plan-card--featured" : ""}` }, [
    h("div", { class: "plan-card__tier" }, p.tier),
    h("div", { class: "plan-card__price" }, p.price),
    h("div", { class: "card__meta plan-card__desc" }, p.desc),
    h("ul", { class: "plan-card__points" },
      p.points.map((pt) => h("li", {}, [h("span", { class: "plan-card__check" }, "✓"), pt]))
    ),
    h("button", {
      class: "btn btn--primary plan-card__cta",
      onclick: () => openBooking(p),
    }, p.price === "免费" ? "免费开始" : "选择方案"),
  ]);
}

function openBooking(plan) {
  if (!getCurrentUser()) {
    toast("请先登录后再预约", "info");
    navigate("/login?redirect=/expert");
    return;
  }
  const backdrop = h("div", { class: "modal-backdrop" });
  const modal = h("div", { class: "modal expert-booking" });
  const name = h("input", { class: "field-input", type: "text", maxlength: 64, placeholder: "联系人姓名" });
  const contact = h("input", {
    class: "field-input",
    type: "text",
    maxlength: 128,
    placeholder: "手机号或邮箱",
  });
  const note = h("textarea", {
    class: "field-textarea",
    rows: 4,
    maxlength: 2000,
    placeholder: "补充学生所在省份、分数段或希望解决的问题（选填）",
  });
  const close = () => {
    backdrop.remove();
    modal.remove();
  };
  const submit = async () => {
    if (!name.value.trim() || !contact.value.trim()) {
      toast("请填写联系人和联系方式", "error");
      return;
    }
    try {
      await api.createExpertAppointment({
        plan_tier: plan.tier,
        price: plan.priceValue,
        contact_name: name.value.trim(),
        contact_info: contact.value.trim(),
        note: note.value.trim(),
      });
      close();
      toast("预约已提交并保存，管理员会尽快与您联系", "success");
    } catch (e) {
      toast(`提交失败：${e.message}`, "error");
    }
  };
  modal.replaceChildren(
    h("div", { class: "modal__header" }, [
      h("h3", { class: "modal__title" }, `预约：${plan.tier}`),
      h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
    ]),
    h("div", { class: "modal__body" }, [
      h("p", { class: "expert-booking__desc" }, `${plan.price} · ${plan.desc}`),
      h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "联系人"), name]),
      h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "联系方式"), contact]),
      h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "补充说明"), note]),
    ]),
    h("div", { class: "modal__footer" }, [
      h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "取消"),
      h("button", { class: "btn btn--primary", type: "button", onclick: submit }, "提交预约"),
    ])
  );
  document.body.appendChild(backdrop);
  document.body.appendChild(modal);
}

function expertCard(e) {
  return h("div", { class: "card expert-card" }, [
    h("div", { class: "expert-card__avatar" }, e.name[0]),
    h("div", { class: "card__body" }, [
      h("div", { class: "card__title" }, `${e.name} · ${e.title}`),
      h("div", { class: "card__meta" }, `${e.years} · ${e.field}`),
    ]),
  ]);
}
