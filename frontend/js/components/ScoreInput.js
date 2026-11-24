/**
 * ScoreInput.js —— 分数 + 位次 + 选科输入组件（T06 骨架 → T23 三模式动态化）
 *
 * 职责：
 *   · 分数输入 0–750，越界即时提示并禁用提交；
 *   · 按省份映射的**高考模式**动态渲染选科控件（模式不可手选，防错）：
 *       - 3+1+2：首选单选（物理/历史）+ 再选四选二；
 *       - 3+3：选考科目池中选三门；
 *       - traditional：文/理 单选；
 *   · 位次可直填（选填，**优先于**按分数换算）；
 *   · 提交回调 `onSubmit({ score, rank, subjectType, electives, mode })`。
 *
 * 注意：真正的高考模式口径与选科校验由后端统一实现；本组件只做前端交互校验。
 */

import { h } from "../ui.js";

export const SCORE_MIN = 0;
export const SCORE_MAX = 750;

/**
 * 校验分数：返回 null 表示合法，否则返回错误文案。
 */
export function validateScore(value) {
  if (value === "" || value === null || value === undefined) return "请输入分数";
  const n = Number(value);
  if (Number.isNaN(n)) return "分数必须是数字";
  if (!Number.isInteger(n)) return "分数必须是整数";
  if (n < SCORE_MIN || n > SCORE_MAX) return `分数需在 ${SCORE_MIN}–${SCORE_MAX} 之间`;
  return null;
}

/**
 * 校验选科（按模式分派）：返回 null 表示合法。
 * @param {object} s { mode, subjectType, electives }
 */
export function validateSubject(s) {
  if (s.mode === "3+1+2") {
    if (!s.subjectType) return "请选择首选科目（物理 / 历史）";
    if (s.electives.length !== 2) return "再选科目需恰好选择 2 门";
  } else if (s.mode === "3+3") {
    if (s.electives.length !== 3) return "选考科目需恰好选择 3 门";
  } else if (s.mode === "traditional") {
    if (!s.subjectType) return "请选择科类（文科 / 理科）";
  }
  return null;
}

/** 校验位次（选填）：空合法；填了必须正整数。 */
export function validateRank(value) {
  if (value === "" || value == null) return null;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) return "位次需为正整数（如不确定可留空，由系统换算）";
  return null;
}

/**
 * 渲染分数+位次+选科输入卡。
 * @param {object} [opts]
 * @param {"3+1+2"|"3+3"|"traditional"} [opts.mode]
 * @param {object} [opts.province] 省份配置 { name, primary, electivePool }
 * @param {Function} [opts.onSubmit]
 * @param {object} [opts.initial] { score, rank, subjectType, electives }
 * @returns {HTMLElement}
 */
export function scoreInput({ mode = "3+1+2", province = {}, onSubmit = () => {}, initial = {} } = {}) {
  const primaryOptions = province.primary || ["物理", "历史"];
  const electivePool = province.electivePool || ["化学", "生物", "政治", "地理"];

  const model = {
    mode,
    score: initial.score ?? "",
    rank: initial.rank ?? "",
    subjectType: initial.subjectType || "",
    electives: [...(initial.electives || [])],
  };

  const errBox = h("div", { class: "score-input__error", role: "alert", hidden: true });
  // 页签一打开就弹「请填入分数」的红框很像报错，先只禁用按钮；用户动过表单再提示。
  let interacted = false;
  const submitBtn = h("button", { class: "btn btn--primary", type: "submit" }, ["生成冲稳保方案 ", h("span", {}, "→")]);

  // ---- 分数输入 ----
  const scoreField = h("input", {
    class: "score-input__field",
    id: "score-field",
    name: "score",
    type: "number",
    inputmode: "numeric",
    min: String(SCORE_MIN),
    max: String(SCORE_MAX),
    step: "1",
    placeholder: `如 600（${SCORE_MIN}–${SCORE_MAX}）`,
    value: model.score === "" ? "" : String(model.score),
  });
  const scoreHint = h("div", { class: "score-input__hint" }, `可填 ${SCORE_MIN}–${SCORE_MAX} 的整数；系统将换算为全省位次`);

  scoreField.addEventListener("input", () => {
    interacted = true;
    model.score = scoreField.value;
    const err = validateScore(model.score);
    scoreField.classList.toggle("is-invalid", Boolean(err));
    scoreHint.textContent = err || `可填 ${SCORE_MIN}–${SCORE_MAX} 的整数；系统将换算为全省位次`;
    scoreHint.classList.toggle("is-error", Boolean(err));
    refreshSubmit();
  });

  // ---- 位次输入（选填；直填优先于换算）----
  const rankField = h("input", {
    class: "score-input__field",
    id: "rank-field",
    name: "rank",
    type: "number",
    inputmode: "numeric",
    min: "1",
    step: "1",
    placeholder: "选填，如 25000",
    value: model.rank === "" || model.rank == null ? "" : String(model.rank),
  });
  const rankHint = h("div", { class: "score-input__hint" }, "选填：你知晓的真实位次将优先于按分数换算");
  rankField.addEventListener("input", () => {
    interacted = true;
    model.rank = rankField.value;
    const err = validateRank(model.rank);
    rankField.classList.toggle("is-invalid", Boolean(err));
    rankHint.textContent = err || "选填：你知晓的真实位次将优先于按分数换算";
    rankHint.classList.toggle("is-error", Boolean(err));
    refreshSubmit();
  });

  // ---- 首选科目 / 文理切换（3+1+2 与 traditional）----
  let primaryRow = null;
  if (mode !== "3+3") {
    primaryRow = h("div", { class: "chip-row", id: "primary-row" },
      primaryOptions.map((subj) =>
        h("button", {
          class: `chip${model.subjectType === subj ? " is-active" : ""}`,
          type: "button",
          "data-subject": subj,
        }, subj)
      )
    );
    primaryRow.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-subject]");
      if (!btn) return;
      model.subjectType = btn.dataset.subject;
      [...primaryRow.querySelectorAll(".chip")].forEach((c) =>
        c.classList.toggle("is-active", c.dataset.subject === model.subjectType)
      );
      refreshSubmit();
    });
  }

  // ---- 再选/选考科目 ----
  const needElectives = mode !== "traditional";
  const electiveCount = mode === "3+1+2" ? 2 : 3;
  let electiveRow = null;
  let electiveHint = null;
  if (needElectives) {
    electiveRow = h("div", { class: "chip-row", id: "elective-row" },
      electivePool.map((subj) =>
        h("button", {
          class: `chip chip--soft${model.electives.includes(subj) ? " is-active" : ""}`,
          type: "button",
          "data-elective": subj,
        }, subj)
      )
    );
    electiveHint = h(
      "div",
      { class: "score-input__hint" },
      mode === "3+1+2" ? "再选科目：从 4 门中选 2 门" : `选考科目：从中选 ${electiveCount} 门`
    );
    electiveRow.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-elective]");
      if (!btn) return;
      const subj = btn.dataset.elective;
      const i = model.electives.indexOf(subj);
      if (i >= 0) {
        model.electives.splice(i, 1);
      } else if (model.electives.length < electiveCount) {
        model.electives.push(subj);
      }
      [...electiveRow.querySelectorAll(".chip")].forEach((c) =>
        c.classList.toggle("is-active", model.electives.includes(c.dataset.elective))
      );
      electiveHint.textContent =
        (mode === "3+1+2" ? "再选科目" : "选考科目") + `：已选 ${model.electives.length}/${electiveCount}`;
      refreshSubmit();
    });
  }

  function refreshSubmit() {
    const err =
      validateScore(model.score) ||
      validateRank(model.rank) ||
      validateSubject({ mode, subjectType: model.subjectType, electives: model.electives });
    submitBtn.disabled = Boolean(err);
    submitBtn.classList.toggle("is-disabled", Boolean(err));
    if (errBox) {
      errBox.textContent = err || "";
      errBox.hidden = !err || !interacted;
    }
  }

  const groups = [
    h("div", { class: "score-input__group" }, [
      h("label", { class: "score-input__label", for: "score-field" }, "高考总分"),
      scoreField,
      scoreHint,
    ]),
    h("div", { class: "score-input__group" }, [
      h("label", { class: "score-input__label", for: "rank-field" }, "全省位次（选填）"),
      rankField,
      rankHint,
    ]),
  ];
  if (primaryRow) {
    groups.push(
      h("div", { class: "score-input__group" }, [
        h("div", { class: "score-input__label" }, mode === "traditional" ? "科类" : "首选科目"),
        primaryRow,
      ])
    );
  }
  if (electiveRow) {
    groups.push(
      h("div", { class: "score-input__group" }, [
        h("div", { class: "score-input__label" }, mode === "3+1+2" ? "再选科目" : "选考科目"),
        electiveRow,
        electiveHint,
      ])
    );
  }

  const form = h("form", { class: "score-input card", id: "score-form", novalidate: true }, [
    ...groups,
    errBox,
    h("div", { class: "score-input__actions" }, submitBtn),
  ]);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const err =
      validateScore(model.score) ||
      validateRank(model.rank) ||
      validateSubject({ mode, subjectType: model.subjectType, electives: model.electives });
    if (err) {
      errBox.textContent = err;
      errBox.hidden = false;
      return;
    }
    onSubmit({
      score: Number(model.score),
      rank: model.rank === "" || model.rank == null ? null : Number(model.rank),
      subjectType: model.subjectType,
      electives: [...model.electives],
      mode: model.mode,
    });
  });

  refreshSubmit();
  return form;
}
