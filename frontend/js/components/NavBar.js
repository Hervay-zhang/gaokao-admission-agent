/**
 * NavBar.js —— 顶部导航（严格对齐见径）
 * Logo 印章块 + 产品名/副标语 + 6 项导航 + 右侧主 CTA「开始测算 →」
 * 移动端折叠进抽屉（无底部 Tab）。
 */

import { h } from "../ui.js";
import { icon } from "./Icon.js";
import { navigate, parseHash } from "../router.js";
import { subscribe } from "../state.js";
import { getCurrentUser, subscribeAuth } from "../auth.js";
import { avatarNode } from "../avatar.js";

// 导航项固定 6 个，顺序与命名对齐见径；「预约专家」为独立付费服务（非 AI 助手）
const NAV_ITEMS = [
  { label: "首页", path: "/" },
  { label: "路径报告", path: "/report" },
  { label: "院校与专业库", path: "/schools" },
  { label: "案例中心", path: "/cases" },
  { label: "社区", path: "/community" },
  { label: "预约专家", path: "/expert" },
  { label: "我的", path: "/me" },
];

let drawerOpen = false;
let navStateSubscribed = false;
let authStateSubscribed = false;
let navHashSubscribed = false;

export function renderNavBar() {
  const root = document.getElementById("navbar-root");
  if (!root) return;
  const user = getCurrentUser();
  const navItems = user?.role === "admin"
    ? [...NAV_ITEMS, { label: "管理后台", path: "/admin" }]
    : NAV_ITEMS;
  root.replaceChildren(h("nav", { class: "navbar" }, h("div", { class: "navbar__inner container" }, [
    // Logo 印章块 + 品牌
    h("a", { class: "navbar__logo", href: "#/" }, [
      h("span", { class: "navbar__seal" }, "志"),
      h("span", { class: "navbar__brand" }, [
        h("span", { class: "navbar__brand-name" }, "高考志愿助手"),
        h("span", { class: "navbar__brand-slogan" }, "家庭升学与人生路径规划"),
      ]),
    ]),
    // 导航项
    h("div", { class: `navbar__nav${drawerOpen ? " is-open" : ""}`, id: "navbar-nav" },
      navItems.map((item) =>
        h("a", {
          class: `navbar__link${isActive(item.path) ? " is-active" : ""}`,
          "data-nav-path": item.path,
          href: `#${item.path}`,
          onclick: () => { closeDrawer(); },
        }, item.label)
      )
    ),
    // 登录态 + 主 CTA
    h("div", { class: "navbar__actions" }, [
      user
        ? h("a", { class: "navbar__user", href: "#/me", title: `${user.roleLabel} · ${user.name}` }, [
            avatarNode(user, user.name, "navbar__avatar"),
            h("span", { class: "navbar__user-name" }, user.name),
          ])
        : h("a", { class: "navbar__login", href: "#/login" }, "登录"),
      h("button", { class: "navbar__cta", onclick: () => navigate("/assessment") }, ["开始测算 ", h("span", {}, "→")]),
    ]),
    // 移动端菜单
    h("button", {
      class: "navbar__toggle",
      "aria-label": "打开菜单",
      onclick: toggleDrawer,
      html: icon("menu", { size: 20 }),
    }),
  ])));

  syncActiveNav();
  if (!navStateSubscribed) {
    subscribe(syncActiveNav);
    navStateSubscribed = true;
  }
  if (!authStateSubscribed) {
    subscribeAuth(renderNavBar);
    authStateSubscribed = true;
  }
  if (!navHashSubscribed) {
    window.addEventListener("hashchange", () => {
      requestAnimationFrame(syncActiveNav);
    });
    navHashSubscribed = true;
  }
}

function isActive(path) {
  const cur = parseHash();
  if (path === "/") return cur === "/";
  return cur === path || cur.startsWith(path + "/");
}

function toggleDrawer() {
  drawerOpen = !drawerOpen;
  document.getElementById("navbar-nav")?.classList.toggle("is-open", drawerOpen);
}

function closeDrawer() {
  drawerOpen = false;
  document.getElementById("navbar-nav")?.classList.remove("is-open");
}

/** 路由状态变化后，同步顶部导航的激活项与红色下划线。 */
function syncActiveNav() {
  document.querySelectorAll(".navbar__link[data-nav-path]").forEach((link) => {
    link.classList.toggle("is-active", isActive(link.dataset.navPath));
  });
}
