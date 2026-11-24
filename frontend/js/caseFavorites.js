/**
 * caseFavorites.js —— 案例收藏（localStorage）
 *
 * 本地离线收藏：案例详情「收藏」/ 案例列表展示收藏状态 / 我的页「收藏案例」。
 * 只存脱敏案例元信息（slug/标题/科类/分数段/位次段/路径类型），不含任何个人可识别信息；
 * 数据仅保存在本机浏览器，不发起网络请求。
 */

const STORAGE_KEY = "gaokao_case_favorites";

function read() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    console.warn("[caseFavorites] 读取失败，使用空列表", e);
    return [];
  }
}

function write(list) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch (e) {
    console.warn("[caseFavorites] 写入失败", e);
  }
}

/** 全部已收藏案例（按收藏时间倒序）。 */
export function getCaseFavorites() {
  return read().slice().sort((a, b) => String(b.saved_at || "").localeCompare(String(a.saved_at || "")));
}

/** 是否已收藏某案例。 */
export function isCaseFavorite(slug) {
  return read().some((f) => f.slug === slug);
}

/** 收藏（幂等；重复收藏返回 false）。 */
export function addCaseFavorite(caseMeta) {
  const list = read();
  if (!caseMeta || !caseMeta.slug || list.some((f) => f.slug === caseMeta.slug)) return false;
  list.push({
    slug: caseMeta.slug,
    title: caseMeta.title || caseMeta.slug,
    subject_type: caseMeta.subject_type || "",
    score_band: caseMeta.score_band || "",
    rank_band: caseMeta.rank_band || "",
    path_type: caseMeta.path_type || "",
    saved_at: new Date().toISOString(),
  });
  write(list);
  return true;
}

/** 取消收藏（幂等；未收藏返回 false）。 */
export function removeCaseFavorite(slug) {
  const list = read();
  const next = list.filter((f) => f.slug !== slug);
  if (next.length === list.length) return false;
  write(next);
  return true;
}

/** 切换收藏；返回 { favorited, list }。 */
export function toggleCaseFavorite(caseMeta) {
  if (isCaseFavorite(caseMeta && caseMeta.slug)) {
    removeCaseFavorite(caseMeta.slug);
    return { favorited: false, list: getCaseFavorites() };
  }
  addCaseFavorite(caseMeta);
  return { favorited: true, list: getCaseFavorites() };
}