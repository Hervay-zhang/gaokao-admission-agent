/**
 * config.js —— 静态配置表
 * 省份 / 高考模式 / 科目 / 偏好选项（全国 31 省骨架，当前只需广东可用）
 *
 * 注意：此处仅为**前端展示与表单选项**；真正的分省口径与选科校验由后端实现（唯一口径）。
 */

// 高考模式
export const MODES = {
  traditional: "传统文理",
  "3+3": "3+3 新高考",
  "3+1+2": "3+1+2 新高考",
};

// 再选/选考科目池
export const SUBJECT_POOL_6 = ["物理", "化学", "生物", "政治", "历史", "地理"];

// 省份 → 高考模式（T23 补全 31 省；模式由省份映射，**不允许用户手选**）
//   primary：首选科目（3+1+2）；electivePool：再选/选考科目池
//   dataAvailable：库内是否已有一分一段/投档数据（当前仅广东；false → 测评可浏览，位次标注「数据待补齐」）
export const PROVINCES = {
  // ---- 3+1+2 新高考（23 省）----
  guangdong: { name: "广东", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"], dataAvailable: true },
  fujian: { name: "福建", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  hebei: { name: "河北", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  liaoning: { name: "辽宁", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  jiangsu: { name: "江苏", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  hunan: { name: "湖南", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  hubei: { name: "湖北", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  chongqing: { name: "重庆", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  jiangxi: { name: "江西", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  anhui: { name: "安徽", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  guizhou: { name: "贵州", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  guangxi: { name: "广西", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  gansu: { name: "甘肃", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  heilongjiang: { name: "黑龙江", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  jilin: { name: "吉林", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  henan: { name: "河南", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  shanxi: { name: "山西", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  neimenggu: { name: "内蒙古", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  sichuan: { name: "四川", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  yunnan: { name: "云南", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  ningxia: { name: "宁夏", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  qinghai: { name: "青海", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  shaanxi: { name: "陕西", mode: "3+1+2", primary: ["物理", "历史"], electivePool: ["化学", "生物", "政治", "地理"] },
  // ---- 3+3 新高考（6 省；浙江含「技术」共 7 门选 3）----
  shanghai: { name: "上海", mode: "3+3", electivePool: SUBJECT_POOL_6 },
  beijing: { name: "北京", mode: "3+3", electivePool: SUBJECT_POOL_6 },
  tianjin: { name: "天津", mode: "3+3", electivePool: SUBJECT_POOL_6 },
  shandong: { name: "山东", mode: "3+3", electivePool: SUBJECT_POOL_6 },
  zhejiang: { name: "浙江", mode: "3+3", electivePool: [...SUBJECT_POOL_6, "技术"] },
  hainan: { name: "海南", mode: "3+3", electivePool: SUBJECT_POOL_6 },
  // ---- 传统文理（2 省区）----
  xinjiang: { name: "新疆", mode: "traditional", primary: ["文科", "理科"] },
  xizang: { name: "西藏", mode: "traditional", primary: ["文科", "理科"] },
};

// 兼容旧引用：3+1+2 的首选/再选科目（取广东口径）
export const PRIMARY_SUBJECTS = ["物理", "历史"];
export const ELECTIVE_POOL = ["化学", "生物", "政治", "地理"];

// 院校层次标签
export const UNI_TAGS = ["985", "211", "双一流", "省重点", "普通本科"];

// 用户偏好（结构化后注入 Prompt）
export const PREFERENCE_OPTIONS = {
  cityPref: ["一线城市", "省会城市", "本省优先", "不限"],
  majorPref: ["计算机", "医学", "师范", "财经", "法学", "理工", "文史", "不限"],
  budgetLevel: ["预算宽裕", "预算适中", "预算有限"],
};

// 数据时效展示
export const DATA_YEARS = [2024, 2025, 2026];

export function getProvince(key) {
  return PROVINCES[key] || PROVINCES.guangdong;
}
