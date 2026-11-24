/**
 * exportImage.js —— 志愿表导出长图（Canvas，T28）
 *
 * 职责
 * ----
 * 1. 把志愿表（+ 脱敏考生画像）绘制为一张长图并触发下载；
 * 2. **纯前端本地生成**（与「服务端只读分享」是两套逻辑，勿混用）；
 * 3. 图上**只出现分数段 / 位次段**，不绘制明文分数/位次（红线与分享一致）；
 * 4. 处理高 DPI（devicePixelRatio）避免模糊；超长列表自动增高，宽度固定。
 *
 * 用法
 * ----
 *   exportPlanImage({ plan, candidate, filename })
 *   // plan: {name, year, province, capacity, items:[{seq, university_name, group_code, tier, system_tier, user_adjusted}]}
 */

const TIER_LABEL = { chong: "冲", wen: "稳", bao: "保" };

const COLORS = {
  paper: "#faf7f2",
  card: "#ffffff",
  ink: "#1e3a5f",
  text: "#2b2b2b",
  sub: "#6b6b6b",
  light: "#999999",
  border: "#e4ddd2",
  vermilion: "#c0392b",
  wen: "#b98a2f",
  bao: "#2e7d5b",
};

const TIER_COLOR = { chong: COLORS.vermilion, wen: COLORS.wen, bao: COLORS.bao };

const W = 900;          // 逻辑宽度
const PAD = 48;
const DPR = Math.min(typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1, 3);
const ROW_H = 62;
const HEAD_H = 300;     // 页头区
const FOOT_H = 120;

/**
 * 生成并下载志愿表长图。
 * @returns {Promise<{width:number,height:number,rows:number}>} 生成尺寸信息（便于测试断言）
 */
export async function exportPlanImage({ plan, candidate = {}, filename } = {}) {
  if (!plan || !Array.isArray(plan.items)) {
    throw new Error("缺少志愿表数据");
  }
  const items = plan.items;
  const contentH = HEAD_H + 56 + items.length * ROW_H + FOOT_H;
  const canvas = document.createElement("canvas");
  canvas.width = W * DPR;
  canvas.height = contentH * DPR;
  const ctx = canvas.getContext("2d");
  ctx.scale(DPR, DPR);

  drawBackground(ctx, W, contentH);
  drawHeader(ctx, plan, candidate);
  drawTableHead(ctx);
  items.forEach((it, i) => drawRow(ctx, it, i));
  drawFooter(ctx, plan, candidate);

  const name = filename || `志愿表_${plan.name || "方案"}_${stamp()}.png`;
  await downloadCanvas(canvas, name);
  return { width: W, height: contentH, rows: items.length };
}

/** 仅供测试/预览：返回 dataURL 而不下载。 */
export function renderPlanDataUrl(opts) {
  const canvas = document.createElement("canvas");
  const plan = opts.plan;
  const items = plan.items || [];
  const contentH = HEAD_H + 56 + items.length * ROW_H + FOOT_H;
  canvas.width = W * DPR;
  canvas.height = contentH * DPR;
  const ctx = canvas.getContext("2d");
  ctx.scale(DPR, DPR);
  drawBackground(ctx, W, contentH);
  drawHeader(ctx, plan, opts.candidate || {});
  drawTableHead(ctx);
  items.forEach((it, i) => drawRow(ctx, it, i));
  drawFooter(ctx, plan, opts.candidate || {});
  return canvas.toDataURL("image/png");
}

// ============================================================
// 绘制
// ============================================================

function drawBackground(ctx, w, h) {
  ctx.fillStyle = COLORS.paper;
  ctx.fillRect(0, 0, w, h);
  // 顶部墨蓝色带（品牌）
  ctx.fillStyle = COLORS.ink;
  ctx.fillRect(0, 0, w, 10);
}

function drawHeader(ctx, plan, candidate) {
  let y = 56;
  ctx.fillStyle = COLORS.ink;
  ctx.font = `700 34px ${serif()}`;
  ctx.textBaseline = "alphabetic";
  ctx.fillText(plan.name || "我的志愿表", PAD, y);

  y += 40;
  const band = [
    plan.province ? `${plan.province}` : "",
    candidate.subject_type ? `${candidate.subject_type}类` : "",
    candidate.score_bucket ? `${candidate.score_bucket} 分` : "",
    candidate.rank_bucket ? `位次段 ${candidate.rank_bucket}` : "",
    `${plan.year || ""} 年`,
  ].filter(Boolean).join(" · ");
  ctx.fillStyle = COLORS.sub;
  ctx.font = `400 18px ${sans()}`;
  ctx.fillText(band, PAD, y);

  y += 34;
  ctx.fillStyle = COLORS.light;
  ctx.font = `400 16px ${sans()}`;
  ctx.fillText(`共 ${plan.items.length} 个院校专业组（容量 ${plan.capacity || "—"}）`
    + `　生成于 ${new Date().toLocaleString("zh-CN")}`, PAD, y);

  y += 34;
  ctx.strokeStyle = COLORS.border;
  ctx.beginPath();
  ctx.moveTo(PAD, y);
  ctx.lineTo(W - PAD, y);
  ctx.stroke();
}

function drawTableHead(ctx) {
  const y = HEAD_H - 8;
  ctx.fillStyle = COLORS.sub;
  ctx.font = `600 16px ${sans()}`;
  ctx.fillText("序号", PAD, y);
  ctx.fillText("院校（专业组）", PAD + 70, y);
  ctx.fillText("档位", W - PAD - 190, y);
  ctx.fillText("系统档位", W - PAD - 110, y);
}

function drawRow(ctx, it, i) {
  const top = HEAD_H + 56 + i * ROW_H;
  // 斑马底
  if (i % 2 === 1) {
    ctx.fillStyle = COLORS.card;
    ctx.fillRect(PAD - 8, top, W - 2 * PAD + 16, ROW_H - 8);
  }
  const midY = top + 26;

  ctx.fillStyle = COLORS.ink;
  ctx.font = `700 20px ${serif()}`;
  ctx.fillText(String(it.seq ?? i + 1), PAD, midY + 6);

  ctx.fillStyle = COLORS.text;
  ctx.font = `600 19px ${sans()}`;
  const name = `${it.university_name}（专业组 ${it.group_code ?? "—"}）`;
  ctx.fillText(clip(ctx, name, W - PAD - 300), PAD + 70, midY + 2);

  ctx.fillStyle = COLORS.light;
  ctx.font = `400 14px ${sans()}`;
  ctx.fillText(it.user_adjusted ? "已手动调整档位" : "系统默认", PAD + 70, midY + 24);

  // 档位徽章
  const tier = it.tier || "";
  const color = TIER_COLOR[tier] || COLORS.light;
  const bx = W - PAD - 190;
  roundRect(ctx, bx, midY - 16, 34, 34, 8);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.font = `700 18px ${serif()}`;
  ctx.fillText(TIER_LABEL[tier] || "—", bx + 11, midY + 7);

  ctx.fillStyle = COLORS.sub;
  ctx.font = `400 16px ${sans()}`;
  ctx.fillText(TIER_LABEL[it.system_tier] || "—", W - PAD - 110, midY + 6);

  // 分隔线
  ctx.strokeStyle = COLORS.border;
  ctx.beginPath();
  ctx.moveTo(PAD, top + ROW_H - 8);
  ctx.lineTo(W - PAD, top + ROW_H - 8);
  ctx.stroke();
}

function drawFooter(ctx, plan, candidate) {
  const y = HEAD_H + 56 + plan.items.length * ROW_H + 46;
  ctx.fillStyle = COLORS.light;
  ctx.font = `400 14px ${sans()}`;
  ctx.fillText("本方案基于近三年真实投档数据加权测算，仅供参考，不作为最终填报承诺。", PAD, y);
  ctx.fillText("导出图仅含分数段/位次段，不含明文成绩与个人身份信息。", PAD, y + 24);
  if (candidate.subject_type) {
    ctx.fillText("由 升学志愿助手 生成", PAD, y + 52);
  }
}

// ============================================================
// 工具
// ============================================================

function serif() {
  return '"Noto Serif SC", "Source Han Serif SC", "Songti SC", serif';
}

function sans() {
  return '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** 超宽文本截断加省略号。 */
function clip(ctx, text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let s = text;
  while (s.length > 1 && ctx.measureText(s + "…").width > maxWidth) s = s.slice(0, -1);
  return s + "…";
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
}

function downloadCanvas(canvas, filename) {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      resolve(true);
    }, "image/png");
  });
}
