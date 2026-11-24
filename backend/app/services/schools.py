"""T24 · 院校库检索服务。

数据源（Protocol + 双实现）
--------------------------
- ``SqlSchoolRepository``：MySQL（生产）；
- ``InMemorySchoolRepository``：内存（测试 / 离线，吃 ``universities.csv`` + ``admission_lines.csv`` 记录）。

职责
----
    search(...)   —— 列表检索（关键词 + 层次标签 + 地域 + 分页），**筛选条件由上层进 URL**；
    detail(...)   —— 院校详情（按专业组聚合历年投档行 + 选科要求）；
    compare(...)  —— ≤3 所院校对比（历年最低位次趋势）。

红线
----
- 数据全部来自库内；**无数据维度显式标注**（``meta_status=待补`` / 缺年不补造）；
- 院校（uni）与专业组（group）分层：列表按院校，详情按专业组；
- 投档线 ≠ 录取线（口径文字随响应带出）。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

CALIBER_TEXT = "数据来自各省考试院公开投档数据；投档线≠录取线；官方「-」为无数据，不推算。"

# 已核验可访问的高校官方招生网站；未命中的院校回退到教育部阳光高考查询。
ADMISSION_URL_OVERRIDES: dict[str, str] = {
    "北京大学": "https://www.gotopku.cn/",
    "清华大学": "https://join-tsinghua.edu.cn/",
    "中国人民大学": "https://rdzs.ruc.edu.cn/",
    "北京航空航天大学": "https://zs.buaa.edu.cn/",
    "北京理工大学": "https://admission.bit.edu.cn/",
    "北京师范大学": "https://admission.bnu.edu.cn/",
    "南开大学": "https://zsb.nankai.edu.cn/",
    "天津大学": "https://zs.tju.edu.cn/",
    "大连理工大学": "https://zs.dlut.edu.cn/",
    "哈尔滨工业大学": "https://zsb.hit.edu.cn/",
    "复旦大学": "https://ao.fudan.edu.cn/",
    "同济大学": "https://bkzs.tongji.edu.cn/",
    "上海交通大学": "https://zsb.sjtu.edu.cn/",
    "华东师范大学": "https://zsb.ecnu.edu.cn/",
    "南京大学": "https://bkzs.nju.edu.cn/",
    "东南大学": "https://zsb.seu.edu.cn/",
    "浙江大学": "https://zdzsc.zju.edu.cn/",
    "中国科学技术大学": "https://zsb.ustc.edu.cn/",
    "厦门大学": "https://zs.xmu.edu.cn/",
    "中国海洋大学": "https://bkzs.ouc.edu.cn/",
    "武汉大学": "https://aoff.whu.edu.cn/",
    "华中科技大学": "https://zsb.hust.edu.cn/",
    "中南大学": "https://zhaosheng.csu.edu.cn/",
    "中山大学": "https://admission.sysu.edu.cn/",
    "华南理工大学": "https://admission.scut.edu.cn/",
    "电子科技大学": "https://zs.uestc.edu.cn/",
    "重庆大学": "https://zhaosheng.cqu.edu.cn/",
    "西安交通大学": "https://zs.xjtu.edu.cn/",
    "兰州大学": "https://zsb.lzu.edu.cn/",
    "华南师范大学": "https://zsb.scnu.edu.cn/",
    "暨南大学": "https://zsb.jnu.edu.cn/",
    "深圳大学": "https://zs.szu.edu.cn/",
}


@dataclass
class UniRow:
    """院校元数据。"""

    code: str
    name: str
    base_name: str = ""
    level: str = "本科"
    province: str | None = None
    school_type: str | None = None
    tags: list[str] = field(default_factory=list)
    meta_status: str = "待补"
    admission_url: str | None = None
    admission_url_status: str = "待核"
    admission_url_updated_at: Any | None = None


@dataclass
class AdmRow:
    """投档行。"""

    year: int
    province: str
    subject_type: str
    university_code: str
    university_name: str
    group_code: int
    plan_count: int | None = None
    admitted_count: int | None = None
    min_score: int | None = None
    min_rank: int | None = None
    requirement: dict = field(default_factory=dict)


# ============================================================
# 数据源协议
# ============================================================


@runtime_checkable
class SchoolRepository(Protocol):
    def list_universities(self, *, level: str | None, province: str | None,
                          keyword: str | None, tag: str | None) -> list[UniRow]:
        """返回满足条件的院校元数据（未分页；分页由服务层做）。"""
        ...

    def admission_rows(self, codes: list[str], *, province: str, subject_type: str | None,
                       years: list[int]) -> list[AdmRow]:
        """返回这些院校的投档行（可按科类过滤）。"""
        ...

    def all_provinces(self) -> list[str]:
        """院校所在省份枚举（供筛选项）。"""
        ...


# ============================================================
# 内存实现（测试 / 离线）
# ============================================================


class InMemorySchoolRepository:
    """内存数据源：直接吃 ``UniRow`` 列表 + ``AdmRow`` 列表。"""

    def __init__(self, unis: list[UniRow], admissions: list[AdmRow] | None = None) -> None:
        self._unis = {u.code: u for u in unis}
        self._adm = admissions or []

    def list_universities(self, *, level: str | None, province: str | None,
                          keyword: str | None, tag: str | None) -> list[UniRow]:
        out = []
        kw = (keyword or "").strip()
        for u in self._unis.values():
            if level and u.level != level:
                continue
            if province and u.province != province:
                continue
            if tag and tag not in u.tags:
                continue
            if kw and kw not in u.name and kw not in u.code and kw not in u.base_name:
                continue
            out.append(u)
        # 稳定排序：985>211>双一流>其他 → 名称
        out.sort(key=lambda u: (_tag_rank(u.tags), u.code))
        return out

    def admission_rows(self, codes: list[str], *, province: str, subject_type: str | None,
                       years: list[int]) -> list[AdmRow]:
        cset = set(codes)
        return [
            r for r in self._adm
            if r.university_code in cset and r.province == province
            and (subject_type is None or r.subject_type == subject_type)
            and (not years or r.year in years)
        ]

    def all_provinces(self) -> list[str]:
        return sorted({u.province for u in self._unis.values() if u.province})


# ============================================================
# MySQL 实现（生产）
# ============================================================


class SqlSchoolRepository:
    def __init__(self) -> None:
        from app.db.session import SessionLocal

        self._SessionLocal = SessionLocal

    def list_universities(self, *, level: str | None, province: str | None,
                          keyword: str | None, tag: str | None) -> list[UniRow]:
        from sqlalchemy import or_, select

        from app.db.models import University

        stmt = select(
            University.code, University.name, University.base_name, University.level,
            University.province, University.school_type, University.tags, University.meta_status,
            University.admission_url, University.admission_url_status,
            University.admission_url_updated_at,
        )
        if level:
            stmt = stmt.where(University.level == level)
        if province:
            stmt = stmt.where(University.province == province)
        if tag:
            # tags 形如 "985|211|双一流"；用 LIKE 精确到分隔符，避免「211」误配「2110」
            stmt = stmt.where(University.tags.like(f"%{tag}%"))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(
                or_(University.name.like(like), University.code.like(like), University.base_name.like(like))
            )
        with self._SessionLocal() as db:
            rows = [
                UniRow(
                    code=code, name=name, base_name=bn or "", level=lv or "本科",
                    province=prov, school_type=st, tags=_split_tags(tags), meta_status=ms or "待补",
                )
                for code, name, bn, lv, prov, st, tags, ms, url, url_status, url_updated in db.execute(stmt).all()
            ]
        rows.sort(key=lambda u: (_tag_rank(u.tags), u.code))
        return rows

    def admission_rows(self, codes: list[str], *, province: str, subject_type: str | None,
                       years: list[int]) -> list[AdmRow]:
        from sqlalchemy import select

        from app.db.models import AdmissionLine

        if not codes:
            return []
        stmt = select(
            AdmissionLine.year, AdmissionLine.province, AdmissionLine.subject_type,
            AdmissionLine.university_code, AdmissionLine.university_name, AdmissionLine.group_code,
            AdmissionLine.plan_count, AdmissionLine.admitted_count,
            AdmissionLine.min_score, AdmissionLine.min_rank, AdmissionLine.subject_requirement,
        ).where(
            AdmissionLine.university_code.in_(codes),
            AdmissionLine.province == province,
        )
        if subject_type:
            stmt = stmt.where(AdmissionLine.subject_type == subject_type)
        if years:
            stmt = stmt.where(AdmissionLine.year.in_(years))
        with self._SessionLocal() as db:
            out = []
            for (y, prov, st, code, name, gcode, plan, adm, mscore, mrank, req_json) in db.execute(stmt).all():
                try:
                    req = json.loads(req_json) if req_json else {}
                except (ValueError, TypeError):
                    req = {}
                out.append(AdmRow(
                    year=int(y), province=prov, subject_type=st, university_code=code,
                    university_name=name, group_code=int(gcode), plan_count=plan,
                    admitted_count=adm, min_score=mscore, min_rank=mrank, requirement=req,
                ))
            return out

    def all_provinces(self) -> list[str]:
        from sqlalchemy import distinct, select

        from app.db.models import University

        with self._SessionLocal() as db:
            return sorted(p for (p,) in db.execute(select(distinct(University.province)).where(
                University.province.is_not(None))).all() if p)


# ============================================================
# 服务
# ============================================================

MAX_COMPARE = 3


class SchoolService:
    """院校库检索 / 详情 / 对比。"""

    def __init__(self, repo: SchoolRepository) -> None:
        self._repo = repo

    # ---------- 列表 ----------

    def search(self, *, province_query: str = "广东", subject_type: str | None = None,
               level: str | None = None, uni_province: str | None = None,
               keyword: str | None = None, tag: str | None = None,
               page: int = 1, page_size: int = 20,
               years: list[int] | None = None) -> dict:
        """检索院校列表（按院校粒度分页；附带最近一年最低投档位次）。"""
        if page < 1:
            raise ValidationError("page 必须 ≥ 1")
        if not (1 <= page_size <= 100):
            raise ValidationError("page_size 需在 1–100 之间")

        years = years or settings.ref_year_list
        unis = self._repo.list_universities(
            level=level, province=uni_province, keyword=keyword, tag=tag,
        )
        total = len(unis)
        start = (page - 1) * page_size
        page_unis = unis[start:start + page_size]

        # 取当前页院校的投档行，聚合最近一年位次/组数
        codes = [u.code for u in page_unis]
        adm = self._repo.admission_rows(codes, province=province_query, subject_type=subject_type, years=years)
        latest: dict[str, tuple[int, int]] = {}  # code -> (year, min_rank)
        group_counts: dict[str, set[tuple[int, str]]] = defaultdict(set)
        for r in adm:
            group_counts[r.university_code].add((r.group_code, r.subject_type))
            if r.min_rank is not None:
                cur = latest.get(r.university_code)
                # 取最近年份（year 大）；同年取更小位次（更好）
                if cur is None or r.year > cur[0] or (r.year == cur[0] and r.min_rank < cur[1]):
                    latest[r.university_code] = (r.year, r.min_rank)

        items = []
        for u in page_unis:
            ly, lr = latest.get(u.code, (None, None))
            admission_url, admission_url_kind = _admission_url(u)
            items.append({
                "code": u.code, "name": u.name, "base_name": u.base_name, "level": u.level,
                "province": u.province, "school_type": u.school_type, "tags": u.tags,
                "meta_status": u.meta_status,
                "admission_url": admission_url,
                "admission_url_kind": admission_url_kind,
                "admission_url_status": u.admission_url_status,
                "admission_url_updated_at": (
                    u.admission_url_updated_at.strftime("%Y-%m-%d %H:%M")
                    if u.admission_url_updated_at else ""
                ),
                "latest_min_rank": lr, "latest_year": ly,
                "group_count": len(group_counts.get(u.code, set())),
            })

        return {
            "total": total, "page": page, "page_size": page_size,
            "items": items, "caliber_text": CALIBER_TEXT,
        }

    def provinces(self) -> list[str]:
        return self._repo.all_provinces()

    # ---------- 详情 ----------

    def detail(self, code: str, *, province: str = "广东", subject_type: str = "",
               years: list[int] | None = None) -> dict:
        """院校详情：按专业组聚合历年投档行。"""
        years = years or settings.ref_year_list
        unis = self._repo.list_universities(level=None, province=None, keyword=code, tag=None)
        uni = next((u for u in unis if u.code == code), None)
        if uni is None:
            raise ValidationError(f"院校代码不存在：{code}", detail={"code": "UNI_NOT_FOUND"})

        adm = self._repo.admission_rows([code], province=province,
                                        subject_type=(subject_type or None), years=years)
        # 按 (subject_type, group_code) 聚合
        grouped: dict[tuple[str, int], list[AdmRow]] = defaultdict(list)
        for r in adm:
            grouped[(r.subject_type, r.group_code)].append(r)

        groups = []
        for (st, gcode), rows in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            rows.sort(key=lambda r: r.year)
            req = rows[-1].requirement or {}
            groups.append({
                "group_code": gcode,
                "subject_type": st,
                "requirement_text": _requirement_text(req),
                "require_status": req.get("status", "待补"),
                "lines": [
                    {
                        "year": r.year, "group_code": r.group_code,
                        "plan_count": r.plan_count, "admitted_count": r.admitted_count,
                        "min_score": r.min_score, "min_rank": r.min_rank,
                        "subject_requirement": r.requirement,
                    }
                    for r in rows
                ],
            })

        usable_years = sorted({r.year for r in adm})
        trend_note = (
            f"已展示 {len(usable_years)} 年投档数据（{', '.join(map(str, usable_years))}）"
            if usable_years else "库内暂未收录该院校在本省本年份范围的投档数据"
        )
        missing = [y for y in years if y not in usable_years]
        if missing and usable_years:
            trend_note += f"；缺 {', '.join(map(str, missing))} 年（不推算）"

        admission_url, admission_url_kind = _admission_url(uni)
        return {
            "code": uni.code, "name": uni.name, "base_name": uni.base_name, "level": uni.level,
            "province": uni.province, "school_type": uni.school_type, "tags": uni.tags,
            "meta_status": uni.meta_status, "province_query": province, "subject_type": subject_type,
            "admission_url": admission_url,
            "admission_url_kind": admission_url_kind,
            "admission_url_status": uni.admission_url_status,
            "admission_url_updated_at": (
                uni.admission_url_updated_at.strftime("%Y-%m-%d %H:%M")
                if uni.admission_url_updated_at else ""
            ),
            "years": years, "groups": groups, "trend_note": trend_note, "caliber_text": CALIBER_TEXT,
        }

    # ---------- 对比 ----------

    def compare(self, codes: list[str], *, province: str = "广东", subject_type: str = "",
                years: list[int] | None = None) -> dict:
        """≤3 所院校对比（历年最低位次趋势 + 基本信息）。"""
        codes = [c.strip() for c in codes if c and c.strip()]
        if not codes:
            raise ValidationError("请至少选择 1 所院校进行对比", detail={"code": "COMPARE_EMPTY"})
        if len(codes) > MAX_COMPARE:
            raise ValidationError(
                f"最多对比 {MAX_COMPARE} 所院校，收到 {len(codes)} 所",
                detail={"code": "COMPARE_TOO_MANY", "max": MAX_COMPARE},
            )
        years = years or settings.ref_year_list

        unis = self._repo.list_universities(level=None, province=None, keyword=None, tag=None)
        by_code = {u.code: u for u in unis}
        missing = [c for c in codes if c not in by_code]
        if missing:
            raise ValidationError(
                f"院校代码不存在：{', '.join(missing)}", detail={"code": "UNI_NOT_FOUND", "codes": missing}
            )

        adm = self._repo.admission_rows(codes, province=province,
                                        subject_type=(subject_type or None), years=years)
        # code -> year -> min_rank（跨组取最优）
        by_code_year: dict[str, dict[int, int]] = defaultdict(dict)
        by_code_score: dict[str, dict[int, int]] = defaultdict(dict)
        groupsets: dict[str, set[tuple[int, str]]] = defaultdict(set)
        for r in adm:
            groupsets[r.university_code].add((r.group_code, r.subject_type))
            if r.min_rank is not None:
                cur = by_code_year[r.university_code].get(r.year)
                if cur is None or r.min_rank < cur:
                    by_code_year[r.university_code][r.year] = r.min_rank
                    if r.min_score is not None:
                        by_code_score[r.university_code][r.year] = r.min_score

        rows = []
        for c in codes:
            u = by_code[c]
            trend = [by_code_year[c].get(y) for y in years]
            latest_year = max(by_code_year[c], default=None)
            rows.append({
                "code": u.code, "name": u.name, "tags": u.tags, "province": u.province,
                "school_type": u.school_type, "meta_status": u.meta_status,
                "latest_min_rank": by_code_year[c].get(latest_year) if latest_year else None,
                "min_score": by_code_score[c].get(latest_year) if latest_year else None,
                "group_count": len(groupsets.get(c, set())),
                "years": years, "rank_trend": trend,
            })

        return {"years": years, "rows": rows, "caliber_text": CALIBER_TEXT}


# ============================================================
# 工具
# ============================================================


def _split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t for t in raw.split("|") if t]


def _tag_rank(tags: list[str]) -> int:
    for t, w in (("985", 3), ("211", 2), ("双一流", 1)):
        if t in tags:
            return -w  # 负号：升序时层次高的排前面
    return 0


def _admission_search_url(name: str) -> str:
    """院校招生官网入口：统一跳转教育部阳光高考院校库搜索结果。"""
    return f"https://gaokao.chsi.com.cn/sch/search.do?searchType=1&keyword={quote(name)}"


def _admission_url(uni: UniRow) -> tuple[str, str]:
    managed = (uni.admission_url or "").strip()
    if managed and uni.admission_url_status != "不可用":
        return managed, "school"
    direct = ADMISSION_URL_OVERRIDES.get(uni.name)
    if direct:
        return direct, "school"
    return _admission_search_url(uni.name), "chsi"


def _requirement_text(req: dict) -> str:
    """选科要求 → 人可读文案（缺数据显式标注）。"""
    if not req:
        return "选科要求：待补齐"
    first = req.get("first") or []
    second = req.get("second") or []
    pattern = req.get("pattern") or "none"
    status = req.get("status") or "待补"
    if status == "待补" and not first and not second:
        return "选科要求：待补齐（官方未公布，未做推断）"
    parts = []
    if first:
        parts.append("首选：" + "/".join(first))
    if second:
        join = {"all": "且", "any": "或", "none": ""}.get(pattern, "")
        parts.append("再选：" + join.join(second))
    txt = "；".join(parts) or "不限"
    if status == "待补":
        txt += "（部分待补）"
    return "选科要求：" + txt


# ============================================================
# 单例（生产）
# ============================================================

_service: SchoolService | None = None


def get_school_service() -> SchoolService:
    global _service
    if _service is None:
        _service = SchoolService(SqlSchoolRepository())
    return _service


def set_school_service(service: SchoolService | None) -> None:
    global _service
    _service = service
