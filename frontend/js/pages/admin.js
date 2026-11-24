/**
 * pages/admin.js —— 管理后台（T31 落地，替代 T06 骨架）
 *
 * 内部工具页（不在主导航，路由 #/admin）：
 * - Token 门控：输入 .env 的 ADMIN_TOKEN（存 localStorage，仅本机浏览器）；错误 Token 一律 404。
 * - 功能区：总览（数据量/当前版本/限流阈值）、年份窗口切换（含 dry_run 预览）、
 *   幂等导入触发与状态、最近反馈列表、免责文案只读清单（红线常量，无修改入口）。
 */

import { api } from "../api.js";
import { pageShell } from "../components/PageShell.js";
import { getCurrentUser } from "../auth.js";

const TOKEN_KEY = "gaokao_admin_token";
const TABLE_LABELS = {
  provinces: "省份",
  score_segments: "一分一段",
  universities: "院校",
  admission_lines: "投档线",
  policy_docs: "政策文档",
  policy_chunks: "政策切片",
  sessions: "会话",
  messages: "消息",
  feedback: "反馈",
};
const DISCLAIMER_LABELS = {
  recommend_report: "推荐报告",
  chat_stream: "AI 对话流",
  share_snapshot: "分享快照",
};
const FEEDBACK_KIND_LABELS = { up: "有帮助", down: "不准确", correction: "纠错" };
const FEEDBACK_DIMENSION_LABELS = { answer: "对话回答", recommend: "推荐报告" };

const h = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  });
  kids.flat().forEach((k) => node.append(k instanceof Node ? k : document.createTextNode(String(k ?? ""))));
  return node;
};

export default async function admin(mount) {
  const tokenInput = h("input", {
    class: "admin-field", type: "password", placeholder: "运维访问口令（未登录管理员账号时填写）",
    value: localStorage.getItem(TOKEN_KEY) || "",
  });
  const msg = h("p", { class: "admin-msg" }, "");
  const overviewBox = h("div", { class: "card" });
  const versionBox = h("div", { class: "card" });
  const importBox = h("div", { class: "card" });
  const feedbackBox = h("div", { class: "card" });
  const disclaimerBox = h("div", { class: "card" });
  const quickLinksBox = h("div", { class: "card" });
  const appointmentBox = h("div", { class: "card" });
  const usersBox = h("div", { class: "card" });
  const universitiesBox = h("div", { class: "card" });
  const reportsBox = h("div", { class: "card" });
  const auditBox = h("div", { class: "card" });

  const token = () => tokenInput.value.trim();

  async function loadAll() {
    msg.textContent = "";
    try {
      await loadOverview();
    } catch (e) {
      msg.textContent = `访问失败（HTTP ${e.status || "网络"}）：Token 无效或后端不可达（管理端点对错误 Token 返回 404）`;
      return;
    }
    void Promise.allSettled([
      loadVersions(),
      loadImportState(),
      loadFeedback(),
      loadDisclaimer(),
      loadQuickLinks(),
      loadAppointments(),
      loadUsers(),
      loadUniversities(),
      loadCommunityReports(),
      loadAuditLogs(),
    ]).then((results) => {
      const failed = results.find((item) => item.status === "rejected");
      if (failed) {
        msg.textContent = `部分后台数据加载失败：${failed.reason?.message || "请稍后重试"}`;
      }
    });
  }

  // ---- 快捷链接管理（页面底部；含默认官方平台，可增删改/启停/排序）----
  const groupLabels = { gaokao: "高考平台", official: "官方入口", tool: "实用工具" };
  let quickLinkFormState = { mode: "create", id: null }; // 编辑复用同一表单

  async function loadQuickLinks(highlight) {
    const d = await api.adminQuickLinks(token());
    renderQuickLinksTable(d.items || [], highlight);
  }

  /** 渲染快捷链接表格 + 表单；highlight 为新保存项 id（闪一下）。 */
  function renderQuickLinksTable(items, highlight) {
    const quickForm = buildQuickLinkForm(items);
    const table = h("table", { class: "admin-table" },
      h("thead", {}, h("tr", {},
        h("th", {}, "图标"), h("th", {}, "名称"), h("th", {}, "链接"), h("th", {}, "分组"),
        h("th", {}, "排序"), h("th", {}, "状态"), h("th", {}, "操作"))),
      h("tbody", {}, items.map((it) => {
        const row = h("tr", {}, [
          h("td", {}, it.icon || "—"),
          h("td", {}, it.label),
          h("td", {}, h("a", { href: it.url, target: "_blank", rel: "noopener noreferrer" }, it.url)),
          h("td", {}, groupLabels[it.group] || it.group),
          h("td", {}, String(it.sort_order)),
          h("td", {}, it.is_active ? "启用" : "停用"),
          h("td", { class: "admin-row" },
            h("button", { class: "btn", onclick: () => fillQuickLinkForm(items, it) }, "编辑"),
            h("button", {
              class: "btn btn--danger",
              onclick: async () => {
                if (!window.confirm(`确定删除快捷链接「${it.label}」？`)) return;
                try {
                  await api.adminDeleteQuickLink(token(), it.id);
                  await loadQuickLinks();
                } catch (e) {
                  msg.textContent = `删除失败：${e.payload?.detail?.code || e.message}`;
                }
              },
            }, "删除"),
          ),
        ]);
        if (highlight === it.id) row.style.background = "#fff8e6";
        return row;
      })),
    );
    quickLinksBox.replaceChildren(
      h("div", { class: "card__body" },
        h("h3", { class: "card__title" }, `快捷链接（底部导航 · 共 ${items.length} 条）`),
        quickForm,
        table,
      ),
    );
  }

  /** 快捷链接新增/编辑表单（编辑时预填并切换保存逻辑）。 */
  function buildQuickLinkForm(items) {
    const fLabel = h("input", { class: "admin-field", placeholder: "名称（如 阳光高考）" });
    const fUrl = h("input", { class: "admin-field quick-form__url", placeholder: "https://…" });
    const fIcon = h("input", { class: "admin-field quick-form__small", placeholder: "图标字" });
    const fGroup = h("select", { class: "admin-field quick-form__small" },
      Object.entries(groupLabels).map(([k, v]) => h("option", { value: k }, v)));
    const fSort = h("input", { class: "admin-field quick-form__small", type: "number", value: "10" });
    const fActive = h("select", { class: "admin-field quick-form__small" },
      h("option", { value: "1" }, "启用"), h("option", { value: "0" }, "停用"));
    const formMsg = h("span", { class: "admin-msg" }, "");
    const submitBtn = h("button", { class: "btn btn--primary" }, "新增");

    function resetForm() {
      quickLinkFormState = { mode: "create", id: null };
      fLabel.value = ""; fUrl.value = ""; fIcon.value = "";
      fGroup.value = "gaokao"; fSort.value = "10"; fActive.value = "1";
      submitBtn.textContent = "新增";
      formMsg.textContent = "";
    }

    submitBtn.onclick = async () => {
      const payload = {
        label: fLabel.value.trim(),
        url: fUrl.value.trim(),
        icon: fIcon.value.trim(),
        group: fGroup.value,
        sort_order: Number(fSort.value) || 0,
        is_active: fActive.value === "1",
      };
      if (!payload.label || !payload.url) {
        formMsg.textContent = "名称与链接必填";
        return;
      }
      try {
        if (quickLinkFormState.mode === "edit") {
          await api.adminUpdateQuickLink(token(), quickLinkFormState.id, payload);
          formMsg.textContent = "已保存更新";
        } else {
          await api.adminCreateQuickLink(token(), payload);
          formMsg.textContent = "已新增";
        }
        await loadQuickLinks();
      } catch (e) {
        formMsg.textContent = `保存失败：${e.payload?.detail?.code || e.message}`;
      }
    };

    return h("div", { class: "admin-quick-form" }, [
      h("div", { class: "admin-row" }, fLabel, fUrl, fIcon, fGroup, fSort, fActive, submitBtn,
        h("button", { class: "btn", onclick: resetForm }, "清空")),
      formMsg,
    ]);
  }

  /** 点「编辑」：回填表单并切到更新模式。 */
  function fillQuickLinkForm(items, it) {
    quickLinkFormState = { mode: "edit", id: it.id };
    const inputs = quickLinksBox.querySelectorAll(".admin-quick-form input, .admin-quick-form select");
    inputs[0].value = it.label;
    inputs[1].value = it.url;
    inputs[2].value = it.icon || "";
    inputs[3].value = it.group || "gaokao";
    inputs[4].value = String(it.sort_order ?? 0);
    inputs[5].value = it.is_active ? "1" : "0";
    const submitBtn = quickLinksBox.querySelector(".admin-quick-form .btn--primary");
    if (submitBtn) submitBtn.textContent = "保存修改";
    const msgs = quickLinksBox.querySelectorAll(".admin-quick-form .admin-msg");
    if (msgs.length) msgs[0].textContent = `正在编辑：「${it.label}」（id=${it.id}）`;
    quickLinksBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function loadOverview() {
    const d = await api.adminOverview(token());
    overviewBox.replaceChildren(      h("div", { class: "card__body" },
      h("h3", { class: "card__title" }, "数据与运行总览"),
      h("p", {}, Object.entries(d.table_counts)
        .map(([k, v]) => `${TABLE_LABELS[k] || k}：${v}`)
        .join("　")),
      h("p", {}, `当前版本：${d.data_version.current_year} 年 / 参考 ${(d.data_version.ref_years || []).join("、")} / 权重 ${(d.data_version.year_weights || []).join("、")}`),
      h("p", {}, `反馈总数：${d.feedback_total}　|　限流：单 IP ${d.ratelimit.ip_per_min}/分钟、流式连接并发 ${d.ratelimit.sse_concurrent}　|　模型模式：${d.mock_mode ? "模拟" : "真实"}`),
    ));
  }

  async function loadVersions() {
    const d = await api.adminVersions(token());
    const yearInput = h("input", { class: "admin-field", type: "number", placeholder: "新年份（如 2027）" });
    const noteInput = h("input", { class: "admin-field", type: "text", placeholder: "切换备注" });
    const out = h("p", { class: "admin-msg" }, "");
    const doSwitch = (dryRun) => async () => {
      try {
        const r = await api.adminSwitch(token(), {
          current_year: Number(yearInput.value) || undefined,
          note: noteInput.value,
          dry_run: dryRun,
        });
        out.textContent = `${dryRun ? "【预览】校验通过" : "已切换"}：${r.active.current_year} / 参考 ${r.active.ref_years.join(",")} / 权重 ${r.active.year_weights.join(",")}`;
        if (!dryRun) await loadOverview();
      } catch (e) {
        out.textContent = `失败：${e.payload?.detail?.code || e.message}`;
      }
    };
    versionBox.replaceChildren(      h("div", { class: "card__body" },
      h("h3", { class: "card__title" }, `数据版本（当前 ${d.active.current_year}）`),
      h("p", {}, `历史快照（近 20 条）：${d.snapshots.map((s) => `#${s.id}→${s.config.current_year}`).join("、") || "无"}`),
      h("div", { class: "admin-row" }, yearInput, noteInput,
        h("button", { class: "btn", onclick: doSwitch(true) }, "预览"),
        h("button", { class: "btn btn--primary", onclick: doSwitch(false) }, "正式切换")),
      out,
    ));
  }

  async function loadImportState() {
    const d = await api.adminImportStatus(token());
    const out = h("p", { class: "admin-msg" }, "");
    const btn = h("button", {
      class: "btn btn--primary",
      onclick: async () => {
        try {
          await api.adminImportTrigger(token());
          out.textContent = "已触发，后台执行中…";
          setTimeout(loadImportState, 4000);
        } catch (e) {
          out.textContent = `触发失败：${e.payload?.detail?.code || e.message}`;
        }
      },
    }, "触发幂等导入");
    importBox.replaceChildren(      h("div", { class: "card__body" },
      h("h3", { class: "card__title" }, "数据导入（幂等重灌）"),
      h("p", {}, `状态：${d.running ? "执行中" : "空闲"}　|　开始 ${d.started_at || "—"}　|　结束 ${d.finished_at || "—"}　|　结果 ${d.result || d.error || "—"}`),
      h("div", { class: "admin-row" }, btn, out),
    ));
  }

  async function loadFeedback() {
    const d = await api.adminFeedback(token(), 30);
    feedbackBox.replaceChildren(      h("div", { class: "card__body" },
      h("h3", { class: "card__title" }, `最近反馈（${d.total} 条）`),
      h("table", { class: "admin-table" },
        h("thead", {}, h("tr", {},
          h("th", {}, "编号"), h("th", {}, "类型"), h("th", {}, "维度"), h("th", {}, "原因/纠错"), h("th", {}, "时间"))),
        h("tbody", {}, d.items.map((f) => h("tr", {},
          h("td", {}, String(f.feedback_id)),
          h("td", {}, FEEDBACK_KIND_LABELS[f.kind] || f.kind),
          h("td", {}, FEEDBACK_DIMENSION_LABELS[f.dimension] || f.dimension || "—"),
          h("td", {}, f.reason || f.correction || "—"),
          h("td", {}, f.created_at || "—"))))),
    ));
  }

  async function loadDisclaimer() {
    const d = await api.adminDisclaimer(token());
    disclaimerBox.replaceChildren(      h("div", { class: "card__body" },
      h("h3", { class: "card__title" }, "免责文案（红线常量 · 只读）"),
      h("p", { class: "admin-msg" }, d.note),
      h("ul", {}, Object.entries(d.entries).map(([k, v]) =>
        h("li", {}, `${DISCLAIMER_LABELS[k] || k}：${v}`)
      )),
    ));
  }

  let appointmentFilter = { status: "", keyword: "" };

  async function loadAppointments() {
    const d = await api.adminExpertAppointments(token(), appointmentFilter);
    const statuses = ["pending", "contacted", "completed", "cancelled"];
    const label = { pending: "待处理", contacted: "已联系", completed: "已完成", cancelled: "已取消" };
    const statusSelect = h("select", { class: "admin-field" }, [
      h("option", { value: "" }, "全部状态"),
      ...statuses.map((status) =>
        h("option", { value: status, selected: appointmentFilter.status === status }, label[status])
      ),
    ]);
    statusSelect.onchange = () => {
      appointmentFilter.status = statusSelect.value;
      void loadAppointments();
    };
    const keywordInput = h("input", {
      class: "admin-field",
      placeholder: "用户名 / 联系人 / 联系方式",
      value: appointmentFilter.keyword,
    });
    keywordInput.onkeydown = (e) => {
      if (e.key === "Enter") {
        appointmentFilter.keyword = keywordInput.value.trim();
        void loadAppointments();
      }
    };
    appointmentBox.replaceChildren(
      h("div", { class: "card__body" },
        h("div", { class: "admin-section-head" }, [
          h("h3", { class: "card__title" }, `专家预约（${d.total} 条）`),
          h("div", { class: "admin-row" }, [
            statusSelect,
            keywordInput,
            h("button", {
              class: "btn btn--sm btn--ghost",
              type: "button",
              onclick: () => loadAppointments().catch((e) => { msg.textContent = `预约刷新失败：${e.message}`; }),
            }, "刷新预约"),
            h("button", {
              class: "btn btn--sm",
              type: "button",
              onclick: () => exportAppointmentsCsv(d.items || []),
            }, "导出 CSV"),
          ]),
        ]),
        d.items.length ? h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "编号"), h("th", {}, "用户"), h("th", {}, "方案"),
            h("th", {}, "联系人"), h("th", {}, "联系方式"), h("th", {}, "状态"), h("th", {}, "时间"))),
          h("tbody", {}, d.items.map((item) => h("tr", {},
            h("td", {}, String(item.id)),
            h("td", {}, item.username),
            h("td", {}, `${item.plan_tier} / ¥${item.price}`),
            h("td", {}, item.contact_name),
            h("td", {}, item.contact_info),
            h("td", {}, h("select", {
              class: "admin-field",
              onchange: async (e) => {
                try {
                  await api.adminUpdateExpertAppointment(token(), item.id, e.target.value);
                  msg.textContent = `预约 #${item.id} 状态已保存为「${label[e.target.value]}」`;
                  await loadAppointments();
                } catch (err) {
                  msg.textContent = `预约状态更新失败：${err.message}`;
                }
              },
            }, statuses.map((status) =>
              h("option", { value: status, selected: item.status === status }, label[status])
            ))),
            h("td", {}, item.created_at)
          )))
        ) : h("div", { class: "empty-state empty-state--sm" }, [
          h("div", { class: "empty-state__title" }, "暂无专家预约"),
          h("div", { class: "empty-state__desc" }, "学生或家长提交预约后，会在这里显示。"),
        ])
      )
    );
  }

  function exportAppointmentsCsv(items) {
    const rows = [
      ["编号", "用户", "方案", "价格", "联系人", "联系方式", "状态", "时间", "备注"],
      ...items.map((item) => [
        item.id,
        item.username,
        item.plan_tier,
        item.price,
        item.contact_name,
        item.contact_info,
        item.status,
        item.created_at,
        item.note || "",
      ]),
    ];
    const csv = "\ufeff" + rows
      .map((row) => row.map((cell) => `"${String(cell ?? "").replaceAll('"', '""')}"`).join(","))
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: "expert-appointments.csv" });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function loadUsers(keyword = "") {
    const d = await api.adminUsers(token(), { keyword });
    const search = h("input", {
      class: "admin-field",
      placeholder: "搜索用户名 / 昵称",
      value: keyword,
    });
    search.onkeydown = (e) => {
      if (e.key === "Enter") void loadUsers(search.value.trim());
    };
    usersBox.replaceChildren(
      h("div", { class: "card__body" }, [
        h("div", { class: "admin-section-head" }, [
          h("h3", { class: "card__title" }, `账号管理（${d.total} 个）`),
          h("button", { class: "btn btn--sm", onclick: () => loadUsers(search.value.trim()) }, "搜索"),
        ]),
        search,
        h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "账号"), h("th", {}, "昵称"), h("th", {}, "角色"),
            h("th", {}, "状态"), h("th", {}, "最近登录"), h("th", {}, "密码"))),
          h("tbody", {}, d.items.map((user) =>
            h("tr", {},
              h("td", {}, user.username),
              h("td", {}, user.name),
              h("td", {}, h("select", {
                class: "admin-field",
                onchange: async (e) => {
                  try {
                    await api.adminUpdateUser(token(), user.username, {
                      role: e.target.value,
                      is_active: user.is_active,
                    });
                    msg.textContent = `账号 ${user.username} 角色已更新`;
                    await loadUsers(search.value.trim());
                  } catch (err) {
                    msg.textContent = `账号更新失败：${err.message}`;
                  }
                },
              }, [
                h("option", { value: "student", selected: user.role === "student" }, "考生"),
                h("option", { value: "parent", selected: user.role === "parent" }, "家长"),
                h("option", { value: "admin", selected: user.role === "admin" }, "管理员"),
              ])),
              h("td", {}, h("button", {
                class: `btn btn--sm ${user.is_active ? "" : "btn--danger"}`,
                onclick: async () => {
                  try {
                    await api.adminUpdateUser(token(), user.username, {
                      role: user.role,
                      is_active: !user.is_active,
                    });
                    msg.textContent = `账号 ${user.username} 已${user.is_active ? "停用" : "启用"}`;
                    await loadUsers(search.value.trim());
                  } catch (err) {
                    msg.textContent = `账号更新失败：${err.message}`;
                  }
                },
              }, user.is_active ? "已启用" : "已停用")),
              h("td", {}, user.last_login_at || "—"),
              h("td", {}, h("button", {
                class: "btn btn--sm",
                type: "button",
                onclick: async () => {
                  const pwd = window.prompt(`为「${user.username}」设置新密码（至少 8 位）`);
                  if (pwd === null) return;
                  if (pwd.length < 8) {
                    msg.textContent = "新密码至少 8 位";
                    return;
                  }
                  try {
                    await api.adminUpdateUser(token(), user.username, {
                      role: user.role,
                      is_active: user.is_active,
                      new_password: pwd,
                    });
                    msg.textContent = `已重置「${user.username}」的密码，请提醒本人登录后自行修改`;
                  } catch (err) {
                    msg.textContent = `重置失败：${err.message}`;
                  }
                },
              }, "重置")),
            )
          )),
        ),
      ])
    );
  }

  async function loadUniversities(keyword = "") {
    const d = await api.adminUniversities(token(), { keyword, page: 1, page_size: 20 });
    const search = h("input", {
      class: "admin-field",
      placeholder: "搜索院校名称 / 代码",
      value: keyword,
    });
    search.onkeydown = (e) => {
      if (e.key === "Enter") void loadUniversities(search.value.trim());
    };
    universitiesBox.replaceChildren(
      h("div", { class: "card__body" }, [
        h("div", { class: "admin-section-head" }, [
          h("h3", { class: "card__title" }, `院校招生官网维护（${d.total} 所）`),
          h("button", { class: "btn btn--sm", onclick: () => loadUniversities(search.value.trim()) }, "搜索"),
        ]),
        search,
        h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "院校"), h("th", {}, "招生官网"), h("th", {}, "核验状态"),
            h("th", {}, "更新时间"), h("th", {}, "操作"))),
          h("tbody", {}, d.items.map((uni) => {
            const input = h("input", {
              class: "admin-field quick-form__url",
              placeholder: "https://…",
              value: uni.admission_url,
            });
            const status = h("select", { class: "admin-field quick-form__small" }, [
              h("option", { value: "已核", selected: uni.admission_url_status === "已核" }, "已核"),
              h("option", { value: "待核", selected: uni.admission_url_status === "待核" }, "待核"),
              h("option", { value: "不可用", selected: uni.admission_url_status === "不可用" }, "不可用"),
            ]);
            return h("tr", {},
              h("td", {}, `${uni.name}（${uni.code}）`),
              h("td", {}, input),
              h("td", {}, status),
              h("td", {}, uni.admission_url_updated_at || "—"),
              h("td", {}, h("button", {
                class: "btn btn--sm btn--primary",
                onclick: async () => {
                  try {
                    await api.adminUpdateUniversity(token(), uni.code, {
                      admission_url: input.value.trim(),
                      admission_url_status: status.value,
                    });
                    msg.textContent = `${uni.name} 招生官网已保存`;
                    await loadUniversities(search.value.trim());
                  } catch (err) {
                    msg.textContent = `官网保存失败：${err.message}`;
                  }
                },
              }, "保存")),
            );
          })),
        ),
      ])
    );
  }

  async function loadCommunityReports(status = "pending") {
    const d = await api.adminCommunityReports(token(), status ? { status } : {});
    const statusSelect = h("select", { class: "admin-field" }, [
      h("option", { value: "pending", selected: status === "pending" }, "待处理"),
      h("option", { value: "resolved", selected: status === "resolved" }, "已处理"),
      h("option", { value: "dismissed", selected: status === "dismissed" }, "已驳回"),
      h("option", { value: "", selected: !status }, "全部"),
    ]);
    statusSelect.onchange = () => void loadCommunityReports(statusSelect.value);
    reportsBox.replaceChildren(
      h("div", { class: "card__body" }, [
        h("div", { class: "admin-section-head" }, [
          h("h3", { class: "card__title" }, `社区举报审核（${d.total} 条）`),
          statusSelect,
        ]),
        d.items.length ? h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "目标"), h("th", {}, "原因"), h("th", {}, "说明"),
            h("th", {}, "状态"), h("th", {}, "时间"), h("th", {}, "操作"))),
          h("tbody", {}, d.items.map((report) =>
            h("tr", {},
              h("td", {}, `${report.target_type === "post" ? "帖子" : "评论"} #${report.target_id}`),
              h("td", {}, report.reason),
              h("td", {}, report.detail || "—"),
              h("td", {}, report.status),
              h("td", {}, report.created_at),
              h("td", { class: "admin-row" }, [
                report.status === "pending"
                  ? h("button", {
                      class: "btn btn--sm btn--primary",
                      onclick: async () => {
                        await api.adminResolveCommunityReport(token(), report.id, "resolved");
                        if (report.target_type === "post") {
                          await api.adminDeleteCommunityPost(token(), report.target_id);
                        } else {
                          await api.adminDeleteCommunityComment(token(), report.target_id);
                        }
                        msg.textContent = `举报 #${report.id} 已处理并删除内容`;
                        await loadCommunityReports(status);
                      },
                    }, "删除并结案")
                  : null,
                report.status === "pending"
                  ? h("button", {
                      class: "btn btn--sm",
                      onclick: async () => {
                        await api.adminResolveCommunityReport(token(), report.id, "dismissed");
                        await loadCommunityReports(status);
                      },
                    }, "驳回")
                  : null,
              ]),
            )
          )),
        ) : h("div", { class: "empty-state empty-state--sm" }, [
          h("div", { class: "empty-state__title" }, "暂无举报记录"),
        ]),
      ])
    );
  }

  // ---- 顶部 Tab 分组导航（T32）----
  // 组 → 卡片映射；点 tab 只显示当前组的卡片，数据仍一次性加载，切换零等待。
  const ADMIN_GROUPS = [
    { key: "overview", label: "总览", boxes: [overviewBox, versionBox] },
    { key: "data", label: "数据管理", boxes: [importBox, universitiesBox] },
    { key: "users", label: "用户与预约", boxes: [usersBox, appointmentBox] },
    { key: "content", label: "内容管理", boxes: [quickLinksBox, disclaimerBox, feedbackBox, reportsBox] },
    { key: "audit", label: "日志", boxes: [auditBox] },
  ];
  const ALL_BOXES = ADMIN_GROUPS.flatMap((group) => group.boxes);

  function showAdminGroup(key) {
    const group = ADMIN_GROUPS.find((item) => item.key === key) || ADMIN_GROUPS[0];
    for (const box of ALL_BOXES) box.style.display = group.boxes.includes(box) ? "" : "none";
    for (const btn of tabsBar.querySelectorAll(".admin-tabs__item")) {
      btn.classList.toggle("is-active", btn.dataset.group === group.key);
    }
    try { localStorage.setItem("gaokao_admin_tab", group.key); } catch { /* 私有模式忽略 */ }
  }

  const tabsBar = h("div", { class: "admin-tabs" },
    ...ADMIN_GROUPS.map((group) =>
      h("button", {
        class: "admin-tabs__item",
        type: "button",
        "data-group": group.key,
        onclick: () => showAdminGroup(group.key),
      }, group.label)
    )
  );

  function buildAdminTabs() {
    let saved = "overview";
    try { saved = localStorage.getItem("gaokao_admin_tab") || "overview"; } catch { /* ignore */ }
    // DOM 就绪后再应用分组（卡片此时才挂进文档）
    window.setTimeout(() => showAdminGroup(saved), 0);
    return tabsBar;
  }

  async function loadAuditLogs() {
    const d = await api.adminAuditLogs(token(), 100);
    auditBox.replaceChildren(
      h("div", { class: "card__body" }, [
        h("h3", { class: "card__title" }, `操作日志（最近 ${d.total} 条）`),
        d.items.length ? h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "时间"), h("th", {}, "动作"), h("th", {}, "操作者"), h("th", {}, "详情"))),
          h("tbody", {}, d.items.map((item) =>
            h("tr", {},
              h("td", {}, item.created_at),
              h("td", {}, item.action),
              h("td", {}, item.actor || "—"),
              h("td", {}, item.detail || "—"),
            )
          )),
        ) : h("div", { class: "card-list__empty" }, "暂无操作日志"),
      ])
    );
  }

  const currentUser = getCurrentUser();
  const isAdminUser = currentUser?.role === "admin";

  mount.replaceChildren(pageShell({
    eyebrow: "内部 · 不在主导航",
    title: "管理后台",
    subtitle: "数据总览、年份窗口切换、数据导入与用户反馈，按功能分五个分组。",
    body: [
      // 已用管理员账号登录：走 Bearer 令牌鉴权，不再要求手填访问口令（那个框只会让人困惑）。
      isAdminUser
        ? h("div", { class: "admin-login" }, [
            h("span", { class: "admin-login__who" }, `已登录管理员账号：${currentUser.name}`),
            h("button", { class: "btn btn--primary", onclick: () => loadAll() }, "刷新数据"),
          ])
        : h("div", { class: "admin-login" }, [
            tokenInput,
            h("button", {
              class: "btn btn--primary",
              onclick: () => { localStorage.setItem(TOKEN_KEY, token()); loadAll(); },
            }, "进入 / 刷新"),
            h("button", { class: "btn", onclick: () => { localStorage.removeItem(TOKEN_KEY); tokenInput.value = ""; msg.textContent = "Token 已清除"; } }, "清除"),
          ]),
      msg,
      buildAdminTabs(),
      overviewBox, versionBox, importBox, usersBox, appointmentBox,
      quickLinksBox, disclaimerBox, feedbackBox, reportsBox, universitiesBox, auditBox,
    ],
  }));

  if (localStorage.getItem(TOKEN_KEY) || isAdminUser) await loadAll();
  if (isAdminUser) {
    const timer = window.setInterval(() => {
      if (!location.hash.startsWith("#/admin")) {
        window.clearInterval(timer);
        return;
      }
      loadAppointments().catch(() => {});
    }, 30000);
  }
}
