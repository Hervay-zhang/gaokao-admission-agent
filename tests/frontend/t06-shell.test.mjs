/**
 * T06 前端外壳验收测试（Playwright，无框架 runner）
 *
 * 覆盖任务卡 T06 的 J1–J7：
 *   J1 主要路由可导航且互不报错
 *   J2 顶栏结构 = Logo 印章块 + 7 项导航 + 右侧主 CTA
 *   J3 首页 Hero = 状态徽章 + 两行衬线大标题 + 双按钮
 *   J4 「我的」页为左统计 + 中菜单 + 右列表三栏
 *   J5 表单校验：分数 0–750、科类必选
 *   J6 375px 不破版（三栏正确折叠）；1024px 展开横向导航
 *   J7 控制台零报错（ES Module 加载正确）
 *
 * 运行（在项目根目录）：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   "C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-6/node.exe" \
 *   tests/frontend/t06-shell.test.mjs
 *
 * 退出码 0 = 全通过；非 0 = 存在失败用例。
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { existsSync } from "node:fs";

// ---- 解析 playwright：ESM 不认 NODE_PATH，这里按固定工作区路径解析 ----
const PW_WORKSPACE = process.env.PW_WORKSPACE
  || "C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules";
const pwEntry = path.join(PW_WORKSPACE, "playwright", "index.js");
if (!existsSync(pwEntry)) {
  console.error(`未找到 playwright（期望路径：${pwEntry}）。请设置环境变量 PW_WORKSPACE 指向含 playwright 的 node_modules 目录。`);
  process.exit(2);
}
const pwMod = await import(pathToFileURL(pwEntry).href);
const { chromium } = pwMod.default ?? pwMod;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = path.resolve(__dirname, "../../frontend");
const PORT = 5599;
const BASE = `http://127.0.0.1:${PORT}`;

const ROUTES = [
  "/", "/assessment", "/report", "/schools", "/chat", "/login",
  "/cases", "/expert", "/plan", "/me", "/community",
  "/community/1", "/user/demo", "/admin",
];

// ---------------- 极简静态文件服务器（仅服务 frontend/） ----------------
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

function startServer() {
  const server = createServer(async (req, res) => {
    try {
      const urlPath = decodeURIComponent(req.url.split("?")[0]);
      const rel = urlPath === "/" ? "/index.html" : urlPath;
      const filePath = path.join(FRONTEND_DIR, rel);
      // 目录穿越防护
      if (!filePath.startsWith(FRONTEND_DIR)) {
        res.writeHead(403).end("forbidden");
        return;
      }
      const buf = await readFile(filePath);
      res.writeHead(200, { "Content-Type": MIME[path.extname(filePath)] || "application/octet-stream" });
      res.end(buf);
    } catch {
      res.writeHead(404).end("not found");
    }
  });
  return new Promise((resolve) => server.listen(PORT, "127.0.0.1", () => resolve(server)));
}

// ---------------- 断言工具 ----------------
const results = [];
function check(id, name, ok, detail = "") {
  results.push({ id, name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  [${id}] ${name}${detail ? "  → " + detail : ""}`);
}
function eq(id, name, actual, expected) {
  check(id, name, actual === expected, `期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
}

// ---------------- 主流程 ----------------
async function main() {
  const server = await startServer();
  const browser = await chromium.launch();

/** 打开页面并收集控制台错误 / 页面错误。 */
async function openPage(context, route) {
  const page = await context.newPage();
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  await page.route("https://fonts.gstatic.com/**", (route) => route.abort());
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const t = msg.text();
    // 离线运行时，后端健康探测（:8000）必然 CORS/网络失败，属预期降级，不计为前端错误
    if (t.includes("127.0.0.1:8000") || t.includes("Failed to load resource")) return;
    errors.push(`console.error: ${t}`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("requestfailed", (req) => {
    // 字体等外部资源离线失败、后端探测失败均属预期，不计错
    const u = req.url();
    if (u.includes("fonts.googleapis.com") || u.includes("fonts.gstatic.com") || u.includes("127.0.0.1:8000")) return;
    errors.push(`requestfailed: ${u}`);
  });
  await page.goto(BASE + "/#" + route, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => {
      const app = document.getElementById("app");
      return Boolean(app && app.children.length > 0);
    },
    null,
    { timeout: 3000 },
  );
  return { page, errors };
}

  // ============ J1 主要路由可导航且互不报错 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const first = await openPage(context, "/");
    const page = first.page;
    const allErrors = [...first.errors];

    for (const route of ROUTES) {
      await page.evaluate((r) => { window.location.hash = "#" + r; }, route);
      await page.waitForFunction(
        () => {
          const app = document.getElementById("app");
          return Boolean(app && app.children.length > 0);
        },
        null,
        { timeout: 3000 },
      );
      const mounted = await page.evaluate(() => {
        const app = document.getElementById("app");
        return app ? app.children.length : 0;
      });
      check("J1", `路由 ${route} 渲染出内容`, mounted > 0, `#app 子节点数=${mounted}`);
    }
    // 汇总所有路由过程中的控制台错误
    check("J1", "主要路由导航全程无 console/page 错误", allErrors.length === 0, allErrors.slice(0, 3).join(" | "));
    await context.close();
  }

  // ============ J2 顶栏结构 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/");

    const seal = await page.locator(".navbar__seal").count();
    check("J2", "顶栏含 Logo 印章块（.navbar__seal）", seal === 1, `count=${seal}`);

    const navCount = await page.locator(".navbar__nav .navbar__link").count();
    eq("J2", "顶部导航恰为 7 项", navCount, 7);

    const ctaText = (await page.locator(".navbar__cta").innerText()).replace(/\s+/g, "");
    check("J2", "右侧主 CTA 文案为「开始测算→」", ctaText.includes("开始测算") && ctaText.includes("→"), `实际「${ctaText}」`);

    const ctaCount = await page.locator(".navbar__cta").count();
    eq("J2", "主 CTA 唯一", ctaCount, 1);

    const tabbar = await page.locator(".tabbar, .bottom-tab, .navbar__tabbar").count();
    eq("J2", "无底部 Tab 结构（未自行发明）", tabbar, 0);

    await page.locator(".navbar__nav .navbar__link", { hasText: "社区" }).click();
    await page.waitForFunction(() => location.hash === "#/community");
    const activeText = (await page.locator(".navbar__link.is-active").innerText()).trim();
    eq("J2", "点击顶部栏目后激活项跟随", activeText, "社区");
    const underlineColor = await page.locator(".navbar__link.is-active").evaluate(
      (el) => getComputedStyle(el, "::after").backgroundColor
    );
    check("J2", "激活项显示朱砂红下划线", underlineColor.includes("192, 57, 43"), `color=${underlineColor}`);

    check("J2", "顶栏渲染过程无错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J3 首页 Hero 结构 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/");

    const badge = await page.locator(".hero__badge").count();
    check("J3", "Hero 含状态徽章（.hero__badge）", badge === 1, `count=${badge}`);

    // 两行大标题：以 <br> 分行，且未压缩成单行
    const heroHtml = await page.locator(".hero__title").innerHTML();
    const brCount = (heroHtml.match(/<br\s*\/?>/gi) || []).length;
    eq("J3", "Hero 大标题为两行（含 1 个 <br> 分行）", brCount, 1);

    const titleRenderedLines = await page.locator(".hero__title").evaluate((el) => {
      // 行高 × 渲染高度 ≈ 行数
      const cs = getComputedStyle(el);
      const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;
      return Math.round(el.getBoundingClientRect().height / lh);
    });
    check("J3", "Hero 大标题渲染为两行", titleRenderedLines >= 2, `实测行数≈${titleRenderedLines}`);

    const titleFont = await page.locator(".hero__title").evaluate((el) => getComputedStyle(el).fontFamily);
    check("J3", "Hero 标题使用衬线字体", /serif|Songti|SimSun|Noto Serif/i.test(titleFont), `font-family=${titleFont}`);

    const btnCount = await page.locator(".hero__actions .btn").count();
    eq("J3", "Hero 含双按钮", btnCount, 2);

    check("J3", "Hero 渲染过程无错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J4 「我的」新版工作台 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/me");

    const hasIdentity = await page.locator(".account-hero .account-identity").count();
    check("J4", "顶部展示用户身份概览", hasIdentity === 1, `count=${hasIdentity}`);

    const statItems = await page.locator(".account-hero .account-metric").count();
    check("J4", "身份概览含至少 3 项指标", statItems >= 3, `count=${statItems}`);

    const menuItems = await page.locator(".account-menu .account-menu__item").count();
    check("J4", "功能导航包含完整入口", menuItems >= 5, `count=${menuItems}`);

    const panel = await page.locator(".account-layout .account-panel").count();
    check("J4", "右侧渲染内容工作区", panel === 1, `count=${panel}`);

    const cols = await page.locator(".account-layout").evaluate((el) =>
      getComputedStyle(el).gridTemplateColumns.trim().split(/\s+/).length
    );
    eq("J4", "≥1024px 用户中心为两列布局", cols, 2);

    check("J4", "「我的」页渲染过程无错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J5 表单校验 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/assessment");

    const form = page.locator("#score-form");
    eq("J5", "评估页含分数表单（#score-form）", await form.count(), 1);

    const submit = page.locator("#score-form button[type=submit]");

    // 初始：无分数 + 无科类 → 禁用
    check("J5", "空表单提交按钮禁用", await submit.isDisabled());

    // 越界分数 800 → 禁用 + 提示
    await page.fill("#score-field", "800");
    await page.waitForTimeout(80);
    check("J5", "分数 800（越界）提交按钮禁用", await submit.isDisabled());
    const hint800 = await page.locator("#score-field + .score-input__hint").innerText();
    check("J5", "越界提示含 0–750", /0–750|0-750/.test(hint800), `hint=「${hint800}」`);

    // 合法分数 600，但未选科类 → 仍禁用
    await page.fill("#score-field", "600");
    await page.waitForTimeout(80);
    check("J5", "分数 600 但未选科类 → 提交按钮仍禁用", await submit.isDisabled());

    // 选首选科目（物理）
    await page.locator('#primary-row [data-subject="物理"]').click();
    await page.waitForTimeout(60);
    check("J5", "仅选首选科目（未选再选）→ 仍禁用", await submit.isDisabled());

    // 再选科目恰好 2 门
    await page.locator('#elective-row [data-elective="化学"]').click();
    await page.locator('#elective-row [data-elective="生物"]').click();
    await page.waitForTimeout(60);
    check("J5", "分数 600 + 物理 + 再选 2 门 → 提交按钮启用", !(await submit.isDisabled()));

    // 负数分数
    await page.fill("#score-field", "-5");
    await page.waitForTimeout(80);
    check("J5", "分数 -5（越界）提交按钮禁用", await submit.isDisabled());

    // 非整数
    await page.fill("#score-field", "600.5");
    await page.waitForTimeout(80);
    check("J5", "分数 600.5（非整数）提交按钮禁用", await submit.isDisabled());

    // 恢复合法并提交 → 当前第 1 步完成建档与位次测算，仍停留在测评页
    await page.fill("#score-field", "600");
    await page.waitForTimeout(80);
    await submit.click();
    await page.waitForTimeout(260);
    const hash = await page.evaluate(() => location.hash);
    eq("J5", "合法提交后停留在测评页", hash, "#/assessment");

    // 提交后精确分数只写入会话暂存，避免长期留在 localStorage
    const scoreShown = await page.evaluate(() => {
      const localRaw = localStorage.getItem("gaokao_state_v2");
      const sessionRaw = sessionStorage.getItem("gaokao_sensitive_v1");
      return {
        local: localRaw ? JSON.parse(localRaw)?.profile?.score ?? null : null,
        session: sessionRaw ? JSON.parse(sessionRaw)?.score ?? null : null,
      };
    });
    eq("J5", "提交后精确分数不写入 localStorage", scoreShown.local, null);
    eq("J5", "提交后精确分数写入会话暂存", Number(scoreShown.session), 600);

    check("J5", "表单校验过程无错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J6 响应式：375px 不破版 / 1024px 展开横向导航 ============
  {
    // --- 375px 移动端 ---
    const mobile = await browser.newContext({ viewport: { width: 375, height: 720 }, isMobile: true, hasTouch: true });
    const { page } = await openPage(mobile, "/me");

    // 无横向溢出
    const overflow = await page.evaluate(() => ({
      scrollW: document.documentElement.scrollWidth,
      clientW: document.documentElement.clientWidth,
    }));
    check("J6", "375px 无横向滚动溢出", overflow.scrollW <= overflow.clientW + 1,
      `scrollWidth=${overflow.scrollW} clientWidth=${overflow.clientW}`);

    // 用户中心折叠为单列
    const colsMobile = await page.locator(".account-layout").evaluate((el) =>
      getComputedStyle(el).gridTemplateColumns.trim().split(/\s+/).length
    );
    eq("J6", "375px 用户中心折叠为单列", colsMobile, 1);

    // 导航折叠进抽屉（默认隐藏）
    const navDisplay = await page.locator(".navbar__nav").evaluate((el) => getComputedStyle(el).display);
    eq("J6", "375px 导航折叠（display:none）", navDisplay, "none");

    // 抽屉按钮可见，点击后展开
    const toggleVisible = await page.locator(".navbar__toggle").isVisible();
    check("J6", "375px 抽屉按钮可见", toggleVisible);
    await page.locator(".navbar__toggle").click();
    await page.waitForTimeout(120);
    const navDisplayOpen = await page.locator(".navbar__nav").evaluate((el) => getComputedStyle(el).display);
    check("J6", "375px 点击后拖屉展开（display:flex）", navDisplayOpen === "flex", `display=${navDisplayOpen}`);

    // 375px 下 Hero 按钮整宽（不破版）
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForTimeout(200);
    const btnW = await page.locator(".hero__actions .btn").first().evaluate((el) => el.getBoundingClientRect().width);
    check("J6", "375px 按钮宽度不溢出（整宽）", btnW > 0 && btnW <= 375, `btn width=${Math.round(btnW)}`);

    await mobile.close();

    // --- 1024px 桌面 ---
    const desktop = await browser.newContext({ viewport: { width: 1024, height: 800 } });
    const { page: dpage } = await openPage(desktop, "/me");

    const navDisplayD = await dpage.locator(".navbar__nav").evaluate((el) => getComputedStyle(el).display);
    check("J6", "1024px 横向导航展开（display:flex）", navDisplayD === "flex", `display=${navDisplayD}`);

    const toggleHidden = await dpage.locator(".navbar__toggle").evaluate((el) => getComputedStyle(el).display);
    eq("J6", "1024px 抽屉按钮隐藏", toggleHidden, "none");

    const colsD = await dpage.locator(".account-layout").evaluate((el) =>
      getComputedStyle(el).gridTemplateColumns.trim().split(/\s+/).length
    );
    eq("J6", "1024px 用户中心为两列", colsD, 2);

    await desktop.close();
  }

  // ============ J8 登录与角色入口 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/login");

    check("J8", "登录页渲染认证面板", await page.locator(".login-shell").count() === 1);
    eq("J8", "提供 3 个演示账号", await page.locator(".login-demo__account").count(), 3);

    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");
    const studentRole = await page.evaluate(() => JSON.parse(localStorage.getItem("gaokao_auth_v1") || "null")?.role);
    eq("J8", "考生账号登录后进入用户中心", studentRole, "student");
    eq("J8", "顶部显示登录用户", await page.locator(".navbar__user").count(), 1);

    await page.locator(".navbar__user").click();
    await page.waitForFunction(() => location.hash === "#/me");
    check("J8", "登录流程无控制台错误", errors.length === 0, errors.join(" | "));
    await context.close();

    const adminContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const adminPage = await adminContext.newPage();
    await adminPage.route("https://fonts.googleapis.com/**", (route) => route.abort());
    await adminPage.route("https://fonts.gstatic.com/**", (route) => route.abort());
    await adminPage.goto(BASE + "/#/login", { waitUntil: "domcontentloaded" });
    await adminPage.waitForTimeout(500);
    await adminPage.locator('[data-demo-user="admin"]').click();
    const overviewResponse = adminPage.waitForResponse(
      (resp) => resp.url().includes("/api/admin/overview") && resp.status() === 200,
      { timeout: 30000 },
    );
    await adminPage.locator(".login-submit").click();
    await adminPage.waitForFunction(() => location.hash === "#/admin");
    await overviewResponse;
    await adminPage.waitForFunction(
      () => document.getElementById("app")?.innerText.includes("政策切片"),
      null,
      { timeout: 10000 },
    );
    const adminRole = await adminPage.evaluate(() => JSON.parse(localStorage.getItem("gaokao_auth_v1") || "null")?.role);
    eq("J8", "管理员账号登录后进入后台", adminRole, "admin");
    check("J8", "管理员专属导航出现", await adminPage.locator('.navbar__link[data-nav-path="/admin"]').count() === 1);
    check(
      "J8",
      "管理员后台加载数据总览",
      await adminPage.locator("#app").innerText().then((text) => text.includes("政策切片")),
    );
    await adminContext.close();
  }

  // ============ J9 路由切换回到顶部 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/");
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(80);
    await page.evaluate(() => document.querySelector(".hero__actions .btn--ghost")?.click());
    await page.waitForFunction(
      () => location.hash === "#/cases" && window.scrollY <= 2,
      null,
      { timeout: 3000 },
    );
    const scrollY = await page.evaluate(() => window.scrollY);
    check("J9", "路由切换后回到页面顶部", scrollY <= 2, `scrollY=${scrollY}`);
    await context.close();
  }

  // ============ J10 多账号档案隔离 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/login");

    await page.locator('[data-demo-user="admin"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/admin");
    await page.evaluate(async () => {
      const stateModule = await import("/js/state.js");
      stateModule.updateProfile({ nickname: "超级管理员" });
    });

    await page.evaluate(async () => {
      const authModule = await import("/js/auth.js");
      authModule.logout();
    });
    await page.waitForFunction(
      () => !localStorage.getItem("gaokao_auth_v1") && !sessionStorage.getItem("gaokao_auth_v1"),
    );
    await page.goto(BASE + "/#/login", { waitUntil: "networkidle" });
    await page.waitForSelector('[data-demo-user="student"]', { state: "visible", timeout: 10000 });
    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");

    const studentNickname = (await page.locator(".account-identity h1").innerText()).trim();
    eq("J10", "学生账号保持自己的昵称", studentNickname, "李同学");
    const stored = await page.evaluate(() => ({
      admin: JSON.parse(localStorage.getItem("gaokao_state_v2:admin") || "null")?.profile?.nickname,
      student: JSON.parse(localStorage.getItem("gaokao_state_v2:student") || "null")?.profile?.nickname,
    }));
    eq("J10", "管理员昵称保存在管理员档案", stored.admin, "超级管理员");
    eq("J10", "学生昵称保存在学生档案", stored.student, "李同学");

    await context.close();
  }

  // ============ J11 社区评论可提交 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/login");
    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");
    await page.evaluate(() => { location.hash = "#/community"; });
    await page.waitForSelector(".post-card:not(.post-card--skeleton)");
    await page.locator(".post-card").first().click();
    await page.waitForSelector("#comment-input");
    const submit = page.locator(".comment-input__footer button");
    check("J11", "空评论时提交按钮禁用", await submit.isDisabled());
    await page.fill("#comment-input", "这是一条准备发表的评论");
    await page.waitForTimeout(60);
    check("J11", "输入评论后提交按钮启用", !(await submit.isDisabled()));
    await context.close();
  }

  // ============ J12 案例中心可加载并进入详情 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/cases");
    await page.waitForSelector(".case-card");
    eq("J12", "案例中心展示 5 个案例", await page.locator(".case-card").count(), 5);
    check("J12", "案例卡片包含类型和统计信息", await page.locator(".case-card__type").count() >= 5
      && await page.locator(".case-card__stats").count() >= 5);
    await page.locator(".case-card").first().click();
    await page.waitForFunction(() => location.hash.startsWith("#/cases/case_"));
    await page.waitForSelector(".case-detail__layout");
    check("J12", "案例详情可正常打开", await page.locator(".case-detail__layout").count() === 1);
    check("J12", "案例中心无控制台错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J13 AI 对话工作区 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/chat");
    check("J13", "AI 页面渲染独立对话工作区", await page.locator(".chat-workspace").count() === 1);
    check("J13", "AI 页面包含消息面板", await page.locator(".chat-panel").count() === 1);
    check("J13", "AI 页面包含对话框输入区", await page.locator(".chat-composer #chat-input").count() === 1);
    check("J13", "AI 页面包含欢迎消息气泡", await page.locator(".chat-msg--assistant .chat-msg__bubble").count() >= 1);
    check("J13", "AI 页面包含快捷问题引导", await page.locator(".chat-prompt").count() >= 4);
    check("J13", "AI 页面无控制台错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J14 2027 文案与专家服务方案 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/");
    const badge = await page.locator(".hero__badge").innerText();
    check("J14", "首页徽章更新为 2027", badge.includes("2027"), badge);
    const footer = await page.locator(".footer-bottom__copy").innerText();
    check("J14", "页脚版权更新为 2027", footer.includes("2027"), footer);

    await page.evaluate(() => { location.hash = "#/expert"; });
    await page.waitForSelector(".plan-card");
    const prices = await page.locator(".plan-card__price").allInnerTexts();
    eq("J14", "专家服务价格档位正确", prices.join("|"), "免费|¥699|¥1,999");
    const pointCounts = await page.locator(".plan-card__points").evaluateAll((lists) =>
      lists.map((list) => list.querySelectorAll("li").length)
    );
    check("J14", "付费越高服务项越多", pointCounts[0] < pointCounts[1] && pointCounts[1] < pointCounts[2],
      pointCounts.join("→"));
    await context.close();
  }

  // ============ J15 首页咨询悬浮窗 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/");
    await page.waitForSelector(".consultation-ticker");
    check("J15", "首页展示滚动咨询浮窗", await page.locator(".consultation-ticker").count() === 1);
    check("J15", "咨询浮窗单次展示一条消息", await page.locator(".consultation-ticker__viewport").count() === 1);
    const before = await page.locator("#consultation-ticker-root").boundingBox();
    await page.waitForTimeout(700);
    const after = await page.locator("#consultation-ticker-root").boundingBox();
    check("J15", "咨询浮窗在画面中持续移动",
      Math.abs(after.x - before.x) > 1 || Math.abs(after.y - before.y) > 1,
      `${before.x},${before.y} → ${after.x},${after.y}`);
    check("J15", "带链接的咨询可供学生家长点击", await page.locator(".consultation-ticker__viewport.is-link").count() >= 1);
    eq("J15", "普通用户不显示咨询编辑按钮", await page.locator(".consultation-ticker__edit").count(), 0);
    check("J15", "咨询浮窗无控制台错误", errors.length === 0, errors.join(" | "));
    await context.close();
  }

  // ============ J16 管理员咨询编辑与社区置顶 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/login");
    await page.locator('[data-demo-user="admin"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/admin");
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForSelector(".consultation-ticker__edit");
    check("J16", "管理员首页显示咨询编辑按钮", await page.locator(".consultation-ticker__edit").count() === 1);
    await page.locator(".consultation-ticker__edit").click();
    await page.waitForSelector(".consultation-editor");
    check("J16", "管理员可打开咨询编辑窗口", await page.locator(".consultation-editor__input").count() >= 2);
    check("J16", "咨询编辑支持独立链接输入", await page.locator(".consultation-editor__input--url").count() >= 1);
    await page.locator(".modal__close").click();

    await page.evaluate(() => { location.hash = "#/community"; });
    await page.waitForSelector(".post-card:not(.post-card--skeleton)");
    await page.waitForSelector(".post-card__admin-pin");
    check("J16", "管理员在社区看到置顶操作", await page.locator(".post-card__admin-pin").count() >= 1);
    await context.close();
  }

  // ============ J17 用户头像上传 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/login");
    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");
    await page.locator(".account-menu__item", { hasText: "账号设置" }).click();
    await page.waitForSelector(".settings-avatar__file", { state: "attached" });
    await page.locator(".settings-avatar__file").setInputFiles({
      name: "avatar.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl0kX0AAAAASUVORK5CYII=",
        "base64"
      ),
    });
    await page.waitForSelector(".navbar__avatar--image", { timeout: 15000 });
    check("J17", "学生上传头像后顶部立即更新", await page.locator(".navbar__avatar--image").count() === 1);

    await page.evaluate(async () => {
      const authModule = await import("/js/auth.js");
      authModule.logout();
    });
    await page.waitForFunction(
      () => !localStorage.getItem("gaokao_auth_v1") && !sessionStorage.getItem("gaokao_auth_v1"),
    );
    await page.goto(BASE + "/?logout=1#/login", { waitUntil: "networkidle" });
    await page.waitForSelector('[data-demo-user="student"]', { state: "visible", timeout: 10000 });
    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");
    await page.waitForSelector(".navbar__avatar--image", { timeout: 15000 });
    check("J17", "重新登录后头像仍然保留", await page.locator(".navbar__avatar--image").count() === 1);
    await context.close();
  }

  // ============ J18 404 路由不执行 HTML 注入 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/");
    await page.evaluate(() => {
      location.hash = "#/%3Cimg%20src%3Dx%20onerror%3Dwindow.__xss%3D1%3E";
    });
    await page.waitForSelector(".placeholder");
    const injected = await page.evaluate(() => window.__xss === 1);
    check("J18", "404 页面不会执行编码后的 HTML", injected === false);
    check("J18", "404 页面正常显示提示文本",
      (await page.locator(".placeholder").innerText()).includes("页面不存在"));
    await context.close();
  }

  // ============ J19 专家预约表单 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page } = await openPage(context, "/login");
    await page.locator('[data-demo-user="student"]').click();
    await page.locator(".login-submit").click();
    await page.waitForFunction(() => location.hash === "#/me");
    await page.goto(BASE + "/#/expert", { waitUntil: "networkidle" });
    await page.waitForSelector(".plan-card");
    await page.locator(".plan-card").nth(1).locator(".plan-card__cta").click();
    await page.waitForSelector(".expert-booking");
    check("J19", "专家方案可打开预约表单", await page.locator(".expert-booking").count() === 1);
    check("J19", "预约表单包含联系人和联系方式",
      await page.locator(".expert-booking .field-input").count() >= 2);
    await page.locator(".expert-booking .modal__close").click();
    await context.close();
  }

  // ============ J7 控制台零报错 ============
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const { page, errors } = await openPage(context, "/");
    // 走一遍所有路由，累计错误
    for (const route of ROUTES) {
      await page.evaluate((r) => { location.hash = "#" + r; }, route);
      await page.waitForTimeout(150);
    }
    // 每个模块都被当作 ES Module 正确加载？检查 api.js 已挂上 window 能力标记（main.js 会探测后端）
    const moduleOk = await page.evaluate(() => {
      // 若 ES Module 加载失败，app 会为空 → 这里应已有内容
      const app = document.getElementById("app");
      return Boolean(app && app.children.length > 0);
    });
    check("J7", "ES Module 加载正常（应用已挂载）", moduleOk);
    check("J7", "全路由零控制台/页面错误", errors.length === 0, errors.slice(0, 5).join(" | "));
    await context.close();
  }

  await browser.close();
  server.close();

  // ---------------- 汇总 ----------------
  const failed = results.filter((r) => !r.ok);
  console.log("\n================ 汇总 ================");
  console.log(`用例总数：${results.length}，通过：${results.length - failed.length}，失败：${failed.length}`);
  if (failed.length) {
    console.log("\n失败用例：");
    failed.forEach((f) => console.log(`  [${f.id}] ${f.name}  ${f.detail}`));
  }
  console.log("======================================\n");
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error("测试执行异常：", e);
  process.exit(2);
});
