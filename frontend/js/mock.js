/**
 * mock.js —— 离线 mock 数据（后端未通时联调）
 * 全部为**占位数据**，不代表真实投档结果；真实数据由后端接口提供。
 */

export const mockSchools = [
  { code: "10558", name: "中山大学", level: "985", province: "广东", city: "广州", tier: "chong", minRank: 12800 },
  { code: "10561", name: "华南理工大学", level: "985", province: "广东", city: "广州", tier: "chong", minRank: 18600 },
  { code: "10559", name: "暨南大学", level: "211", province: "广东", city: "广州", tier: "wen", minRank: 26300 },
  { code: "10574", name: "华南师范大学", level: "211", province: "广东", city: "广州", tier: "wen", minRank: 32100 },
  { code: "10560", name: "汕头大学", level: "省重点", province: "广东", city: "汕头", tier: "bao", minRank: 45800 },
  { code: "11078", name: "广州大学", level: "省重点", province: "广东", city: "广州", tier: "bao", minRank: 52300 },
];

export const mockQuickQuestions = [
  "广东物理类 600 分能报哪些学校？",
  "冲稳保三档是怎么划分的？",
  "什么是平行志愿？填报要注意什么？",
  "新高考选科对专业选择有什么影响？",
];

export const mockCases = [
  { id: "c1", title: "2025 广东物理类 598 分：从纠结到笃定", tag: "理工方向", meta: "最终录取 · 华南理工大学" },
  { id: "c2", title: "2025 广东历史类 562 分：稳妥上岸 211", tag: "文史方向", meta: "最终录取 · 暨南大学" },
  { id: "c3", title: "2024 广东物理类 545 分：精准保底不滑档", tag: "保底策略", meta: "最终录取 · 汕头大学" },
];

export const mockReport = {
  chong: [
    { name: "中山大学", group: "204", minRank: 12800, minScore: 632 },
    { name: "华南理工大学", group: "201", minRank: 18600, minScore: 615 },
  ],
  wen: [
    { name: "暨南大学", group: "203", minRank: 26300, minScore: 594 },
    { name: "华南师范大学", group: "207", minRank: 32100, minScore: 581 },
  ],
  bao: [
    { name: "汕头大学", group: "211", minRank: 45800, minScore: 558 },
    { name: "广州大学", group: "205", minRank: 52300, minScore: 547 },
  ],
};
