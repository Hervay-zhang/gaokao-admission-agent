/**
 * pages/login.js —— 统一登录 / 注册页
 *
 * 视觉参考 CHSI 账号登录：蓝色品牌头、白色登录面板、清晰的双栏信息层级。
 * 不展示任何演示账号（公网暴露账号密码属安全隐患）；考生/家长自助注册，
 * 管理员账号由运维创建（后台鉴权实现见 pages/admin.js，不在用户界面暴露技术细节）。
 */

import { h, toast } from "../ui.js";
import { fetchCaptcha, getCurrentUser, login, logout, register } from "../auth.js";
import { navigate, parseQuery } from "../router.js";

export default async function loginPage(mount) {
  activateLoginMode();
  const existingUser = getCurrentUser();
  if (existingUser) {
    renderAlreadyLogged(mount, existingUser);
    return;
  }

  const query = parseQuery();
  const redirect = safeRedirect(query.redirect);

  // ---- 模式页签（复用 .login-role-tabs 样式）----
  let mode = "login"; // login | register
  const modeTabs = h("div", { class: "login-role-tabs" });

  // ---- 登录表单 ----
  const loginUsername = h("input", {
    class: "login-field__input",
    id: "login-username",
    type: "text",
    autocomplete: "username",
    placeholder: "请输入账号",
  });
  const loginPassword = h("input", {
    class: "login-field__input",
    id: "login-password",
    type: "password",
    autocomplete: "current-password",
    placeholder: "请输入密码",
  });
  const loginMessage = h("div", { class: "login-message", role: "alert" });
  const remember = h("input", { type: "checkbox", checked: true });

  const loginPasswordToggle = h("button", {
    class: "login-field__toggle",
    type: "button",
    "aria-label": "显示密码",
    onclick: () => {
      const showing = loginPassword.type === "text";
      loginPassword.type = showing ? "password" : "text";
      loginPasswordToggle.textContent = showing ? "显示" : "隐藏";
      loginPasswordToggle.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
    },
  }, "显示");

  async function submitLogin(event) {
    event.preventDefault();
    const result = await login(loginUsername.value, loginPassword.value, {
      remember: remember.checked,
    });
    if (!result.ok) {
      loginMessage.textContent = result.message;
      return;
    }
    loginMessage.textContent = "";
    toast(`欢迎回来，${result.user.name}`, "success");
    navigate(redirect || (result.user.role === "admin" ? "/admin" : "/me"));
  }

  const loginForm = h("form", { class: "login-form", onsubmit: submitLogin }, [
    h("label", { class: "login-field", for: "login-username" }, [
      h("span", { class: "login-field__label" }, "账号"),
      loginUsername,
    ]),
    h("label", { class: "login-field", for: "login-password" }, [
      h("span", { class: "login-field__label" }, "密码"),
      h("span", { class: "login-field__control" }, [
        loginPassword,
        loginPasswordToggle,
      ]),
    ]),
    h("div", { class: "login-form__options" }, [
      h("label", { class: "login-remember" }, [remember, h("span", {}, "记住登录状态")]),
      h("button", {
        class: "login-forgot",
        type: "button",
        onclick: () => toast("请联系管理员重置：管理员可在后台「账号管理」里设置新密码", "info"),
      }, "忘记密码"),
    ]),
    loginMessage,
    h("button", { class: "btn btn--primary login-submit", type: "submit" }, "登录"),
  ]);

  // ---- 注册表单 ----
  const regUsername = h("input", {
    class: "login-field__input",
    id: "register-username",
    type: "text",
    autocomplete: "username",
    placeholder: "3~32 位，字母开头，可含数字/下划线",
  });
  const regNickname = h("input", {
    class: "login-field__input",
    id: "register-nickname",
    type: "text",
    maxlength: "32",
    placeholder: "选填，默认与账号相同",
  });
  const regPassword = h("input", {
    class: "login-field__input",
    id: "register-password",
    type: "password",
    autocomplete: "new-password",
    placeholder: "至少 8 位",
  });
  const regMessage = h("div", { class: "login-message", role: "alert" });
  let regRole = "student";
  const regRoleTabs = h("div", { class: "login-role-tabs" });

  // ---- 图形验证码（防批量注册；验证码一次性，失败自动换一张）----
  let captchaId = "";
  const captchaInput = h("input", {
    class: "login-captcha__input",
    id: "register-captcha",
    type: "text",
    maxlength: "8",
    autocomplete: "off",
    placeholder: "请输入图中字符",
  });
  const captchaImg = h("img", {
    class: "login-captcha__img",
    alt: "图形验证码",
    title: "点击换一张",
  });
  const captchaRefresh = h("button", {
    class: "login-captcha__refresh",
    type: "button",
    onclick: (e) => {
      e.preventDefault();
      refreshCaptcha();
    },
  }, "换一张");
  captchaImg.addEventListener("click", () => refreshCaptcha());

  async function refreshCaptcha() {
    captchaId = "";
    captchaInput.value = "";
    const got = await fetchCaptcha();
    if (!got.ok) {
      captchaImg.removeAttribute("src");
      captchaImg.alt = got.message || "验证码获取失败";
      return;
    }
    captchaId = got.captchaId;
    captchaImg.src = got.dataUrl;
    captchaImg.alt = "图形验证码";
  }

  function renderRegRoleTabs() {
    regRoleTabs.replaceChildren(
      ...[
        { key: "student", label: "我是考生" },
        { key: "parent", label: "我是家长" },
      ].map((item) =>
        h("button", {
          class: `login-role-tabs__item${regRole === item.key ? " is-active" : ""}`,
          type: "button",
          onclick: () => {
            regRole = item.key;
            renderRegRoleTabs();
          },
        }, item.label)
      )
    );
  }

  async function submitRegister(event) {
    event.preventDefault();
    if (regPassword.value.length < 8) {
      regMessage.textContent = "密码至少 8 位";
      return;
    }
    if (!captchaInput.value.trim()) {
      regMessage.textContent = "请填写图形验证码";
      return;
    }
    regMessage.textContent = "注册中…";
    const result = await register({
      username: regUsername.value,
      password: regPassword.value,
      nickname: regNickname.value.trim(),
      role: regRole,
      captchaId,
      captchaCode: captchaInput.value.trim(),
    });
    if (!result.ok) {
      const wait = result.retryAfter ? `，请 ${result.retryAfter} 秒后再试` : "";
      regMessage.textContent = `${result.message}${wait}`;
      // 验证码一次性：无论失败原因都换一张，避免用户拿已作废的码反复提交
      refreshCaptcha();
      return;
    }
    toast(`注册成功，欢迎你，${result.user.name}`, "success");
    navigate(redirect || "/assessment");
  }

  renderRegRoleTabs();

  const registerForm = h("form", { class: "login-form", onsubmit: submitRegister }, [
    h("label", { class: "login-field", for: "register-username" }, [
      h("span", { class: "login-field__label" }, "账号"),
      regUsername,
    ]),
    h("label", { class: "login-field", for: "register-nickname" }, [
      h("span", { class: "login-field__label" }, "昵称"),
      regNickname,
    ]),
    h("label", { class: "login-field", for: "register-password" }, [
      h("span", { class: "login-field__label" }, "密码"),
      regPassword,
    ]),
    h("div", { class: "login-field" }, [
      h("span", { class: "login-field__label" }, "验证码"),
      h("div", { class: "login-captcha" }, [
        captchaInput,
        captchaImg,
        captchaRefresh,
      ]),
    ]),
    h("div", { class: "login-field" }, [
      h("span", { class: "login-field__label" }, "身份"),
      regRoleTabs,
    ]),
    regMessage,
    h("button", { class: "btn btn--primary login-submit", type: "submit" }, "注册并登录"),
  ]);

  function renderModeTabs() {
    modeTabs.replaceChildren(
      ...[
        { key: "login", label: "登录" },
        { key: "register", label: "注册新账号" },
      ].map((item) =>
        h("button", {
          class: `login-role-tabs__item${mode === item.key ? " is-active" : ""}`,
          type: "button",
          onclick: () => {
            mode = item.key;
            loginForm.style.display = mode === "login" ? "" : "none";
            registerForm.style.display = mode === "register" ? "" : "none";
            loginMessage.textContent = "";
            regMessage.textContent = "";
            renderModeTabs();
            // 切到注册页签时按需取一张验证码（登录模式不消耗签发配额）
            if (mode === "register" && !captchaId) refreshCaptcha();
          },
        }, item.label)
      )
    );
  }
  registerForm.style.display = "none";
  renderModeTabs();

  mount.replaceChildren(
    h("div", { class: "login-page" }, [
      h("div", { class: "login-shell" }, [
        h("section", { class: "login-showcase" }, [
          h("div", { class: "login-showcase__veil" }),
          h("div", { class: "login-showcase__content" }, [
            h("span", { class: "login-showcase__eyebrow" }, "统一身份认证"),
            h("h1", { class: "login-showcase__title" },
              "每一次志愿选择，都值得更清晰的判断。"),
            h("p", { class: "login-showcase__desc" },
              "不登录也可以直接测算；登录后可跨设备保存评估信息、路径报告与志愿表。"),
          ]),
        ]),
        h("section", { class: "login-panel" }, [
          h("a", { class: "login-panel__back", href: "#/" }, "← 返回首页"),
          h("div", { class: "login-panel__brand" }, [
            h("span", { class: "login-panel__seal" }, "志"),
            h("div", {}, [
              h("h1", {}, "高考志愿助手"),
              h("p", {}, "升学路径规划与数据参考"),
            ]),
          ]),
          h("div", { class: "login-panel__heading" }, [
            h("h2", {}, "账号登录"),
            h("p", {}, "已有账号直接登录；没有账号可免费注册。"),
          ]),
          modeTabs,
          loginForm,
          registerForm,
          h("p", { class: "login-panel__note" },
            "无需注册即可试用评估与升学问答；登录后历史记录会跟随账号保存，换设备也能继续。"),
        ]),
      ]),
    ])
  );
}

function activateLoginMode() {
  document.body.classList.add("login-page-active");
  const cleanup = () => {
    if (!location.hash.startsWith("#/login")) {
      document.body.classList.remove("login-page-active");
      window.removeEventListener("hashchange", cleanup);
    }
  };
  window.addEventListener("hashchange", cleanup);
}

function safeRedirect(value) {
  if (!value || typeof value !== "string") return "";
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/login")) return "";
  return value;
}

function renderAlreadyLogged(mount, user) {
  mount.replaceChildren(
    h("div", { class: "container login-state-page" }, [
      h("div", { class: "login-state-card" }, [
        h("div", { class: "login-state-card__avatar" }, user.avatar || user.name.slice(0, 1)),
        h("h1", {}, `已登录：${user.name}`),
        h("p", {}, `当前身份：${user.roleLabel} · ${user.username}`),
        h("div", { class: "login-state-card__actions" }, [
          h("button", {
            class: "btn btn--primary",
            onclick: () => navigate(user.role === "admin" ? "/admin" : "/me"),
          }, user.role === "admin" ? "进入管理后台" : "进入我的"),
          h("button", {
            class: "btn btn--ghost",
            onclick: () => {
              logout();
              navigate("/login");
            },
          }, "退出登录"),
        ]),
      ]),
    ])
  );
}
