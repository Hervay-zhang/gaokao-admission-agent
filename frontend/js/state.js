/**
 * state.js —— 全局状态 + 用户档案 + 会话
 *
 * 轻量发布订阅；公开档案持久化到 localStorage，分数/位次仅暂存于 sessionStorage。
 */

import { getCurrentUser, subscribeAuth } from "./auth.js";

const STORAGE_KEY = "gaokao_state_v2";
const SENSITIVE_STORAGE_KEY = "gaokao_sensitive_v1";
const SENSITIVE_PROFILE_KEYS = ["score", "rank"];

const defaultState = {
  // 用户档案（画像）
  profile: {
    nickname: "",
    avatar: "",
    grade: "高三",
    province: "guangdong",
    subjectType: "",      // 物理 / 历史
    score: null,          // 位次换算输入
    rank: null,
    electives: [],        // 再选科目
    preferences: {        // 偏好（后续注入 Prompt）
      cityPref: [],
      majorPref: [],
      budgetLevel: "",
    },
  },
  // 会话
  sessionId: "",
  // 志愿表草稿（报告页「加入志愿表」暂存，志愿表页消费）
  planDraft: [],
  // 当前路由
  route: "/",
  // 数据时效（与后端 settings.current_year 对齐：仅使用 ≤ 该年份的投档数据）
  currentYear: 2026,
  // 我的报告历史（本地缓存，后续可对接后端）
  reports: [],
  // 收藏方案（本地缓存）
  favorites: [],
  // 服务记录（本地缓存）
  services: [],
};

// 订阅者
const listeners = new Set();

let state = load();

function load() {
  const user = getCurrentUser();
  const key = storageKeyFor(user);
  try {
    const raw = localStorage.getItem(key);
    if (raw) return normalizeState(JSON.parse(raw), user);
  } catch (e) {
    console.warn("[state] 读取本地状态失败，使用默认值", e);
  }

  // 修复历史版本：管理员此前可能把昵称写在共享状态中。
  // 仅在管理员登录且共享昵称明显属于管理员时迁移，避免污染学生账号。
  if (user?.role === "admin") {
    const migrated = loadLegacyAdminState(user);
    if (migrated) return migrated;
  }
  return createDefaultState(user);
}

function storageKeyFor(user = getCurrentUser()) {
  return user?.username ? `${STORAGE_KEY}:${user.username}` : STORAGE_KEY;
}

function sensitiveStorageKeyFor(user = getCurrentUser()) {
  return user?.username ? `${SENSITIVE_STORAGE_KEY}:${user.username}` : SENSITIVE_STORAGE_KEY;
}

function createDefaultState(user = null) {
  const next = structuredClone(defaultState);
  if (user?.name) next.profile.nickname = user.name;
  const sensitive = readSensitiveProfile(user);
  for (const field of SENSITIVE_PROFILE_KEYS) {
    next.profile[field] = sensitive[field] ?? null;
  }
  return next;
}

function normalizeState(raw, user = null) {
  const base = createDefaultState(user);
  const parsed = raw && typeof raw === "object" ? raw : {};
  const storedProfile = parsed.profile && typeof parsed.profile === "object" ? parsed.profile : {};
  const safeProfile = { ...storedProfile };
  for (const field of SENSITIVE_PROFILE_KEYS) delete safeProfile[field];
  return {
    ...base,
    ...parsed,
    profile: {
      ...base.profile,
      ...safeProfile,
      preferences: {
        ...base.profile.preferences,
        ...(storedProfile.preferences || {}),
      },
    },
  };
}

function readSensitiveProfile(user = getCurrentUser()) {
  try {
    const raw = sessionStorage.getItem(sensitiveStorageKeyFor(user));
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (e) {
    console.warn("[state] 读取会话敏感字段失败", e);
    return {};
  }
}

function clearSensitiveProfile(user = getCurrentUser()) {
  try {
    sessionStorage.removeItem(sensitiveStorageKeyFor(user));
  } catch (e) {
    console.warn("[state] 清理会话敏感字段失败", e);
  }
}

function loadLegacyAdminState(user) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const legacy = normalizeState(JSON.parse(raw));
    const nickname = String(legacy.profile?.nickname || "").trim();
    if (!nickname.includes("管理") && !nickname.toLowerCase().includes("admin")) return null;
    localStorage.setItem(storageKeyFor(user), JSON.stringify(legacy));
    return legacy;
  } catch (e) {
    console.warn("[state] 管理员历史状态迁移失败", e);
    return null;
  }
}

function persist() {
  try {
    const safeProfile = { ...state.profile };
    const sensitive = {};
    for (const field of SENSITIVE_PROFILE_KEYS) {
      sensitive[field] = safeProfile[field] ?? null;
      delete safeProfile[field];
    }
    localStorage.setItem(storageKeyFor(), JSON.stringify({ ...state, profile: safeProfile }));

    const hasSensitive = SENSITIVE_PROFILE_KEYS.some(
      (field) => sensitive[field] !== null && sensitive[field] !== undefined && sensitive[field] !== ""
    );
    if (hasSensitive) {
      sessionStorage.setItem(sensitiveStorageKeyFor(), JSON.stringify(sensitive));
    } else {
      sessionStorage.removeItem(sensitiveStorageKeyFor());
    }
  } catch (e) {
    console.warn("[state] 持久化失败", e);
  }
}

/** 获取当前状态（只读快照）。 */
export function getState() {
  return state;
}

/** 合并更新状态并通知订阅者。 */
export function setState(patch) {
  state = { ...state, ...patch };
  persist();
  listeners.forEach((fn) => {
    try {
      fn(state);
    } catch (e) {
      console.error("[state] 订阅回调异常", e);
    }
  });
}

/** 更新档案字段。 */
export function updateProfile(patch) {
  setState({ profile: { ...state.profile, ...patch } });
}

/** 订阅状态变更，返回取消函数。 */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 确保有会话 ID（匿名优先）。 */
export function ensureSessionId() {
  if (!state.sessionId) {
    const id = "s_" + Math.random().toString(36).slice(2, 6) + Date.now().toString(36);
    setState({ sessionId: id });
  }
  return state.sessionId;
}

/** 确保有展示昵称：未设置时按会话 ID 稳定派生一个默认昵称并持久化。 */
export function ensureNickname() {
  const nick = (state.profile.nickname || "").trim();
  if (nick) return nick;
  const seed = state.sessionId || "s_" + Date.now().toString(36);
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const suffix = String(100 + (h % 900)); // 100~999
  const year = state.currentYear || 2026;
  const generated = `家长${year}${suffix}`;
  updateProfile({ nickname: generated });
  return generated;
}

/** 重置状态（测试/换用户）。 */
export function resetState() {
  clearSensitiveProfile();
  state = createDefaultState(getCurrentUser());
  persist();
}

// 登录/退出后切换到对应的账号档案，不同账号之间不再共享昵称、分数和收藏。
subscribeAuth((user) => {
  // 同一浏览器标签内退出时，主动清掉上一账号的精确分数/位次暂存。
  if (!user) clearSensitiveProfile();
  state = load();
  listeners.forEach((fn) => {
    try {
      fn(state);
    } catch (e) {
      console.error("[state] 认证切换回调异常", e);
    }
  });
});

/** 加入志愿表草稿（报告页「加入志愿表」→ 志愿表页消费）。 */
export function addPlanDraft(item) {
  const draft = state.planDraft || [];
  const dup = draft.some(
    (d) => d.university_code === item.university_code && (d.group_code ?? null) === (item.group_code ?? null)
  );
  if (dup) return false;
  setState({ planDraft: [...draft, item] });
  return true;
}

/** 读取并清空志愿表草稿。 */
export function takePlanDraft() {
  const draft = state.planDraft || [];
  setState({ planDraft: [] });
  return draft;
}

/** 草稿是否存在某条目。 */
export function inPlanDraft(universityCode, groupCode) {
  return (state.planDraft || []).some(
    (d) => d.university_code === universityCode && (d.group_code ?? null) === (groupCode ?? null)
  );
}
