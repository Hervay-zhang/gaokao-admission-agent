/**
 * pages/me.js —— 用户中心
 *
 * 新布局：身份概览 → 功能导航 → 内容工作区。
 * 登录态用于展示角色与管理员入口；未登录时仍可查看本地演示档案。
 */

import { cardList } from "../components/CardList.js";
import { icon } from "../components/Icon.js";
import { getState, subscribe, updateProfile } from "../state.js";
import { getCurrentUser, logout, subscribeAuth, updateCurrentUser } from "../auth.js";
import { h, toast, formatNumber } from "../ui.js";
import { PROVINCES } from "../config.js";
import { getCaseFavorites, removeCaseFavorite } from "../caseFavorites.js";
import { navigate } from "../router.js";
import { avatarNode, fileToAvatarDataUrl } from "../avatar.js";
import { api } from "../api.js";

// icon 用 components/Icon.js 的线描名（原几何符号 ◫◈★☆▤⚙ 在不同字体下粗细/大小不一，观感杂）
const MENU_TABS = [
  { key: "overview", label: "数据总览", icon: "grid" },
  { key: "reports", label: "报告历史", icon: "report" },
  { key: "favorites", label: "我的志愿表", icon: "list" },
  { key: "caseFavs", label: "收藏案例", icon: "bookmark" },
  { key: "services", label: "服务记录", icon: "clock" },
  { key: "settings", label: "账号设置", icon: "gear" },
];

const CASE_PATH_TAG = {
  进取型: "tag--chong",
  稳妥型: "tag--wen",
  保底型: "tag--bao",
  滑档预警: "card__tag--warn",
};

function formatSavedAt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default async function me(mount) {
  let activeTab = "overview";
  let alive = true;
  const accountData = {
    loading: true,
    plans: [],
    reports: [],
    appointments: [],
    profile: null,
  };

  const render = () => {
    if (!alive) return;
    const state = getState();
    const user = getCurrentUser();
    const profile = { ...state.profile, electives: state.profile.electives || [] };
    const prov = PROVINCES[profile.province] || PROVINCES.guangdong;
    const nickname = profile.nickname || user?.name || "访客同学";
    const hasScore = profile.score !== null && profile.score !== undefined && profile.score !== "";
    const subjectCombo = profile.subjectType
      ? `${profile.subjectType}${profile.electives.length ? " + " + profile.electives.join(" + ") : ""}`
      : "未选科";
    const caseFavCount = getCaseFavorites().length;

    const menuItems = MENU_TABS.map((tab) =>
      h("button", {
        class: `account-menu__item${activeTab === tab.key ? " is-active" : ""}`,
        type: "button",
        onclick: () => {
          activeTab = tab.key;
          render();
        },
      }, [
        h("span", { class: "account-menu__icon", html: icon(tab.icon, { size: 16 }) }),
        h("span", { class: "account-menu__label" }, tab.label),
        tab.key === "favorites" && accountData.plans.length
          ? h("span", { class: "account-menu__count" }, String(accountData.plans.length))
          : null,
        tab.key === "reports" && accountData.reports.length
          ? h("span", { class: "account-menu__count" }, String(accountData.reports.length))
          : null,
        tab.key === "caseFavs" && caseFavCount
          ? h("span", { class: "account-menu__count" }, String(caseFavCount))
          : null,
      ])
    );

    const panel = buildPanel(activeTab, {
      state,
      profile,
      user,
      hasScore,
      prov,
      currentYear: state.currentYear,
      accountData,
    });

    mount.replaceChildren(
      h("div", { class: "account-page" }, [
        h("section", { class: "account-hero" }, [
          h("div", { class: "container account-hero__inner" }, [
            h("div", { class: "account-identity" }, [
              avatarNode(user, nickname, "account-identity__avatar"),
              h("div", { class: "account-identity__body" }, [
                h("div", { class: "account-identity__top" }, [
                  h("h1", {}, nickname),
                  h("span", { class: `account-role account-role--${user?.role || "guest"}` },
                    user?.roleLabel || "访客模式"),
                ]),
                h("p", {}, user ? `账号 ${user.username}` : "登录后可同步角色界面与个人入口"),
                h("div", { class: "account-identity__tags" }, [
                  h("span", {}, prov.name),
                  h("span", {}, profile.grade || "高三"),
                  h("span", {}, subjectCombo),
                ]),
              ]),
            ]),
            h("div", { class: "account-hero__metrics" }, [
              metric(hasScore ? formatNumber(profile.score) : "—", "预估分数"),
              metric(profile.rank ? `~${formatNumber(Math.round(profile.rank / 1000))}K` : "—", "省排位次"),
              // 用真实数量代替「画像完整度」这种自造指标
              metric(String(accountData.plans.length || 0), "志愿表方案"),
            ]),
            h("div", { class: "account-hero__actions" }, [
              h("button", { class: "btn btn--primary", onclick: () => navigate("/assessment") }, "继续完善评估"),
              h("button", { class: "btn btn--ghost", onclick: () => navigate("/report") }, "查看路径报告"),
              user ? null : h("a", { class: "btn btn--ghost", href: "#/login" }, "登录账号"),
            ]),
          ]),
        ]),
        h("div", { class: "container account-layout" }, [
          h("aside", { class: "account-sidebar" }, [
            h("nav", { class: "account-menu", "aria-label": "用户中心菜单" }, menuItems),
            h("div", { class: "account-sidebar__tip" }, [
              h("strong", {}, user?.role === "admin" ? "管理员模式" : "填完基本信息，报告会更贴合"),
              h("p", {}, user?.role === "admin"
                ? "管理员账号可直接进入后台，管理数据、用户与内容。"
                : "补充位次、选科和偏好后，推荐结果会更贴近你的真实情况。"),
              user?.role === "admin"
                ? h("button", { class: "btn btn--sm btn--primary", onclick: () => navigate("/admin") }, "进入管理后台")
                : h("button", { class: "btn btn--sm btn--ghost", onclick: () => navigate("/assessment") }, "完善资料"),
            ]),
          ]),
          h("section", { class: "account-panel" }, panel),
        ]),
      ])
    );
  };

  function buildPanel(tab, ctx) {
    const titles = {
      overview: ["数据总览", "快速查看档案完成度、方案沉淀与下一步动作。"],
      reports: ["路径报告", "查看已生成的升学报告与冲稳保方案。"],
      favorites: ["我的志愿表", "查看当前账号已保存的志愿表。"],
      caseFavs: ["收藏案例", "回看真实考生的脱敏决策案例。"],
      services: ["服务记录", "查看预约咨询与专家服务状态。"],
      settings: ["账号设置", "管理个人资料与当前登录状态。"],
    };
    const [title, subtitle] = titles[tab] || titles.overview;
    let content;
    switch (tab) {
      case "reports":
        content = buildReportsPanel(ctx.accountData, ctx.hasScore, ctx.profile, ctx.currentYear);
        break;
      case "favorites":
        content = buildFavoritesPanel(ctx.accountData.plans, ctx.accountData.loading);
        break;
      case "caseFavs":
        content = buildCaseFavsPanel();
        break;
      case "services":
        content = buildServicesPanel(ctx.accountData.appointments, ctx.accountData.loading);
        break;
      case "settings":
        content = buildSettingsPanel(ctx.profile, ctx.user, ctx.prov);
        break;
      default:
        content = buildOverviewPanel(ctx);
    }
    return [
      h("header", { class: "account-panel__head" }, [
        h("div", {}, [
          h("h2", {}, title),
          h("p", {}, subtitle),
        ]),
      ]),
      h("div", { class: "account-panel__body" }, content),
    ];
  }

  function buildOverviewPanel({ state, profile, hasScore, prov }) {
    const actions = [
      {
        title: hasScore ? "继续查看冲稳保报告" : "先完成升学评估",
        desc: hasScore ? "已有分数与选科数据，可直接进入报告页。" : "补充分数、位次与选科后生成个性化方案。",
        action: hasScore ? "/report" : "/assessment",
        label: hasScore ? "查看报告" : "开始评估",
      },
      {
        title: "整理我的志愿表",
        desc: "把感兴趣的院校专业组加入志愿表并调整顺序。",
        action: "/plan",
        label: "打开志愿表",
      },
      {
        title: "浏览院校与专业库",
        desc: "按地域、层次和专业方向继续筛选目标院校。",
        action: "/schools",
        label: "进入院校库",
      },
    ];
    return [
      h("div", { class: "account-overview-grid" }, [
        overviewCard("档案信息", [
          ["省份", prov.name],
          ["选科", profile.subjectType || "未选择"],
          ["位次", profile.rank ? formatNumber(profile.rank) : "未填写"],
          ["志愿表", `${accountData.plans.length} 个方案`],
        ]),
        overviewCard("内容沉淀", [
          ["报告", `${accountData.reports.length} 份`],
          ["收藏案例", `${getCaseFavorites().length} 个`],
          ["服务记录", `${accountData.appointments.length} 条`],
          ["数据版本", String(state.currentYear || 2026)],
        ]),
      ]),
      h("div", { class: "account-action-list" },
        actions.map((item) =>
          h("button", {
            class: "account-action-row",
            type: "button",
            onclick: () => navigate(item.action),
          }, [
            h("span", { class: "account-action-row__body" }, [
              h("strong", {}, item.title),
              h("span", {}, item.desc),
            ]),
            h("span", { class: "account-action-row__link" }, `${item.label} →`),
          ])
        )
      ),
    ];
  }

  function overviewCard(title, rows) {
    return h("section", { class: "account-overview-card" }, [
      h("h3", {}, title),
      h("dl", {}, rows.flatMap(([label, value]) => [
        h("dt", {}, label),
        h("dd", {}, value),
      ])),
    ]);
  }

  function buildReportsPanel(data, hasScore, profile, currentYear) {
    if (data.loading) {
      return h("div", { class: "card-list__empty" }, "正在加载报告历史…");
    }
    if (!data.reports.length) {
      return h("div", { class: "empty-state" }, [
        h("div", { class: "empty-state__title" }, "暂无报告历史"),
        h("div", { class: "empty-state__desc" },
          hasScore ? "打开路径报告后会保存到账号。" : "先完成升学评估，再生成冲稳保报告。"),
        h("button", {
          class: "btn btn--primary",
          onclick: () => navigate(hasScore ? "/report" : "/assessment"),
        }, hasScore ? "生成报告" : "开始评估"),
      ]);
    }
    // 点历史记录先在原地展开摘要；要看最新数据再走「重新测算」
    const detailBox = h("div", { id: "report-detail" });
    const list = cardList(data.reports.map((report) => ({
      title: report.title || "冲稳保路径报告",
      meta: `${report.province || "—"} · ${report.subject_type || "—"}类 · 参考 ${report.current_year || currentYear} 年 · ${report.created_at}`,
      tag: `冲${report.tier_counts?.chong || 0} 稳${report.tier_counts?.wen || 0} 保${report.tier_counts?.bao || 0}`,
      onClick: async () => {
        detailBox.replaceChildren(h("div", { class: "card-list__empty" }, "正在读取这条记录…"));
        try {
          const detail = await api.reportHistoryDetail(report.id);
          detailBox.replaceChildren(
            h("div", { class: "card note-card" }, [
              h("div", { class: "note-card__title" }, `${report.created_at} 的报告摘要`),
              h("p", { class: "note-card__text" }, detail.summary || "这条记录没有摘要。"),
              h("button", {
                class: "btn btn--ghost btn--sm",
                type: "button",
                onclick: () => navigate("/report"),
              }, "按当前分数重新测算 →"),
            ])
          );
        } catch (e) {
          detailBox.replaceChildren(
            h("div", { class: "card-list__empty" }, `读取失败：${e.message}`)
          );
        }
      },
    })), { title: "我的报告历史" });
    return h("div", {}, [list, detailBox]);
  }

  function buildFavoritesPanel(plans, loading) {
    if (loading) {
      return h("div", { class: "card-list__empty" }, "正在加载志愿表…");
    }
    if (!plans.length) {
      return h("div", { class: "empty-state" }, [
        h("div", { class: "empty-state__title" }, "暂无志愿表"),
        h("div", { class: "empty-state__desc" }, "在路径报告中加入院校专业组后，可在这里继续整理。"),
        h("button", { class: "btn btn--primary", onclick: () => navigate("/plan") }, "打开志愿表"),
      ]);
    }
    return cardList(plans.map((plan) => ({
      title: plan.name || "我的志愿表",
      meta: `${plan.year} 年 · ${plan.count}/${plan.capacity} 个院校专业组`,
      tag: plan.province || "未设置省份",
      onClick: () => navigate("/plan"),
    })), { title: "我的志愿表" });
  }

  function buildCaseFavsPanel() {
    const favs = getCaseFavorites();
    return h("div", { class: "card-list-wrap" }, [
      h("h3", { class: "card-list__title" }, "收藏的案例"),
      favs.length
        ? h("div", { class: "card-list" }, favs.map((f) =>
            h("article", {
              class: "card card--row case-fav-row",
              onclick: () => navigate(`/cases/${encodeURIComponent(f.slug)}`),
            }, [
              h("div", { class: "card__body" }, [
                h("div", { class: "card__title" }, [
                  f.title,
                  f.path_type
                    ? h("span", { class: `card__tag ${CASE_PATH_TAG[f.path_type] || ""}` }, f.path_type)
                    : null,
                ]),
                h("div", { class: "card__meta" },
                  `${f.subject_type || "—"}类 · ${f.score_band || "—"} 分 · 位次段 ${f.rank_band || "—"} · 收藏于 ${formatSavedAt(f.saved_at)}`),
              ]),
              h("button", {
                class: "btn btn--ghost btn--sm",
                type: "button",
                onclick: (ev) => {
                  ev.stopPropagation();
                  removeCaseFavorite(f.slug);
                  toast("已取消收藏", "info");
                  render();
                },
              }, "取消收藏"),
            ])
          ))
        : h("div", { class: "empty-state" }, [
            h("div", { class: "empty-state__icon", html: icon("star", { size: 30 }) }),
            h("div", { class: "empty-state__title" }, "暂无收藏案例"),
            h("div", { class: "empty-state__desc" }, "在案例中心点击卡片右上角的星标即可收藏，方便随时回看。"),
          ]),
    ]);
  }

  function buildServicesPanel(appointments, loading) {
    if (loading) {
      return h("div", { class: "card-list__empty" }, "正在加载服务记录…");
    }
    if (!appointments || appointments.length === 0) {
      return h("div", { class: "card-list-wrap" }, [
        h("h3", { class: "card-list__title" }, "服务记录"),
        h("div", { class: "empty-state" }, [
          h("div", { class: "empty-state__icon", html: icon("clock", { size: 30 }) }),
          h("div", { class: "empty-state__title" }, "暂无服务记录"),
          h("div", { class: "empty-state__desc" }, "预约专家咨询后，记录将显示在这里。"),
        ]),
      ]);
    }
    const statusLabel = {
      pending: "待处理",
      contacted: "已联系",
      completed: "已完成",
      cancelled: "已取消",
    };
    return cardList(appointments.map((item) => ({
      title: item.plan_tier,
      meta: `${item.created_at} · 联系人 ${item.contact_name} · ${item.contact_info}`,
      tag: statusLabel[item.status] || item.status,
    })), { title: "服务记录" });
  }

  function buildSettingsPanel(profile, user, prov) {
    const fields = [
      ["登录账号", user ? `${user.username}（${user.roleLabel}）` : "未登录"],
      ["昵称", profile.nickname || "未设置"],
      ["省份", prov.name],
      ["年级", profile.grade || "高三"],
      ["首选科目", profile.subjectType || "未选择"],
      ["再选科目", profile.electives?.join("、") || "未选择"],
      ["预估分数", profile.score ? `${profile.score} 分` : "未填写"],
      ["省排位次", profile.rank ? formatNumber(profile.rank) : "未填写"],
    ];
    return h("div", { class: "settings-card" }, [
      user ? h("div", { class: "settings-avatar" }, [
        avatarNode(user, profile.nickname || user.name, "settings-avatar__preview"),
        h("div", { class: "settings-avatar__body" }, [
          h("strong", {}, "头像"),
          h("span", {}, "支持 JPG、PNG、WebP，上传后自动压缩"),
          h("div", { class: "settings-avatar__actions" }, [
            h("label", { class: "btn btn--ghost btn--sm settings-avatar__upload" }, [
              "上传头像",
              h("input", {
                class: "settings-avatar__file",
                type: "file",
                accept: "image/jpeg,image/png,image/webp",
                onchange: async (e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  try {
                    const dataUrl = await fileToAvatarDataUrl(file);
                    const result = await api.uploadAvatar(dataUrl);
                    updateCurrentUser({ avatarUrl: result.avatar_url });
                    toast("头像已更新", "success");
                    render();
                  } catch (err) {
                    toast(`头像上传失败：${err.message}`, "error");
                  }
                },
              }),
            ]),
            user.avatarUrl
              ? h("button", {
                  class: "btn btn--ghost btn--sm",
                  type: "button",
                  onclick: async () => {
                    try {
                      await api.deleteAvatar();
                      updateCurrentUser({ avatarUrl: "" });
                      toast("头像已删除", "info");
                      render();
                    } catch (err) {
                      toast(`删除失败：${err.message}`, "error");
                    }
                  },
                }, "删除头像")
              : null,
          ]),
        ]),
      ]) : null,
      ...fields.map(([label, value]) =>
        h("div", { class: "settings-row" }, [
          h("div", { class: "settings-row__label" }, label),
          h("div", { class: "settings-row__value" }, value),
        ])
      ),
      h("div", { class: "settings-row settings-row--action" }, [
        h("button", { class: "btn btn--primary", onclick: openEditModal }, "编辑资料"),
        user ? h("button", { class: "btn btn--ghost", onclick: openPasswordModal }, "修改密码") : null,
        user
          ? h("button", {
              class: "btn btn--ghost",
              onclick: () => {
                logout();
                toast("已退出登录", "info");
                navigate("/login");
              },
            }, "退出登录")
          : h("a", { class: "btn btn--ghost", href: "#/login" }, "登录账号"),
      ]),
    ]);
  }

  function openEditModal() {
    const profile = { ...getState().profile, electives: getState().profile.electives || [] };
    const prov = PROVINCES[profile.province] || PROVINCES.guangdong;
    const backdrop = h("div", { class: "modal-backdrop" });
    const modal = h("div", { class: "modal" });
    const nickInput = h("input", { class: "field-input", type: "text", value: profile.nickname || "", placeholder: "如：李同学" });
    const gradeInput = h("select", { class: "field-select" }, [
      h("option", { value: "高一", selected: profile.grade === "高一" }, "高一"),
      h("option", { value: "高二", selected: profile.grade === "高二" }, "高二"),
      h("option", { value: "高三", selected: profile.grade === "高三" || !profile.grade }, "高三"),
      h("option", { value: "复读", selected: profile.grade === "复读" }, "复读"),
    ]);
    const close = () => {
      backdrop.remove();
      modal.remove();
    };
    const save = async () => {
      const nickname = nickInput.value.trim();
      if (!nickname) {
        toast("昵称不能为空", "error");
        return;
      }
      const patch = { nickname, grade: gradeInput.value };
      try {
        if (getCurrentUser()) {
          const result = await api.updateAccountProfile({
            nickname,
            grade: gradeInput.value,
          });
          updateCurrentUser(result.user);
        }
        updateProfile(patch);
        close();
        toast("资料已更新并同步到账号", "success");
        render();
      } catch (e) {
        toast(`保存失败：${e.message}`, "error");
      }
    };
    modal.replaceChildren(
      h("div", { class: "modal__header" }, [
        h("h3", { class: "modal__title" }, "编辑个人资料"),
        h("button", { class: "modal__close", onclick: close }, "×"),
      ]),
      h("div", { class: "modal__body" }, [
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "昵称"), nickInput]),
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "年级"), gradeInput]),
        h("div", { class: "field-group" }, [
          h("label", { class: "field-label" }, "省份"),
          h("div", { class: "field-static" }, prov.name),
        ]),
        h("div", { class: "field-group" }, [
          h("label", { class: "field-label" }, "选科"),
          h("div", { class: "field-static" },
            profile.subjectType
              ? `${profile.subjectType}${profile.electives.length ? "+" + profile.electives.join("+") : ""}`
              : "未选择（可在升学评估页填写）"),
        ]),
      ]),
      h("div", { class: "modal__footer" }, [
        h("button", { class: "btn btn--ghost", onclick: close }, "取消"),
        h("button", { class: "btn btn--primary", onclick: save }, "保存"),
      ])
    );
    document.body.appendChild(backdrop);
    document.body.appendChild(modal);
  }

  function openPasswordModal() {
    const backdrop = h("div", { class: "modal-backdrop" });
    const modal = h("div", { class: "modal" });
    const current = h("input", {
      class: "field-input",
      type: "password",
      autocomplete: "current-password",
      placeholder: "当前密码",
    });
    const next = h("input", {
      class: "field-input",
      type: "password",
      autocomplete: "new-password",
      placeholder: "新密码，至少 8 位",
    });
    const close = () => {
      backdrop.remove();
      modal.remove();
    };
    const save = async () => {
      if (next.value.length < 8) {
        toast("新密码至少 8 位", "error");
        return;
      }
      try {
        await api.changePassword({ current_password: current.value, new_password: next.value });
        close();
        toast("密码已修改", "success");
      } catch (err) {
        toast(`修改失败：${err.message}`, "error");
      }
    };
    modal.replaceChildren(
      h("div", { class: "modal__header" }, [
        h("h3", { class: "modal__title" }, "修改密码"),
        h("button", { class: "modal__close", type: "button", onclick: close }, "×"),
      ]),
      h("div", { class: "modal__body" }, [
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "当前密码"), current]),
        h("div", { class: "field-group" }, [h("label", { class: "field-label" }, "新密码"), next]),
      ]),
      h("div", { class: "modal__footer" }, [
        h("button", { class: "btn btn--ghost", type: "button", onclick: close }, "取消"),
        h("button", { class: "btn btn--primary", type: "button", onclick: save }, "确认修改"),
      ])
    );
    document.body.appendChild(backdrop);
    document.body.appendChild(modal);
  }

  async function loadAccountData() {
    const user = getCurrentUser();
    accountData.loading = true;
    if (!user) {
      accountData.loading = false;
      accountData.plans = [];
      accountData.reports = [];
      accountData.appointments = [];
      accountData.profile = null;
      render();
      return;
    }
    const [plansResult, reportsResult, appointmentsResult, profileResult] =
      await Promise.allSettled([
        api.myPlans(),
        api.reportHistory(),
        api.myExpertAppointments(),
        api.myProfile(),
      ]);
    if (!alive) return;
    accountData.plans = plansResult.status === "fulfilled" ? (plansResult.value.plans || []) : [];
    accountData.reports = reportsResult.status === "fulfilled" ? (reportsResult.value.items || []) : [];
    accountData.appointments = appointmentsResult.status === "fulfilled" ? (appointmentsResult.value.items || []) : [];
    accountData.profile = profileResult.status === "fulfilled"
      ? profileResult.value.profile
      : null;
    if (accountData.profile) {
      const profile = accountData.profile;
      const provinceKey = Object.entries(PROVINCES)
        .find(([, value]) => value.name === profile.province)?.[0] || "guangdong";
      updateProfile({
        profileId: profile.id,
        province: provinceKey,
        subjectType: profile.subject_type || "",
        electives: profile.elective_subjects || [],
        scoreBucket: profile.score_bucket || "",
        rankBucket: profile.rank_bucket || "",
      });
    }
    if (user.nickname || user.grade) {
      updateProfile({
        nickname: user.nickname || getState().profile.nickname,
        grade: user.grade || getState().profile.grade,
      });
    }
    accountData.loading = false;
    render();
  }

  const unsubscribeState = subscribe(render);
  const unsubscribeAuth = subscribeAuth(render);
  window.addEventListener("hashchange", function onLeave() {
    if (!location.hash.startsWith("#/me")) {
      alive = false;
      unsubscribeState();
      unsubscribeAuth();
      window.removeEventListener("hashchange", onLeave);
    }
  });

  render();
  void loadAccountData();
  return {
    destroy: () => {
      alive = false;
      unsubscribeState();
      unsubscribeAuth();
    },
  };
}

function metric(value, label) {
  return h("div", { class: "account-metric" }, [
    h("strong", {}, value),
    h("span", {}, label),
  ]);
}

function profileCompleteness(profile) {
  const checks = [
    Boolean(profile.province),
    Boolean(profile.subjectType),
    profile.electives?.length > 0,
    profile.score !== null && profile.score !== undefined && profile.score !== "",
    Boolean(profile.nickname),
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}
