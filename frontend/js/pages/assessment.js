/**
 * pages/assessment.js —— 画像测评与档案表单（T06 骨架 → T23 三模式全量接线）
 *
 * 流程：省份下拉（31 省，**模式由省份映射，不可手选**）→ 自动带出高考模式
 *   → 按模式渲染选科控件（3+1+2 / 3+3 / 传统文理）→ 分数 + 位次(选填) + 偏好
 *   → 提交：POST /api/profile 建档（脱敏）+ GET /api/rank 换算位次与近三年等位分
 *   → 结果卡展示（位次 / rank_source / 等位分表 / capped 提示 / 免责）
 *
 * 红线：
 *   - 未落地数据省份（dataAvailable=false）：可浏览表单，提交时**明确提示
 *     「数据待补齐」**，本地建档不调后端位次换算（不伪造）；
 *   - 分数越界 / 再选数量错误 → 前端即时拦截；
 *   - DATA_MISSING（后端 200+code）→ 展示待补提示，不当作报错。
 */

import { h, toast } from "../ui.js";
import { pageShell } from "../components/PageShell.js";
import { scoreInput } from "../components/ScoreInput.js";
import { DISCLAIMER_TEXT } from "../components/DisclaimerBar.js";
import { updateProfile, getState } from "../state.js";
import { getCurrentUser } from "../auth.js";
import { PROVINCES, MODES, getProvince, PREFERENCE_OPTIONS } from "../config.js";
import { navigate } from "../router.js";
import { api } from "../api.js";

export default async function assessment(mount) {
  const { profile } = getState();
  const initialProvince = profile.province || "guangdong";

  /** 结果卡容器（提交后填充位次与等位分）。 */
  const resultBox = h("div", { id: "rank-result" });
  const noticeBox = h("div", { id: "province-notice" });

  function renderProvinceNotice(prov) {
    noticeBox.replaceChildren();
    if (prov.dataAvailable) return;
    noticeBox.appendChild(
      h("div", { class: "chat-notice chat-notice--warn" }, [
        h("strong", {}, `${prov.name}数据待补齐：`),
        document.createTextNode(" 该省份的一分一段与投档数据尚未入库，可先浏览院校库；位次换算与推荐等数据补齐后开放。"),
      ])
    );
  }

  /** 省份下拉：选中后重建表单（模式/科目池随省份变化）。 */
  const provinceSelect = h("select", { class: "score-input__field", id: "province-select" },
    Object.entries(PROVINCES).map(([key, p]) =>
      h("option", { value: key, selected: key === initialProvince }, p.name)
    )
  );

  let formHost = h("div", { id: "form-host" });

  function buildForm(provKey) {
    const prov = getProvince(provKey);
    renderProvinceNotice(prov);
    const form = scoreInput({
      mode: prov.mode,
      province: prov,
      initial: {
        score: profile.score ?? "",
        rank: profile.rank ?? "",
        subjectType: profile.subjectType || "",
        electives: profile.electives || [],
      },
      onSubmit: (data) => submit(prov, provKey, data),
    });

    // 偏好区（城市 / 专业方向 / 预算）—— 追加到表单内部 actions 之前
    const prefGroup = buildPreferenceGroup();
    form.insertBefore(prefGroup, form.querySelector(".score-input__actions"));

    // 模式徽章 + 说明（省份下拉下方）
    form.insertBefore(
      h("div", { class: "score-input__hint" },
        `${prov.name}采用「${MODES[prov.mode]}」模式（由省份自动带出，不可手选）`),
      form.firstChild
    );
    formHost.replaceChildren(form);
  }

  /** 偏好选项组（结构化，后端注入 Prompt 用）。 */
  function buildPreferenceGroup() {
    const pref = (getState().profile.preferences) || {};
    const mk = (name, options, current) =>
      h("div", { class: "chip-row" }, options.map((o) =>
        h("button", {
          class: `chip chip--soft${(current || []).includes(o) ? " is-active" : ""}`,
          type: "button",
          "data-pref": name, "data-val": o,
        }, o)
      ));
    const cityRow = mk("cityPref", PREFERENCE_OPTIONS.cityPref, pref.cityPref);
    const majorRow = mk("majorPref", PREFERENCE_OPTIONS.majorPref, pref.majorPref);
    const budgetRow = mk("budgetLevel", PREFERENCE_OPTIONS.budgetLevel, pref.budgetLevel ? [pref.budgetLevel] : []);

    for (const row of [cityRow, majorRow, budgetRow]) {
      row.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-pref]");
        if (!btn) return;
        if (btn.dataset.pref === "budgetLevel") {
          [...row.querySelectorAll(".chip")].forEach((c) => c.classList.toggle("is-active", c === btn));
        } else {
          btn.classList.toggle("is-active");
        }
      });
    }
    return h("div", { class: "score-input__group", id: "pref-group" }, [
      h("div", { class: "score-input__label" }, "偏好（选填，用于个性化推荐）"),
      h("div", { class: "score-input__hint" }, "期望城市"), cityRow,
      h("div", { class: "score-input__hint" }, "专业方向"), majorRow,
      h("div", { class: "score-input__hint" }, "预算"), budgetRow,
    ]);
  }

  function readPreferences() {
    const g = formHost.querySelector("#pref-group");
    if (!g) return { preferred_cities: [], preferred_majors: [], preferred_types: [], budget: "" };
    const sel = (name) =>
      [...g.querySelectorAll(`[data-pref="${name}"].is-active`)].map((b) => b.dataset.val);
    return {
      preferred_cities: sel("cityPref"),
      preferred_majors: sel("majorPref"),
      preferred_types: [],
      budget: (sel("budgetLevel")[0]) || "",
    };
  }

  provinceSelect.addEventListener("change", () => buildForm(provinceSelect.value));

  /** 提交：建档 + 位次换算（未落地省份只本地建档并提示）。 */
  async function submit(prov, provKey, data) {
    const prefs = readPreferences();

    // 本地档案（敏感明文只进内存态；服务端落库为脱敏 hash+bucket）
    updateProfile({
      province: provKey,
      score: data.score,
      rank: data.rank,
      subjectType: data.subjectType,
      electives: data.electives,
      preferences: {
        cityPref: prefs.preferred_cities,
        majorPref: prefs.preferred_majors,
        budgetLevel: prefs.budget,
      },
    });

    // 未落地省份：不调后端换算（科类口径也不适用），本地建档 + 明确提示
    if (!prov.dataAvailable) {
      resultBox.replaceChildren(
        h("div", { class: "card note-card" }, [
          h("div", { class: "note-card__title" }, "评估已暂存"),
          h("p", { class: "note-card__text" },
            `${prov.name}的位次换算与推荐数据待补齐，暂不能生成测算结果。您可以先浏览院校库与政策问答。`),
        ]),
        h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT)
      );
      toast("评估已暂存（该省数据待补齐）", "success");
      return;
    }

    try {
      // 位次未填时前端为 ""，后端 Pydantic int|null 解析 "" 会 422，需显式转 null
      const safeRank = data.rank === "" ? null : data.rank;

      // 1) 建档（脱敏落库；回执只含区间）
      const prof = await api.createProfile({
        province: prov.name,
        current_year: getState().currentYear || 2026,
        subject_type: data.subjectType,
        elective_subjects: data.electives,
        score: data.score,
        rank: safeRank,
        preferred_cities: prefs.preferred_cities,
        preferred_majors: prefs.preferred_majors,
        preferred_types: prefs.preferred_types,
        anon_id: getState().sessionId || null,
      });
      updateProfile({ profileId: prof.id, scoreBucket: prof.score_bucket });

      // 2) 位次 + 近三年等位分
      const rank = await api.queryRank({
        subject_type: data.subjectType,
        score: data.score,
        rank: safeRank,
        province: prov.name,
      });
      renderResult(prov, data, rank);
      toast("评估已保存，位次测算完成", "success");
    } catch (e) {
      // DATA_MISSING：业务正常但无数据（如该年缺表）→ 明确提示不报错
      if (e.payload && e.payload.code === "DATA_MISSING") {
        resultBox.replaceChildren(
          h("div", { class: "chat-notice chat-notice--warn" },
            `库内暂缺该组合的位次数据（${e.payload.message || "数据待补齐"}），未做任何推算。`),
          h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT)
        );
        return;
      }
      toast(`提交失败：${e.message}`, "error");
    }
  }

  /** 结果卡：位次 + 等位分三年表 + 口径与免责。 */
  function renderResult(prov, data, rank) {
    const equivRows = (rank.equiv_scores || []).map((e) =>
      h("tr", {}, [
        h("td", {}, String(e.year)),
        h("td", {}, e.score != null ? String(e.score) : "数据待补齐"),
        h("td", {}, e.capped ? "边界值（不外推）" : "—"),
      ])
    );
    resultBox.replaceChildren(
      h("div", { class: "card rank-result-card" }, [
        h("div", { class: "note-card__title" }, "位次测算结果"),
        h("div", { class: "rank-result__main" }, [
          h("div", { class: "rank-result__item" }, [
            h("div", { class: "rank-result__label" }, `${data.score} 分 · ${data.subjectType}`),
            h("div", { class: "rank-result__value" }, [
              document.createTextNode(`约第 ${rank.rank} 位`),
              h("span", { class: "rank-result__tag" },
                rank.rank_source === "manual" ? "（你直填的位次）" : "（按一分一段换算）"),
            ]),
          ]),
        ]),
        rank.note ? h("div", { class: "chat-notice chat-notice--warn" }, rank.note) : null,
        h("div", { class: "score-input__label" }, "近三年等位分（同位次对应年份的分数）"),
        h("div", { class: "fallback-table-wrap" }, [
          h("table", { class: "fallback-table" }, [
            h("thead", {}, [h("tr", {}, [h("th", {}, "年份"), h("th", {}, "等位分"), h("th", {}, "口径")])]),
            h("tbody", {}, equivRows),
          ]),
        ]),
        h("div", { class: "score-input__hint" }, rank.caliber_text || ""),
        h("div", { class: "score-input__actions" },
          h("button", { class: "btn btn--primary", onclick: () => navigate("/report") },
            ["查看冲稳保报告 ", h("span", {}, "→")])
        ),
        getCurrentUser()
          ? null
          : h("div", { class: "score-input__hint" },
              "提示：当前为游客模式，测算结果仅保存在本机。登录后可在「我的」中跨设备查看历史报告与志愿表。"),
      ]),
      h("div", { class: "chat-disclaimer" }, DISCLAIMER_TEXT)
    );
    resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  buildForm(initialProvince);

  mount.replaceChildren(
    pageShell({
      eyebrow: "第 1 步 / 共 3 步",
      title: "升学评估",
      subtitle: "先告诉系统你的基本情况，我们基于近三年真实投档数据推演冲稳保方案。",
      body: [
        renderGuestNotice(),
        h("div", { class: "assessment-grid" }, [
          h("div", {}, [
            h("div", { class: "score-input__group" }, [
              h("label", { class: "score-input__label", for: "province-select" }, "省份（决定高考模式）"),
              provinceSelect,
            ]),
            noticeBox,
            formHost,
            resultBox,
          ]),
          sideNotes(),
        ]),
      ],
    })
  );
}

/** 游客引导：无需登录即可测算；登录仅用于跨设备保存历史。 */
function renderGuestNotice() {
  const user = getCurrentUser();
  if (user) return null;
  return h("div", { class: "chat-notice chat-notice--info guest-notice" }, [
    h("strong", {}, "无需登录，直接测算。"),
    document.createTextNode(" 填写分数即可获得冲稳保方案与 AI 问答；"),
    h("a", { href: "#/login" }, "登录 / 注册"),
    document.createTextNode(" 仅用于跨设备保存你的评估信息与志愿表。"),
  ]);
}

/** 右侧说明卡：隐私与口径提示（产品必须告知的信息） */
function sideNotes() {
  return h("aside", { class: "assessment-notes" }, [
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, "为什么要填这些？"),
      h("ul", { class: "note-card__list" }, [
        h("li", {}, "分数用于换算全省位次，是匹配院校的核心依据；"),
        h("li", {}, "科类决定可选专业组范围；再选科目影响专业选科要求匹配；"),
        h("li", {}, "若你知道真实位次，直填后将优先于换算结果。"),
      ]),
    ]),
    h("div", { class: "card note-card" }, [
      h("div", { class: "note-card__title" }, "关于隐私"),
      h("p", { class: "note-card__text" },
        "你的分数与位次仅用于本次测算，服务端以脱敏形式存储（哈希 + 区间），日志不留存明文成绩与身份信息。"),
    ]),
  ]);
}
