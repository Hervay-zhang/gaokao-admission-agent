/**
 * ContourBg.js —— 等高线纹理背景（内联 SVG，极低对比度）
 * 呼应见径「测量地形图美学」；纯装饰，aria-hidden。
 */

const SVG = `
<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice">
  <defs>
    <linearGradient id="cg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1e3a5f" stop-opacity="0.05"/>
      <stop offset="100%" stop-color="#c0392b" stop-opacity="0.03"/>
    </linearGradient>
  </defs>
  <g fill="none" stroke="url(#cg)" stroke-width="1">
    <path d="M-100,220 C180,140 420,300 720,240 S1200,120 1540,220"/>
    <path d="M-100,260 C180,180 420,340 720,280 S1200,160 1540,260"/>
    <path d="M-100,300 C180,220 420,380 720,320 S1200,200 1540,300"/>
    <path d="M-100,500 C160,430 400,600 700,540 S1180,420 1540,510"/>
    <path d="M-100,540 C160,470 400,640 700,580 S1180,460 1540,550"/>
    <path d="M-100,580 C160,510 400,680 700,620 S1180,500 1540,590"/>
    <path d="M-100,760 C200,690 480,840 760,780 S1220,660 1540,750"/>
    <path d="M-100,800 C200,730 480,880 760,820 S1220,700 1540,790"/>
    <ellipse cx="1120" cy="360" rx="180" ry="90" transform="rotate(-18 1120 360)"/>
    <ellipse cx="1120" cy="360" rx="130" ry="62" transform="rotate(-18 1120 360)"/>
    <ellipse cx="1120" cy="360" rx="80" ry="36" transform="rotate(-18 1120 360)"/>
  </g>
</svg>`;

export function renderContourBg() {
  const root = document.getElementById("contour-bg");
  if (!root) return;
  root.innerHTML = SVG;
}
