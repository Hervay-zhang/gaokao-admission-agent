/**
 * components/Icon.js —— 全站统一线描图标（内联 SVG，零依赖）
 *
 * 设计口径（改图标务必沿用，否则会破坏视觉一致性）
 * --------------------------------------------------
 * - 画布 24×24，`fill: none`，`stroke: currentColor`，线宽 1.6，圆头圆角；
 * - 只用线条与圆，不用实心色块 —— 与纸感底 + 墨蓝主色同构；
 * - 颜色由外层 CSS 的 `color` 决定（生产环境别在这里写死色值）。
 *
 * 为什么不用 emoji：emoji 是彩色位图字体，各系统渲染差异极大（Windows / macOS /
 * Android 字形完全不同，部分符号还会被降级成豆腐块或别的形状），且与站点
 * 「墨蓝线描 + 衬线标题」的调性冲突 —— 用户反馈的「小机器人图标」即此类问题。
 *
 * 用法：
 *   import { icon } from "../components/Icon.js";
 *   h("div.feature-card__icon", { html: icon("radar") })
 */

const PATHS = {
  /* 全景评估画像：雷达图（五维画像，含数据多边形） */
  radar:
    '<path d="M12 3.4 20.2 9.3 17.1 19H6.9L3.8 9.3Z"/>' +
    '<path d="M12 12 12 3.4M12 12 20.2 9.3M12 12 17.1 19M12 12 6.9 19M12 12 3.8 9.3" opacity=".28"/>' +
    '<path d="M12 4.6 17.9 9.9 15.1 16H8L6.6 10.1Z" fill="currentColor" fill-opacity=".14"/>',

  /* AI 路径判断：三段节点网络（推演链路） */
  network:
    '<circle cx="12" cy="5.2" r="2.3"/>' +
    '<circle cx="5.1" cy="17.4" r="2.3"/>' +
    '<circle cx="18.9" cy="17.4" r="2.3"/>' +
    '<path d="M10.6 7.3 6.6 15.3M13.4 7.3l4 8M7.4 17.4h9.2"/>',

  /* 院校专业库：学院建筑（山花 + 立柱） */
  school:
    '<path d="M12 3.2 20.6 8.6H3.4Z"/>' +
    '<path d="M5.9 8.6v10.2M9.6 8.6v10.2M14.4 8.6v10.2M18.1 8.6v10.2"/>' +
    '<path d="M3.2 18.8h17.6"/>',

  /* 真实案例集：摊开的书 */
  book:
    '<path d="M12 6.5C10.2 5.3 7.8 4.7 4.4 4.7v13c3.4 0 5.8.6 7.6 1.8 1.8-1.2 4.2-1.8 7.6-1.8v-13c-3.4 0-5.8.6-7.6 1.8Z"/>' +
    '<path d="M12 6.5v13"/>',

  /* 升学社区：对话气泡 */
  chat:
    '<path d="M5.6 4.4h12.8a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3h-4.2l-4.6 3.6v-3.6H5.6a3 3 0 0 1-3-3v-5a3 3 0 0 1 3-3Z"/>',

  /* 专家咨询：人像 */
  mentor:
    '<circle cx="12" cy="8.4" r="3.7"/>' +
    '<path d="M4.9 20.4v-1.1a7.1 7.1 0 0 1 14.2 0v1.1"/>',

  /* 数据真实可信：盾牌 + 勾 */
  shield:
    '<path d="M12 3.3 19.4 6v6.2c0 4.2-3 7.4-7.4 8.8-4.4-1.4-7.4-4.6-7.4-8.8V6Z"/>' +
    '<path d="M8.9 12.1l2.2 2.2 4.1-4.4"/>',

  /* 算法可解释：分叉路径（可追溯） */
  branch:
    '<circle cx="5.4" cy="19" r="2.1"/>' +
    '<circle cx="5.4" cy="5" r="2.1"/>' +
    '<circle cx="18.6" cy="9.4" r="2.1"/>' +
    '<path d="M5.4 7.1v9.8M7.5 19h3a4 4 0 0 0 4-4v-1.6a4 4 0 0 1 4-4h.1"/>',

  /* 核心功能免费：心形 */
  heart:
    '<path d="M12 20.4 4.9 13.2a4.4 4.4 0 0 1 6.2-6.2l.9.9.9-.9a4.4 4.4 0 0 1 6.2 6.2Z"/>',

  /* 反馈：赞 / 踩（手掌 + 拇指，两个方向镜像构图） */
  thumbUp:
    '<rect x="3.6" y="10.2" width="3.4" height="9.4" rx="1"/>' +
    '<path d="M7 19.6V10.2l3.7-6.4a1.7 1.7 0 0 1 3 1.2v4.2h3.9a2 2 0 0 1 2 2.4l-1.1 5.6a2 2 0 0 1-2 1.6Z"/>',
  thumbDown:
    '<rect x="3.6" y="4.4" width="3.4" height="9.4" rx="1"/>' +
    '<path d="M7 4.4v9.4l3.7 6.4a1.7 1.7 0 0 0 3-1.2v-4.2h3.9a2 2 0 0 0 2-2.4l-1.1-5.6a2 2 0 0 0-2-1.6Z"/>',

  /* 移动端菜单按钮 */
  menu:
    '<path d="M4 7h16M4 12h16M4 17h16"/>',

  /* 社区：浏览 / 评论 / 点赞 / 收藏 */
  eye:
    '<path d="M2.7 12S6.3 5.6 12 5.6 21.3 12 21.3 12 17.7 18.4 12 18.4 2.7 12 2.7 12Z"/>' +
    '<circle cx="12" cy="12" r="2.9"/>',

  /* 编辑 / 删除 */
  pencil:
    '<path d="M4.5 19.5l1-4.4L16 4.7a2 2 0 0 1 2.8 0l1.5 1.5a2 2 0 0 1 0 2.8L9.9 18.5Z"/>' +
    '<path d="M14.7 6l3.3 3.3"/>',
  trash:
    '<path d="M4.7 7h14.6M9.3 7V4.9a1 1 0 0 1 1-1h3.4a1 1 0 0 1 1 1V7"/>' +
    '<path d="M6.5 7l.9 12.1a1 1 0 0 0 1 .9h7.2a1 1 0 0 0 1-.9L17.5 7"/>' +
    '<path d="M10.3 10.9v6.2M13.7 10.9v6.2"/>',

  /* 用户中心侧栏：总览 / 报告 / 志愿表 / 收藏 / 服务记录 / 设置 */
  grid:
    '<rect x="3.6" y="3.6" width="7.2" height="7.2" rx="1.6"/>' +
    '<rect x="13.2" y="3.6" width="7.2" height="7.2" rx="1.6"/>' +
    '<rect x="3.6" y="13.2" width="7.2" height="7.2" rx="1.6"/>' +
    '<rect x="13.2" y="13.2" width="7.2" height="7.2" rx="1.6"/>',
  report:
    '<path d="M13.6 3.6H7.2a2 2 0 0 0-2 2v12.8a2 2 0 0 0 2 2h9.6a2 2 0 0 0 2-2V8.8Z"/>' +
    '<path d="M13.6 3.6v5.2h5.2M8.6 13h6.8M8.6 16.4h4.4"/>',
  list:
    '<path d="M8.4 5.6h11M8.4 12h11M8.4 18.4h11"/>' +
    '<path d="M4.4 5.6h.01M4.4 12h.01M4.4 18.4h.01"/>',
  star:
    '<path d="m12 3.8 2.6 5.4 5.8.8-4.2 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9L3.6 10l5.8-.8Z"/>',
  bookmark:
    '<path d="M6.4 4.6h11.2a1 1 0 0 1 1 1v14.2L12 16.2l-6.6 3.6V5.6a1 1 0 0 1 1-1Z"/>',
  clock:
    '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.4V12l3.2 2"/>',
  gear:
    '<circle cx="12" cy="12" r="3.1"/>' +
    '<path d="M19.2 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1h-.2a1.9 1.9 0 1 1 0-3.8h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5v-.2a1.9 1.9 0 1 1 3.8 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.2a1.9 1.9 0 1 1 0 3.8h-.1a1.6 1.6 0 0 0-1.5 1Z"/>',
};

/**
 * 生成内联 SVG 字符串（配合 h(..., { html }) 使用）。
 *
 * @param {keyof typeof PATHS|string} name 图标名
 * @param {{size?: number, stroke?: number, cls?: string}} [opts]
 * @returns {string} SVG 标记；名称不存在时返回空串（调用方不会渲染出破损图标）
 */
export function icon(name, opts = {}) {
  const body = PATHS[name];
  if (!body) {
    if (typeof console !== "undefined") console.warn("[icon] 未知图标：", name);
    return "";
  }
  const { size = 24, stroke = 1.6, cls = "" } = opts;
  const klass = cls ? ` class="icon ${cls}"` : ' class="icon"';
  return (
    `<svg${klass} viewBox="0 0 24 24" width="${size}" height="${size}" ` +
    `fill="none" stroke="currentColor" stroke-width="${stroke}" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">` +
    `${body}</svg>`
  );
}

export const ICON_NAMES = Object.keys(PATHS);
