/** 375 / 768 / 1440 三档页面视觉与基础无障碍回归。 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const workspace = process.env.PW_WORKSPACE
  || "C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules";
const playwrightEntry = path.join(workspace, "playwright", "index.js");
if (!existsSync(playwrightEntry)) {
  console.error(`未找到 Playwright：${playwrightEntry}`);
  process.exit(2);
}
const playwright = await import(pathToFileURL(playwrightEntry).href);
const { chromium } = playwright.default ?? playwright;

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(here, "../../frontend");
const port = 5601;
const base = `http://127.0.0.1:${port}`;
const routes = ["/", "/schools", "/cases", "/community", "/chat", "/me", "/login"];
const viewports = [
  { name: "mobile", width: 375, height: 812 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 1000 },
];
const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

const server = createServer(async (req, res) => {
  try {
    const clean = decodeURIComponent(req.url.split("?")[0]);
    const rel = clean === "/" ? "/index.html" : clean;
    const filePath = path.join(frontendDir, rel);
    if (!filePath.startsWith(frontendDir)) throw new Error("forbidden");
    const body = await readFile(filePath);
    res.writeHead(200, { "Content-Type": mime[path.extname(filePath)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
});

await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true });
const failures = [];

for (const viewport of viewports) {
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
  for (const route of routes) {
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/#${route}`, { waitUntil: "domcontentloaded", timeout: 15000 });
    await page.waitForTimeout(1200);

    const audit = await page.evaluate(() => {
      const root = document.getElementById("app");
      const buttons = [...document.querySelectorAll("button")];
      const images = [...document.querySelectorAll("img")];
      const unlabeledButtons = buttons.filter((button) => {
        const name = (button.getAttribute("aria-label") || button.textContent || button.title || "").trim();
        return !name;
      }).length;
      const imagesWithoutAlt = images.filter((image) => image.getAttribute("alt") === null).length;
      return {
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        rootChildren: root?.children.length || 0,
        textLength: (root?.innerText || "").trim().length,
        h1: (document.querySelector("h1, .page-head__title")?.textContent || "").trim(),
        unlabeledButtons,
        imagesWithoutAlt,
      };
    });
    const screenshot = await page.screenshot({ fullPage: false });
    const label = `${viewport.name}:${route}`;
    if (audit.overflow > 2) failures.push(`${label} 横向溢出 ${audit.overflow}px`);
    if (!audit.rootChildren || audit.textLength < 30) failures.push(`${label} 页面内容为空`);
    if (!audit.h1) failures.push(`${label} 缺少可识别主标题`);
    if (audit.unlabeledButtons) failures.push(`${label} 有 ${audit.unlabeledButtons} 个按钮缺少可访问名称`);
    if (audit.imagesWithoutAlt) failures.push(`${label} 有 ${audit.imagesWithoutAlt} 张图片缺少 alt`);
    if (screenshot.length < 5000) failures.push(`${label} 截图疑似空白`);
    if (errors.length) failures.push(`${label} 页面错误：${errors[0]}`);
    await page.close();
  }
  await context.close();
}

await browser.close();
server.close();

if (failures.length) {
  console.error(`视觉/无障碍回归失败 ${failures.length} 项：`);
  failures.forEach((failure) => console.error(`- ${failure}`));
  process.exit(1);
}
console.log(`视觉/无障碍回归通过：${viewports.length} 档 × ${routes.length} 页面`);
