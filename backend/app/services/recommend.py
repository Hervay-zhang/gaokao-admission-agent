"""「冲稳保」梯度推荐引擎（T08）。

输入：考生档案（分数 / 科类 / 再选科目 / 省份）
输出：冲 / 稳 / 保 三档院校专业组（各 ≤ ``tier_limit``）+ 数据依据 + 风险解释 + 家长版摘要。

算法总览
--------
1. **候选过滤**（严格顺序，任一不通过即排除）：
   ① 省份 —— 该专业组在本省有投档记录；
   ② 首选科目（``first``）—— **硬过滤**，考生 ``subject_type`` 必须与之一致（数据 100% 可靠），
      绝不出现「物理考生被推荐到仅历史招生的专业组」；
   ③ 再选科目（``second``）—— **分层**：``second`` 非空则按 ``all/any/none`` 严格校验；
      ``second`` 空且 ``status=待补`` 则**不排除**，但结果带 ``require_note`` 标注（见 data_caliber §5.2）；
   ④ 必须有投档数据（``min_rank`` 非空）—— 否则不进正常档位。
2. **参考位次 L**：近三年投档最低位次的**加权平均**（权重取 ``settings.year_weight_list``，
   只对有数据的年份加权并**重新归一化**），再做**计划变化因子**修正。
3. **分档**（以考生位次 R 为基准）：
   - 冲 ``L ≤ chong_ratio × R``
   - 稳 ``chong_ratio×R < L ≤ wen_ratio×R``
   - 保 ``wen_ratio×R < L ≤ bao_ratio×R``
4. **档内排序**：院校层次（985 > 211 > 双一流 > 普通）→ 地域（省份）→ 计划变化（扩招优先）。
5. **解释**：``explanation`` / ``risk_explanation`` / ``parent_summary`` 全部由**真实数据拼接**的规则模板生成，
   **不调用 LLM**；风险解释**禁止出现概率数字**（只用「偏高 / 适中 / 偏低」）。

红线
----
- 推荐必须有库内依据；无数据 / 缺年**显式标注**；**不得编造录取概率**；
- 禁止跨科类、跨高考模式推荐；
- 年份只允许 ``year ≤ CURRENT_YEAR``（由 ``settings.ref_year_list`` 守卫）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import DataMissingError, ValidationError
from app.core.logging import get_logger
from app.schemas.recommend import (  # noqa: F401  (schema 定义在独立模块，便于前端契约复用)
    RecommendItem,
    RecommendResult,
    TierGroup,
)

logger = get_logger(__name__)

# 层次排序权重（越大越优先）
LEVEL_WEIGHT = {"985": 3, "211": 2, "双一流": 1, "": 0}

# 广东（唯一演示省份）主要生源地热度（用于档内排序的「地域」维度）
# 说明：这是**排序偏好**，不是录取难度，仅为让同层次院校稳定有序；值越大越优先。
REGION_HOT = {"广东": 3, "北京": 2, "上海": 2, "江苏": 2, "浙江": 2}


@dataclass
class Candidate:
    """候选专业组（同一 院校×专业组×科类，聚合多年数据）。"""

    university_code: str
    university_name: str
    group_code: int
    subject_type: str
    # 年份 → (min_rank, min_score, plan_count)
    yearly: dict[int, tuple[int | None, int | None, int | None]] = field(default_factory=dict)
    # 院校元数据（用于排序/展示）
    level_tags: str = ""
    school_province: str | None = None
    school_type: str | None = None
    meta_status: str = "待补"
    # 选科要求
    first: list[str] = field(default_factory=list)
    second: list[str] = field(default_factory=list)
    pattern: str = "none"
    require_status: str = "待补"


@runtime_checkable
class RecommendRepository(Protocol):
    """推荐所需的最小数据访问接口（可注入：生产 MySQL，测试内存）。"""

    def iter_admission_rows(self, province: str, subject_type: str, years: list[int]) -> list[dict]:
        """产出投档行（含院校元数据与选科要求），字段见 :meth:`_rows_to_candidates`。"""
        ...

    def university_meta(self, codes: list[str]) -> dict[str, dict]:
        """院校元数据：``{code: {tags, province, school_type, meta_status}}``。"""
        ...


# ============================================================
# 内存实现（测试 / 离线）
# ============================================================


class InMemoryRecommendRepository:
    """内存数据源：直接吃结构化行，便于构造边界场景做单测。"""

    def __init__(self, rows: list[dict], meta: dict[str, dict] | None = None) -> None:
        self._rows = rows
        self._meta = meta or {}

    def iter_admission_rows(self, province: str, subject_type: str, years: list[int]) -> list[dict]:
        out = []
        for r in self._rows:
            if r.get("province") != province:
                continue
            if r.get("subject_type") != subject_type:
                continue
            if years and r.get("year") not in years:
                continue
            out.append(r)
        return out

    def university_meta(self, codes: list[str]) -> dict[str, dict]:
        return {c: self._meta.get(c, {}) for c in codes}


# ============================================================
# 生产实现（MySQL）
# ============================================================


class SqlRecommendRepository:
    """MySQL 数据源。"""

    def __init__(self) -> None:
        from app.db.session import SessionLocal

        self._SessionLocal = SessionLocal

    def iter_admission_rows(self, province: str, subject_type: str, years: list[int]) -> list[dict]:
        import json

        from sqlalchemy import select

        from app.db.models import AdmissionLine

        with self._SessionLocal() as db:
            stmt = select(
                AdmissionLine.year,
                AdmissionLine.province,
                AdmissionLine.subject_type,
                AdmissionLine.university_code,
                AdmissionLine.university_name,
                AdmissionLine.group_code,
                AdmissionLine.plan_count,
                AdmissionLine.admitted_count,
                AdmissionLine.min_score,
                AdmissionLine.min_rank,
                AdmissionLine.subject_requirement,
            ).where(
                AdmissionLine.province == province,
                AdmissionLine.subject_type == subject_type,
                AdmissionLine.year.in_(years),
            )
            rows = []
            for rec in db.execute(stmt).all():
                (
                    year, prov, stype, code, name, gcode,
                    plan, admitted, mscore, mrank, req_json,
                ) = rec
                try:
                    req = json.loads(req_json) if req_json else {}
                except (ValueError, TypeError):
                    req = {}
                rows.append({
                    "year": int(year), "province": prov, "subject_type": stype,
                    "university_code": code, "university_name": name, "group_code": int(gcode),
                    "plan_count": plan, "admitted_count": admitted,
                    "min_score": mscore, "min_rank": mrank,
                    "first": req.get("first", []), "second": req.get("second", []),
                    "pattern": req.get("pattern", "none"), "require_status": req.get("status", "待补"),
                })
            return rows

    def university_meta(self, codes: list[str]) -> dict[str, dict]:
        from sqlalchemy import select

        from app.db.models import University

        if not codes:
            return {}
        with self._SessionLocal() as db:
            stmt = select(
                University.code, University.tags, University.province,
                University.school_type, University.meta_status,
            ).where(University.code.in_(codes))
            return {
                code: {"tags": tags or "", "province": prov, "school_type": stype, "meta_status": ms}
                for code, tags, prov, stype, ms in db.execute(stmt).all()
            }


# ============================================================
# 引擎
# ============================================================


class RecommendEngine:
    """冲稳保推荐引擎。"""

    def __init__(self, repo: RecommendRepository) -> None:
        self._repo = repo

    # ---------- 主入口 ----------

    def recommend(
        self,
        *,
        score: int,
        subject_type: str,
        electives: list[str] | None = None,
        province: str | None = None,
        rank: int | None = None,
        current_year: int | None = None,
        ref_years: list[int] | None = None,
        tier_limit: int | None = None,
    ) -> RecommendResult:
        """生成冲稳保推荐。

        ``rank`` 可由上位（T07 位次引擎）传入；若未传则要求 caller 先算好（本引擎不重复做分数→位次，
        避免口径双实现）。这里仅做**入参校验**，位次缺失直接报错而非猜测。
        """
        province = province or "广东"
        current_year = current_year or settings.current_year
        ref_years = ref_years or settings.ref_year_list
        tier_limit = tier_limit or settings.tier_limit
        electives = electives or []

        self._validate(score, subject_type, rank, ref_years, current_year)

        assert rank is not None  # _validate 已保证
        # 只允许使用 <= current_year 的年份（时效红线）
        usable_years = [y for y in ref_years if y <= current_year]
        if not usable_years:
            raise DataMissingError(
                f"参考年份 {ref_years} 全部晚于当前报考年份 {current_year}，违反时效红线",
                detail={"code": "FUTURE_YEAR", "ref_years": ref_years, "current_year": current_year},
            )

        # 期望参考年份 → 权重（只取本次请求且合法的年份；缺失年份的判断基准）
        all_w = dict(zip(settings.ref_year_list, settings.year_weight_list, strict=False))
        weights = {y: all_w.get(y, 0.0) for y in usable_years}

        rows = self._repo.iter_admission_rows(province, subject_type, usable_years)
        if not rows:
            raise DataMissingError(
                f"库内无 {province} {subject_type} 在 {usable_years} 的投档数据",
                detail={"code": "NO_ADMISSION_DATA", "province": province, "subject_type": subject_type},
            )

        # ---- 候选过滤 ----
        candidates, excluded = self._filter_candidates(rows, subject_type, electives)

        # ---- 合并院校元数据（层次 / 地域，用于档内排序与展示）----
        meta = self._repo.university_meta([c.university_code for c in candidates])
        for cand in candidates:
            m = meta.get(cand.university_code) or {}
            cand.level_tags = m.get("tags", "") or ""
            cand.school_province = m.get("province")
            cand.school_type = m.get("school_type")
            cand.meta_status = m.get("meta_status", "待补")

        # ---- 计算参考位次 L 并分档 ----
        tiers: dict[str, list[RecommendItem]] = {"chong": [], "wen": [], "bao": []}
        unmatched = 0
        for cand in candidates:
            item = self._build_item(cand, weights, rank)
            if item.ref_rank is None:
                continue
            tier = self._classify(item.ref_rank, rank)
            if tier is None:
                unmatched += 1
                continue
            tiers[tier].append(item)

        # ---- 档内排序 + 去重 + 截断 ----
        groups: list[TierGroup] = []
        for key, label in (("chong", "冲"), ("wen", "稳"), ("bao", "保")):
            ordered = self._rank_tier(tiers[key], rank, tier_limit)
            for it in ordered:
                it.tier = key
                it.tier_label = label
            groups.append(TierGroup(
                tier=key, label=label, interval=self._interval_text(key, rank),
                items=ordered, count=len(ordered),
            ))

        caliber = self._caliber_text(usable_years, weights)

        result = RecommendResult(
            province=province,
            current_year=current_year,
            subject_type=subject_type,
            electives=electives,
            score=score,
            rank=rank,
            ref_years_used=usable_years,
            tiers=groups,
            excluded=excluded,
            unmatched_count=unmatched,
            caliber_text=caliber,
            explanation=self._explanation(
                score=score, rank=rank, subject_type=subject_type,
                groups=groups, usable_years=usable_years, province=province,
            ),
            risk_explanation=self._risk_summary(groups, rank),
            parent_summary=self._parent_summary(groups, score, rank, subject_type, province, caliber),
        )
        return result

    # ---------- 校验 ----------

    @staticmethod
    def _validate(score: int, subject_type: str, rank: int | None, ref_years: list[int], current_year: int) -> None:
        if subject_type not in ("物理", "历史"):
            raise ValidationError(f"科类必须是「物理」或「历史」，收到 {subject_type!r}")
        if not isinstance(score, int) or score < 0 or score > 750:
            raise ValidationError(f"分数必须是 0–750 的整数，收到 {score!r}")
        if rank is None:
            raise ValidationError("缺少考生位次 rank（应由位次引擎 T07 先换算后传入，禁止猜测）")
        if not isinstance(rank, int) or rank < 1:
            raise ValidationError(f"位次必须为正整数，收到 {rank!r}")

    # ---------- 候选过滤 ----------

    def _filter_candidates(
        self, rows: list[dict], subject_type: str, electives: list[str]
    ) -> tuple[list[Candidate], dict]:
        """按 §5.3 顺序过滤，并统计各阶段排除原因。"""
        # 聚合为「院校×专业组」
        agg: dict[tuple[str, int], Candidate] = {}
        for r in rows:
            key = (r["university_code"], r["group_code"])
            cand = agg.get(key)
            if cand is None:
                cand = Candidate(
                    university_code=r["university_code"],
                    university_name=r["university_name"],
                    group_code=r["group_code"],
                    subject_type=r["subject_type"],
                    first=list(r.get("first") or []),
                    second=list(r.get("second") or []),
                    pattern=r.get("pattern", "none"),
                    require_status=r.get("require_status", "待补"),
                )
                agg[key] = cand
            cand.yearly[int(r["year"])] = (
                r.get("min_rank"), r.get("min_score"), r.get("plan_count"),
            )

        reason: dict[str, int] = {
            "first_mismatch": 0,      # 首选科目不符（硬排除）
            "elective_mismatch": 0,   # 再选科目不符（仅当 second 非空时可能发生）
            "no_rank_data": 0,        # 全部年份均无投档位次
            "elective_unverified": 0,  # 再选科目待补 → 未校验（标注，不排除）
        }

        out: list[Candidate] = []
        for cand in agg.values():
            # ① 首选科目 — 硬过滤（数据 100% 可靠）
            if cand.first and subject_type not in cand.first:
                reason["first_mismatch"] += 1
                continue
            # ② 再选科目 — 分层
            if cand.second:
                if not _elective_match(cand.second, cand.pattern, electives):
                    reason["elective_mismatch"] += 1
                    continue
            else:
                # 待补 → 不排除，仅计数（结果带 require_note）
                reason["elective_unverified"] += 1
            # ③ 必须有投档数据
            if not any(v[0] is not None for v in cand.yearly.values()):
                reason["no_rank_data"] += 1
                continue
            out.append(cand)

        return out, {"reason_counts": reason}

    # ---------- 参考位次 L ----------

    def _build_item(self, cand: Candidate, weights: dict[int, float], rank: int) -> RecommendItem:
        """算参考位次 L（缺年归一化加权 + 计划变化修正）并产出 RecommendItem 主体。"""
        pairs: list[tuple[int, int, int | None, int | None]] = []  # (year, min_rank, min_score, plan)
        for y in sorted(cand.yearly):
            mr, ms, plan = cand.yearly[y]
            if mr is not None:
                pairs.append((y, mr, ms, plan))

        used_years = [p[0] for p in pairs]
        # 缺失年份 = 「期望参考的年份」中该院校没有可用位次数据的那些
        # （口径 §1.4：缺年必须显式标注，不得静默；期望年份取本次实际参与计算的 ref_years）
        missing_years = [y for y in sorted(weights) if y not in used_years]

        # 归一化加权（只对有数据的年份）
        wsum = sum(weights.get(y, 0.0) for y in used_years)
        if wsum <= 0:
            wsum = float(len(used_years)) or 1.0
            eff_w = {y: 1.0 for y in used_years}
        else:
            eff_w = {y: weights.get(y, 0.0) / wsum for y in used_years}

        ref_rank = sum(p[1] * eff_w[p[0]] for p in pairs)

        # 计划变化因子修正
        plan_note = ""
        plan_ratio = _plan_ratio(cand.yearly)
        if plan_ratio is not None and plan_ratio < settings.plan_shrink_threshold:
            # 计划缩减 → 参考位次调小（更靠前 = 更「冲」），保守化
            ref_rank *= (1 - settings.plan_shrink_factor)
            plan_note = f"计划较上年缩减（×{plan_ratio:.2f}），已做保守化修正"

        # ---- 缺年保守化修正（T10 回测定稿）----
        # 缺年比例越高 → 年际不确定性越大 → 参考位次向「更难」收缩（位次变小）。
        # 效果：缺年院校更难被列为「稳/保」，避免仅凭 1 年数据给出乐观结论。
        # 依据：实测相邻年位次预测 |误差| 中位 11.4%、p90 45–59%（见 data_caliber §7.5）。
        missing_ratio = len(missing_years) / len(weights) if weights else 0.0
        penalty = missing_ratio * settings.missing_penalty
        if len(used_years) == 1 and missing_ratio > 0:
            penalty += settings.single_year_extra_penalty
        if penalty > 0:
            ref_rank *= (1 - penalty)
            miss_txt = ",".join(str(y) for y in missing_years)
            plan_note = (plan_note + "；" if plan_note else "") + f"缺 {miss_txt} 年数据，已做保守化修正"

        latest = pairs[-1]
        return RecommendItem(
            university_code=cand.university_code,
            university_name=cand.university_name,
            group_code=cand.group_code,
            subject_type=cand.subject_type,
            ref_rank=round(ref_rank),
            ref_score=latest[2],
            latest_year=latest[0],
            latest_rank=latest[1],
            plan_count=latest[3],
            plan_ratio=round(plan_ratio, 3) if plan_ratio is not None else None,
            ref_years_used=used_years,
            ref_years_missing=missing_years,
            ref_years_note=_ref_years_note(used_years, missing_years),
            level_tags=cand.level_tags,
            school_province=cand.school_province,
            school_type=cand.school_type,
            require_status=cand.require_status,
            require_note=(
                "再选科目要求待补，请以当年招生计划为准" if not cand.second else ""
            ),
            plan_note=plan_note,
            evidence=self._evidence(pairs, eff_w),
        )

    @staticmethod
    def _evidence(pairs: list[tuple[int, int, int | None, int | None]], eff_w: dict[int, float]) -> list[str]:
        lines = []
        for y, mr, ms, _plan in pairs:
            w = eff_w.get(y, 0.0)
            score_txt = f"{ms} 分 / " if ms is not None else ""
            lines.append(f"{y} 年：投档最低 {score_txt}位次 {mr}（权重 {w:.2f}）")
        return lines

    # ---------- 分档 ----------

    @staticmethod
    def _classify(ref_rank: float, rank: int) -> str | None:
        """按考生位次 R 与院校参考位次 L 分档（口径 data_caliber §7.1）。"""
        basis = max(rank, settings.tier_min_rank_basis)
        if ref_rank <= settings.chong_ratio * basis:
            return "chong"
        if ref_rank <= settings.wen_ratio * basis:
            return "wen"
        if ref_rank <= settings.bao_ratio * basis:
            return "bao"
        return None  # 明显低于考生水平（L > 2R）→ 不作为推荐

    @staticmethod
    def _interval_text(tier: str, rank: int) -> str:
        c, w, b = settings.chong_ratio, settings.wen_ratio, settings.bao_ratio
        basis = max(rank, settings.tier_min_rank_basis)
        text = {
            "chong": f"参考位次 ≤ {c}×考生位次",
            "wen": f"{c}×考生位次 < 参考位次 ≤ {w}×考生位次",
            "bao": f"{w}×考生位次 < 参考位次 ≤ {b}×考生位次",
        }[tier]
        if basis != rank:
            text += f"（高分保护：按位次 {basis} 计算）"
        return text

    # ---------- 排序 ----------

    @staticmethod
    def _sort_key(it: RecommendItem) -> tuple:
        """档内排序主键：层次（985>211>双一流>普通）→ 地域热度 → 计划扩招优先。"""
        lv = max((LEVEL_WEIGHT.get(t, 0) for t in (it.level_tags or "").split("|") if t), default=0)
        hot = REGION_HOT.get(it.school_province or "", 1)
        ratio = it.plan_ratio if it.plan_ratio is not None else 1.0
        return (lv, hot, ratio)

    def _rank_tier(self, items: list[RecommendItem], rank: int, limit: int) -> list[RecommendItem]:
        """档内排序 → 同院校去重 → 截断。

        ⚠️ T09 修正（冒烟发现）：原实现只按 ``_sort_key`` 排序，**忽略 ``group_code``**，
        导致同一院校的多个专业组（如「中山大学(中外合作办学)」「中山大学(高校专项计划)」）
        在结果里并列霸榜、甚至出现同名三连，档位名义 10 所实际只有 3–4 所不同院校。
        修正后：
        1. **同院校只保留最优专业组** —— 按 ``_sort_key`` 降序后，同 ``university_code`` 取第一条；
           被丢弃的专业组数记入 ``TierNote``（调用侧可选展示，避免「静默丢弃」）。
        2. 在 ``_sort_key`` 之后追加**位次贴近度**维度（``ref_rank`` 越接近档位中心越优先），
           让同层次院校按「档次契合度」排，而不是按 code 偶然序。
        """
        # ① 按「层次 / 地域 / 计划」主序，再按「档次契合度」次序
        center, span = _tier_center(max(rank, settings.tier_min_rank_basis))
        ordered = sorted(
            items,
            key=lambda it: (
                self._sort_key(it),
                -abs((it.ref_rank or center) - center) / span,
                -it.group_code,   # 稳定兜底：组号小的优先（避免集合遍历顺序不定）
            ),
            reverse=True,
        )

        # ② 同院校去重（保留排序最优的那一条）
        seen: set[str] = set()
        deduped: list[RecommendItem] = []
        for it in ordered:
            if it.university_code in seen:
                continue
            seen.add(it.university_code)
            deduped.append(it)

        return deduped[:limit]

    # ---------- 解释（规则模板，非 LLM）----------

    @staticmethod
    def _caliber_text(usable_years: list[int], weights: dict[int, float] | None = None) -> str:
        """口径文字（**必须与本次实际参与测算的年份/权重一致**，防口径漂移）。

        ⚠️ T09 修正：原实现读的是全局 ``settings.year_weight_list``，当上层通过 T09
        `DataVersionService` 切换了参考窗口（如只留 2026 年、权重 1.0）时，口径文字仍会
        输出旧的 ``0.2/0.3/0.5``，与实际计算不符 —— 属真实缺陷，故改为按**本次生效权重**生成。
        """
        if not usable_years:
            return "无可用参考年份"
        span = f"{usable_years[0]}–{usable_years[-1]}" if len(usable_years) > 1 else f"{usable_years[0]}"
        if weights:
            # 逐年列出，形如「2024:0.2 / 2025:0.3 / 2026:0.5」，彻底消除「权重与年份对不上」的歧义
            wtxt = " / ".join(f"{y}:{weights.get(y, 0.0):g}" for y in usable_years)
        else:
            wtxt = "/".join(f"{w:g}" for w in settings.year_weight_list)
        return f"本方案基于 {span} 年广东投档数据，按 {wtxt}（年份:权重）加权测算（越近权重越高）"

    @staticmethod
    def _explanation(*, score: int, rank: int, subject_type: str,
                     groups: list[TierGroup], usable_years: list[int], province: str) -> str:
        parts = [
            f"考生为 {province} {subject_type}类，{score} 分（全省位次约 {rank}）。",
            f"系统在 {usable_years[0]}–{usable_years[-1]} 年真实投档数据中筛选同科类专业组，",
            "对近三年投档最低位次加权平均得到「参考位次」，再与考生位次比较分为三档：",
        ]
        for g in groups:
            if g.items:
                parts.append(f"{g.label}档 {g.count} 所（{g.interval}）；")
            else:
                parts.append(f"{g.label}档暂无符合条件的院校；")
        parts.append("以上均为历史投档数据推演，不代表录取结果。")
        return "".join(parts)

    @staticmethod
    def _risk_summary(groups: list[TierGroup], rank: int) -> str:
        """风险解释：只用「偏高/适中/偏低」，**禁止概率数字**。"""
        chong = next((g for g in groups if g.tier == "chong"), None)
        bao = next((g for g in groups if g.tier == "bao"), None)
        lines = [
            f"冲刺档院校历年投档位次普遍优于你当前位次（{rank}），录取难度偏高，建议作为尝试项，不作为主攻。",
        ]
        if bao and bao.items:
            lines.append("保底档院校历年投档位次明显低于你的位次，录取把握较大，可作为兜底，但需留意专业是否可接受。")
        else:
            lines.append("本次未筛出保底档院校，建议放宽地域或专业偏好，或关注当年招生计划变化。")
        if chong and not chong.items:
            lines.append("未筛出冲刺档院校，说明邻近范围内可冲刺的院校较少，可适当关注新增招生院校。")
        lines.append("投档线不等于录取线，且每年计划与报考热度都会变化，请结合当年招生计划与专业要求判断。")
        return "".join(lines)

    @staticmethod
    def _parent_summary(groups: list[TierGroup], score: int, rank: int, subject_type: str,
                        province: str, caliber: str) -> str:
        lines = [
            f"孩子在 {province} 属于 {subject_type}类，考了 {score} 分（全省大约第 {rank} 名）。",
        ]
        for g in groups:
            names = "、".join(it.university_name for it in g.items[:3])
            if g.items:
                more = f" 等 {g.count} 所" if g.count > 3 else ""
                lines.append(f"{g.label}一档：{names}{more}。")
            else:
                lines.append(f"{g.label}一档：暂无符合条件的选择。")
        lines.append(f"{caliber}。这些是往年情况，最终要以今年的招生计划和孩子的实际意愿为准。")
        return "".join(lines)


# ============================================================
# 纯函数辅助
# ============================================================


def _tier_center(rank: int) -> tuple[float, float]:
    """三档整体的「位次中心」与量程（用于把 ``ref_rank`` 归一化后做贴近度排序）。

    中心取 ``(0 + bao_ratio) / 2 × rank``，量程取 ``(bao_ratio - 0) × rank``：
    位次越接近中心（≈ 稳档中段），排序越靠前；冲到极致 / 保到极致都略靠后。
    这样同层次院校的排列有确定含义，而非依赖 dict 遍历顺序。
    """
    span = max(settings.bao_ratio * rank, 1.0)
    return (span / 2.0), span


def _elective_match(second: list[str], pattern: str, electives: list[str]) -> bool:
    """再选科目匹配（仅当 second 非空时调用）。"""
    if pattern == "all":
        return all(s in electives for s in second)
    if pattern == "any":
        return any(s in electives for s in second)
    return True  # none / 未知 → 不限


def _plan_ratio(yearly: dict[int, tuple[int | None, int | None, int | None]]) -> float | None:
    """最新两年的计划数比值（扩招 > 1，缩减 < 1）；不足两年或计划缺失返回 None。"""
    years = sorted(y for y in yearly if yearly[y][2] is not None)
    if len(years) < 2:
        return None
    y_new, y_old = years[-1], years[-2]
    new_plan = yearly[y_new][2]
    old_plan = yearly[y_old][2]
    if not old_plan:
        return None
    return new_plan / old_plan


def _ref_years_note(used: list[int], missing: list[int]) -> str:
    if not used:
        return "无参考年份数据"
    span = f"{used[0]}-{used[-1]}" if len(used) > 1 else str(used[0])
    if missing:
        return f"参考年份：{span}（参考年份不足，缺 {','.join(str(y) for y in missing)}）"
    return f"参考年份：{span}（完整）"


# ============================================================
# 单例（生产）
# ============================================================

_engine: RecommendEngine | None = None


def get_recommend_engine() -> RecommendEngine:
    global _engine
    if _engine is None:
        _engine = RecommendEngine(SqlRecommendRepository())
    return _engine


def reset_recommend_engine() -> None:
    global _engine
    _engine = None
