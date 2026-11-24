"""数据版本服务（T09）——把「年份」做成可配置、可切换、可对比的一等公民。

三个「年份」概念，**严禁混淆**（任务卡 T09「易混淆」）
----------------------------------------------------
=================  ================  ==========================================
概念                字段               含义
=================  ================  ==========================================
报考年份            ``current_year``   考生要报考的那一年（如 2026）；用户填分所指之年
参考年份            ``ref_years``      用于冲稳保加权测算的历年（如 2024,2025,2026）
验证年份            ``valid_year``     回测时用来「对答案」的年份（T10），只属于回测语境
=================  ================  ==========================================

本服务只管前两者 + 权重，并保证：

1. **唯一有效行**：``data_version`` 表任意时刻最多一行 ``is_active=1``；
2. **时效红线**：任何 ``ref_years`` 元素 **不得 > ``current_year``**（禁止用未来年份数据测算）；
3. **可回滚**：切换前把旧配置整体存档（``DataVersionSnapshot``），``rollback()`` 可原样恢复；
4. **可审计**：每次切换/回滚写 ``audit_log``（动作 + 操作者 + 变更前后 JSON，脱敏）；
5. **口径文字随结果返回**：``caliber_text()`` 产出一句人可读口径，供报告/接口直接透出（防口径漂移）；
6. **配置读取可注入**：引擎侧通过 ``DataVersionProvider`` 拿配置，生产走 MySQL + Redis 缓存，
   测试走内存，**不依赖数据库即可单测**。

缓存失效
--------
缓存键 ``dv:active:{dataset_epoch}``；``dataset_epoch`` 由「本进程内每次写入自增」+ 落库行的
``(id, created_at)`` 摘要共同构成，**任何写入都会改变键**，旧键自然不再命中，无需显式删除。
Redis 不可用时静默降级为直查（缓存是优化，不是依赖）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import DataMissingError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 缓存 TTL（秒）——1 天；真正失效靠 epoch，TTL 仅兜底
CACHE_TTL_SECONDS = 86400
# 配置合法性：参考年份至少 1 个、最多 8 个（防止 .env / 表单塞入异常长串）
MIN_REF_YEARS = 1
MAX_REF_YEARS = 8


# ============================================================
# 数据结构
# ============================================================


@dataclass(frozen=True)
class DataVersionConfig:
    """一份数据版本配置（不可变值对象）。"""

    current_year: int
    ref_years: tuple[int, ...]
    year_weights: tuple[float, ...]
    source: str = "env"          # env / db / override，便于追溯配置来源
    row_id: int | None = None    # data_version 表主键（env 兜底时为 None）
    note: str = ""

    # ---------- 校验 ----------

    def validate(self) -> None:
        """配置自洽性校验（任一不满足即抛 ``ValidationError``，**绝不静默修正**）。"""
        if not isinstance(self.current_year, int) or self.current_year < 2000 or self.current_year > 2100:
            raise ValidationError(f"current_year 必须是 2000–2100 的整数，收到 {self.current_year!r}")

        years = list(self.ref_years)
        if not (MIN_REF_YEARS <= len(years) <= MAX_REF_YEARS):
            raise ValidationError(f"ref_years 需 {MIN_REF_YEARS}–{MAX_REF_YEARS} 个年份，收到 {len(years)} 个：{years}")
        if len(set(years)) != len(years):
            raise ValidationError(f"ref_years 存在重复年份：{years}")
        if years != sorted(years):
            raise ValidationError(
                f"ref_years 必须按「从旧到新」排列，收到 {years}"
                "（否则 year_weights 会与年份错配，口径：data_caliber §1.1）"
            )

        if len(self.year_weights) != len(years):
            raise ValidationError(
                f"year_weights({len(self.year_weights)} 个) 与 ref_years({len(years)} 个) 数量不一致"
            )
        if any(w < 0 for w in self.year_weights):
            raise ValidationError(f"year_weights 不得为负：{list(self.year_weights)}")
        if abs(sum(self.year_weights) - 1.0) > 1e-6:
            raise ValidationError(f"year_weights 之和应为 1，当前为 {sum(self.year_weights)}")

        # ---- 时效红线 ----
        future = [y for y in years if y > self.current_year]
        if future:
            raise ValidationError(
                f"ref_years 含晚于 current_year({self.current_year}) 的年份 {future}，"
                "违反时效红线（禁止使用未来年份数据测算）"
            )

    # ---------- 派生 ----------

    @property
    def weight_map(self) -> dict[int, float]:
        """``{year: weight}``（键序与 ``ref_years`` 一致）。"""
        return dict(zip(self.ref_years, self.year_weights, strict=True))

    @property
    def span_text(self) -> str:
        """年份跨度文案：``2024–2026`` / ``2026``。"""
        if not self.ref_years:
            return "无"
        if len(self.ref_years) == 1:
            return str(self.ref_years[0])
        return f"{self.ref_years[0]}–{self.ref_years[-1]}"

    @property
    def weights_text(self) -> str:
        """权重文案：``2024:0.2 / 2025:0.3 / 2026:0.5``（年份:权重，消除错配歧义）。"""
        return " / ".join(f"{y}:{w:g}" for y, w in zip(self.ref_years, self.year_weights, strict=True))

    @property
    def fingerprint(self) -> str:
        """配置指纹（用于缓存键 / 结果留痕）。"""
        raw = f"{self.current_year}|{','.join(map(str, self.ref_years))}|{self.weights_text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def caliber_text(self) -> str:
        """口径文字（**必须随结果返回**，防口径漂移）。

        与 ``RecommendEngine._caliber_text`` **同格式同语义**（均为「年份:权重」逐项列出），
        保证「接口层口径」与「引擎层口径」不会各说各话。
        """
        return (
            f"本方案基于 {self.span_text} 年广东投档数据，按 {self.weights_text}"
            "（年份:权重）加权测算（越近权重越高）"
        )

    def to_dict(self) -> dict:
        return {
            "current_year": self.current_year,
            "ref_years": list(self.ref_years),
            "year_weights": list(self.year_weights),
            "source": self.source,
            "row_id": self.row_id,
            "note": self.note,
            "fingerprint": self.fingerprint,
            "caliber_text": self.caliber_text(),
        }

    @classmethod
    def from_strings(
        cls,
        *,
        current_year: int,
        ref_years: str | list[int] | tuple[int, ...],
        year_weights: str | list[float] | tuple[float, ...],
        source: str = "env",
        row_id: int | None = None,
        note: str = "",
    ) -> DataVersionConfig:
        """从「逗号分隔字符串」或序列构造（.env / DB 两种来源共用）。"""
        if isinstance(ref_years, str):
            years = tuple(int(y.strip()) for y in ref_years.split(",") if y.strip())
        else:
            years = tuple(int(y) for y in ref_years)

        if isinstance(year_weights, str):
            ws = tuple(float(w.strip()) for w in year_weights.split(",") if w.strip())
        else:
            ws = tuple(float(w) for w in year_weights)

        return cls(
            current_year=int(current_year),
            ref_years=years,
            year_weights=ws,
            source=source,
            row_id=row_id,
            note=note,
        )


@dataclass(frozen=True)
class DataVersionSnapshot:
    """一次切换的存档（用于回滚）。"""

    id: int
    config: DataVersionConfig
    before: DataVersionConfig
    actor: str
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.id,
            "before": self.before.to_dict(),
            "after": self.config.to_dict(),
            "actor": self.actor,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True)
class TrendPoint:
    """趋势序列中的一年。"""

    year: int
    min_rank: int | None
    min_score: int | None
    plan_count: int | None
    # 与「上一年」比较的位次变化量（正值 = 位次变大 = 难度下降；负值 = 位次变小 = 难度上升）
    rank_delta: int | None = None
    direction: str = "unknown"   # up（上涨/变难） / down（下降/变易） / flat / unavailable

    @property
    def direction_text(self) -> str:
        return {"up": "难度上升", "down": "难度下降", "flat": "基本持平", "unavailable": "无数据"}[self.direction]


@dataclass(frozen=True)
class TrendResult:
    """「近年位次趋势」结果。"""

    university_code: str
    university_name: str
    group_code: int
    subject_type: str
    province: str
    points: list[TrendPoint]
    years: list[int]
    direction: str
    direction_text: str
    summary: str
    missing_years: list[int] = field(default_factory=list)
    note: str = ""


# ============================================================
# 数据源协议（可注入：生产 MySQL，测试内存）
# ============================================================


@runtime_checkable
class DataVersionRepository(Protocol):
    """``data_version`` + ``audit_log`` 的最小数据访问接口。"""

    def get_active(self) -> DataVersionConfig | None:
        """当前唯一有效行；无则返回 None。"""
        ...

    def save_active(self, config: DataVersionConfig, actor: str) -> DataVersionConfig:
        """把 ``config`` 落为唯一有效行（旧行 ``is_active`` 置 0），返回带 row_id 的配置。"""
        ...

    def write_audit(self, action: str, actor: str, detail: str) -> None:
        """写审计日志（detail 为已序列化的脱敏 JSON 字符串）。"""
        ...

    def admission_trend(
        self, *, province: str, subject_type: str, university_code: str, group_code: int, years: list[int]
    ) -> list[dict]:
        """取某专业组指定年份的投档行（字段：year/min_rank/min_score/plan_count/university_name）。"""
        ...


# ============================================================
# 内存实现（测试 / 离线）
# ============================================================


class InMemoryDataVersionRepository:
    """内存数据源：配置 + 审计 + 投档行都在内存里，**不依赖 MySQL**。"""

    def __init__(
        self,
        *,
        active: DataVersionConfig | None = None,
        admission_rows: list[dict] | None = None,
    ) -> None:
        self._active = active
        self._rows = admission_rows or []
        self.audits: list[dict] = []
        self._seq = 0

    # ---- 配置 ----

    def get_active(self) -> DataVersionConfig | None:
        return self._active

    def save_active(self, config: DataVersionConfig, actor: str) -> DataVersionConfig:
        self._seq += 1
        saved = DataVersionConfig(
            current_year=config.current_year,
            ref_years=config.ref_years,
            year_weights=config.year_weights,
            source="db",
            row_id=self._seq,
            note=config.note,
        )
        self._active = saved
        return saved

    # ---- 审计 ----

    def write_audit(self, action: str, actor: str, detail: str) -> None:
        self.audits.append({"action": action, "actor": actor, "detail": detail})

    # ---- 投档行 ----

    def admission_trend(
        self, *, province: str, subject_type: str, university_code: str, group_code: int, years: list[int]
    ) -> list[dict]:
        out = []
        for r in self._rows:
            if r.get("province") != province:
                continue
            if r.get("subject_type") != subject_type:
                continue
            if r.get("university_code") != university_code:
                continue
            if int(r.get("group_code", -1)) != int(group_code):
                continue
            if years and r.get("year") not in years:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r["year"])


# ============================================================
# 生产实现（MySQL）
# ============================================================


class SqlDataVersionRepository:
    """MySQL 数据源（每方法短连接会话）。"""

    def __init__(self) -> None:
        from app.db.session import SessionLocal

        self._SessionLocal = SessionLocal

    def get_active(self) -> DataVersionConfig | None:
        from sqlalchemy import select

        from app.db.models import DataVersion

        with self._SessionLocal() as db:
            stmt = (
                select(DataVersion)
                .where(DataVersion.is_active.is_(True))
                .order_by(DataVersion.id.desc())
                .limit(1)
            )
            row = db.execute(stmt).scalars().first()
            if row is None:
                return None
            return DataVersionConfig.from_strings(
                current_year=int(row.current_year),
                ref_years=row.ref_years,
                year_weights=row.year_weights,
                source="db",
                row_id=int(row.id),
                note=row.note or "",
            )

    def save_active(self, config: DataVersionConfig, actor: str) -> DataVersionConfig:
        from sqlalchemy import update

        from app.db.models import DataVersion

        with self._SessionLocal() as db:
            # 旧行全部置为无效（唯一有效行语义）
            db.execute(update(DataVersion).where(DataVersion.is_active.is_(True)).values(is_active=False))
            row = DataVersion(
                current_year=config.current_year,
                ref_years=",".join(str(y) for y in config.ref_years),
                year_weights=",".join(f"{w:g}" for w in config.year_weights),
                is_active=True,
                note=config.note or None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return DataVersionConfig.from_strings(
                current_year=int(row.current_year),
                ref_years=row.ref_years,
                year_weights=row.year_weights,
                source="db",
                row_id=int(row.id),
                note=row.note or "",
            )

    def write_audit(self, action: str, actor: str, detail: str) -> None:
        from app.db.models import AuditLog

        with self._SessionLocal() as db:
            db.add(AuditLog(action=action, actor=actor, detail=detail))
            db.commit()

    def admission_trend(
        self, *, province: str, subject_type: str, university_code: str, group_code: int, years: list[int]
    ) -> list[dict]:
        from sqlalchemy import select

        from app.db.models import AdmissionLine

        with self._SessionLocal() as db:
            stmt = (
                select(
                    AdmissionLine.year,
                    AdmissionLine.university_name,
                    AdmissionLine.min_rank,
                    AdmissionLine.min_score,
                    AdmissionLine.plan_count,
                )
                .where(
                    AdmissionLine.province == province,
                    AdmissionLine.subject_type == subject_type,
                    AdmissionLine.university_code == university_code,
                    AdmissionLine.group_code == group_code,
                    AdmissionLine.year.in_(years),
                )
                .order_by(AdmissionLine.year)
            )
            return [
                {
                    "year": int(y),
                    "university_name": name,
                    "min_rank": mr,
                    "min_score": ms,
                    "plan_count": plan,
                }
                for y, name, mr, ms, plan in db.execute(stmt).all()
            ]


# ============================================================
# 缓存（Redis，可选依赖）
# ============================================================


class _Cache:
    """极简缓存：Redis 不可用时全部降级为 miss，不抛异常。"""

    def __init__(self, epoch: str) -> None:
        self.epoch = epoch

    def _key(self) -> str:
        return f"dv:active:{self.epoch}"

    def get(self) -> str | None:
        try:
            from app.core.redis import get_redis

            return _run_sync(lambda: get_redis().get(self._key()))
        except Exception:  # noqa: BLE001 —— 缓存故障不得影响主流程
            return None

    def set(self, payload: str) -> None:
        try:
            from app.core.redis import get_redis

            _run_sync(lambda: get_redis().set(self._key(), payload, ex=CACHE_TTL_SECONDS))
        except Exception:  # noqa: BLE001
            pass


def _run_sync(make_coro):
    """委托共享桥接（专用临时客户端，防共享连接池被临时事件循环毒化；见 app.core.redis）。"""
    from app.core.redis import run_sync

    return run_sync(make_coro)


def _payload_to_config(s: str) -> DataVersionConfig | None:
    try:
        d = json.loads(s)
        cfg = DataVersionConfig.from_strings(
            current_year=int(d["current_year"]),
            ref_years=d["ref_years"],
            year_weights=d["year_weights"],
            source=d.get("source", "db"),
            row_id=d.get("row_id"),
            note=d.get("note", ""),
        )
        return cfg
    except Exception:  # noqa: BLE001 —— 缓存脏数据一律当 miss
        return None


# ============================================================
# 配置提供者（引擎侧依赖倒置；生产读 DB+缓存，测试读内存/env）
# ============================================================


class DataVersionProvider:
    """给推荐引擎提供「当前生效配置」。

    生产：``DbDataVersionProvider``（Redis 缓存 + MySQL + .env 兜底）。
    测试：``StaticDataVersionProvider``（固定配置，零依赖）。
    """

    def get_config(self) -> DataVersionConfig:  # pragma: no cover - 协议默认实现
        raise NotImplementedError


class StaticDataVersionProvider(DataVersionProvider):
    """固定配置提供者（测试 / 回测 / 离线）。"""

    def __init__(self, config: DataVersionConfig) -> None:
        self._config = config

    def get_config(self) -> DataVersionConfig:
        return self._config


class DbDataVersionProvider(DataVersionProvider):
    """生产提供者：Redis 缓存 → MySQL → ``.env`` 兜底。

    ``.env`` 兜底的意义：首次部署 / ``data_version`` 表为空时，系统仍能以
    ``settings`` 中的默认年份口径正常出结果，**不会因缺配置而不可用**。
    """

    def __init__(self, repo: DataVersionRepository | None = None, *, use_cache: bool = True) -> None:
        self._repo = repo or SqlDataVersionRepository()
        self._use_cache = use_cache
        self._epoch = 0

    def invalidate(self) -> None:
        """写入后调用：epoch 自增 → 缓存键变化 → 旧键自然失效。"""
        self._epoch += 1

    def _epoch_str(self) -> str:
        return f"e{self._epoch}"

    def get_config(self) -> DataVersionConfig:
        cache = _Cache(self._epoch_str()) if self._use_cache else None
        if cache is not None:
            hit = cache.get()
            if hit is not None:
                cfg = _payload_to_config(hit)
                if cfg is not None:
                    return cfg

        cfg = self._repo.get_active()
        if cfg is None:
            cfg = DataVersionConfig.from_strings(
                current_year=settings.current_year,
                ref_years=settings.ref_year_list,
                year_weights=settings.year_weight_list,
                source="env",
            )
        cfg.validate()

        if cache is not None:
            cache.set(json.dumps(
                {
                    "current_year": cfg.current_year,
                    "ref_years": list(cfg.ref_years),
                    "year_weights": list(cfg.year_weights),
                    "source": cfg.source,
                    "row_id": cfg.row_id,
                    "note": cfg.note,
                },
                ensure_ascii=False,
            ))
        return cfg


# ============================================================
# 服务
# ============================================================


class DataVersionService:
    """数据版本服务（配置读写 / 切换 / 回滚 / 趋势对比 / 口径文字）。"""

    def __init__(
        self,
        repo: DataVersionRepository,
        *,
        provider: DataVersionProvider | None = None,
        use_cache: bool = False,
    ) -> None:
        self._repo = repo
        self._provider = provider
        self._use_cache = use_cache
        # 进程内切换存档（供 rollback 用）；生产可换成 DB 表，此处保底「切换可回滚」
        self._snapshots: list[DataVersionSnapshot] = []
        self._seq = 0

    # ---------- 读取 ----------

    def get_active(self) -> DataVersionConfig:
        """当前生效配置（DB → .env 兜底），并做合法性校验。"""
        cfg = self._repo.get_active()
        if cfg is None:
            cfg = DataVersionConfig.from_strings(
                current_year=settings.current_year,
                ref_years=settings.ref_year_list,
                year_weights=settings.year_weight_list,
                source="env",
            )
        cfg.validate()
        return cfg

    def caliber_text(self) -> str:
        """口径文字（报告 / 接口必须透出）。"""
        return self.get_active().caliber_text()

    # ---------- 切换 ----------

    def switch(
        self,
        *,
        current_year: int | None = None,
        ref_years: list[int] | str | None = None,
        year_weights: list[float] | str | None = None,
        actor: str = "admin",
        note: str = "",
        dry_run: bool = False,
    ) -> DataVersionConfig:
        """切换数据版本窗口（新一年数据入库后调用）。

        未传的字段沿用当前值。**切换前先校验（含时效红线），校验不过直接拒绝、不落库**；
        ``dry_run=True`` 时只校验不落库（供「预览影响面」用）。
        """
        before = self.get_active()

        def _parse_years(raw: list[int] | str) -> tuple[int, ...]:
            return DataVersionConfig.from_strings(
                current_year=0, ref_years=raw, year_weights=[1.0]
            ).ref_years

        def _parse_weights(raw: list[float] | str) -> tuple[float, ...]:
            return DataVersionConfig.from_strings(
                current_year=0, ref_years=[2000], year_weights=raw
            ).year_weights

        new_years = _parse_years(ref_years) if ref_years is not None else before.ref_years

        if year_weights is not None:
            new_weights = _parse_weights(year_weights)
        elif ref_years is not None and len(new_years) != len(before.year_weights):
            # 只改 ref_years 数量、未给 weights → 按「等权重」自动补齐
            # （若沿用旧权重会出现数量不匹配直接报错；等权重是显式、可追溯的兜底，非静默猜测）
            new_weights = tuple(round(1.0 / len(new_years), 6) for _ in range(len(new_years)))
        else:
            new_weights = before.year_weights

        candidate = DataVersionConfig(
            current_year=int(current_year) if current_year is not None else before.current_year,
            ref_years=new_years,
            year_weights=new_weights,
            source="db",
            note=note or before.note,
        )
        candidate.validate()

        if dry_run:
            return candidate

        saved = self._repo.save_active(candidate, actor)
        snapshot = self._archive(before=before, after=saved, actor=actor)
        self._audit(
            action="switch_year",
            actor=actor,
            payload={
                "before": before.to_dict(),
                "after": saved.to_dict(),
                "note": note,
                "snapshot_id": snapshot.id,
            },
        )
        self._invalidate()
        logger.info(
            "数据版本已切换：current_year=%s ref_years=%s（actor=%s）",
            saved.current_year, list(saved.ref_years), actor,
        )
        return saved

    # ---------- 回滚 ----------

    def rollback(self, snapshot_id: int | None = None, *, actor: str = "admin") -> DataVersionConfig:
        """回滚到某个存档（默认最近一次切换前的配置）。"""
        if not self._snapshots:
            raise DataMissingError(
                "无可用切换存档，无法回滚",
                detail={"code": "NO_SNAPSHOT"},
            )
        snap = (
            next((s for s in reversed(self._snapshots) if s.id == snapshot_id), None)
            if snapshot_id is not None
            else self._snapshots[-1]
        )
        if snap is None:
            raise DataMissingError(
                f"未找到存档 id={snapshot_id}",
                detail={"code": "SNAPSHOT_NOT_FOUND", "snapshot_id": snapshot_id},
            )

        target = snap.before
        target.validate()
        saved = self._repo.save_active(target, actor)
        self._audit(
            action="rollback_year",
            actor=actor,
            payload={
                "snapshot_id": snap.id,
                "before": self.get_active().to_dict(),
                "after": saved.to_dict(),
            },
        )
        self._invalidate()
        logger.info("数据版本已回滚至存档 %s（actor=%s）", snap.id, actor)
        return saved

    def list_snapshots(self) -> list[DataVersionSnapshot]:
        return list(self._snapshots)

    # ---------- 趋势对比 ----------

    def trend(
        self,
        *,
        university_code: str,
        group_code: int,
        subject_type: str,
        province: str = "广东",
        years: list[int] | None = None,
    ) -> TrendResult:
        """「近年位次趋势」：某院校专业组历年投档最低位次序列 + 逐年涨跌方向。

        口径
        ----
        - 位次**数值变大** = 该专业组更容易进（难度下降）→ ``direction="down"``；
        - 位次**数值变小** = 更靠前、更难进（难度上升）→ ``direction="up"``；
        - 缺年的年份仍在序列中占位（``direction="unavailable"``），**不插值、不猜测**。
        """
        cfg = self.get_active()
        use_years = list(years) if years else list(cfg.ref_years)
        if not use_years:
            raise DataMissingError("无可用参考年份", detail={"code": "NO_REF_YEARS"})
        # 时效红线同样适用于对比：禁止查询未来年份
        future = [y for y in use_years if y > cfg.current_year]
        if future:
            raise ValidationError(
                f"年份 {future} 晚于当前报考年份 {cfg.current_year}，禁止查询未来数据"
            )

        rows = self._repo.admission_trend(
            province=province,
            subject_type=subject_type,
            university_code=university_code,
            group_code=int(group_code),
            years=sorted(use_years),
        )
        by_year = {int(r["year"]): r for r in rows}
        name = next((r.get("university_name") for r in rows if r.get("university_name")), "")

        points: list[TrendPoint] = []
        prev_rank: int | None = None
        for y in sorted(use_years):
            r = by_year.get(y)
            mr = int(r["min_rank"]) if r and r.get("min_rank") is not None else None
            ms = int(r["min_score"]) if r and r.get("min_score") is not None else None
            plan = int(r["plan_count"]) if r and r.get("plan_count") is not None else None

            if mr is None or prev_rank is None:
                delta, direction = None, ("unavailable" if mr is None else "flat")
            else:
                delta = mr - prev_rank
                if delta == 0:
                    direction = "flat"
                elif delta > 0:
                    direction = "down"   # 位次变大 → 更容易进
                else:
                    direction = "up"     # 位次变小 → 更难进
            points.append(TrendPoint(year=y, min_rank=mr, min_score=ms, plan_count=plan,
                                     rank_delta=delta, direction=direction))
            if mr is not None:
                prev_rank = mr

        missing = [p.year for p in points if p.min_rank is None]
        overall = _overall_direction(points)
        return TrendResult(
            university_code=university_code,
            university_name=name,
            group_code=int(group_code),
            subject_type=subject_type,
            province=province,
            points=points,
            years=[p.year for p in points],
            direction=overall,
            direction_text={
                "up": "整体难度上升", "down": "整体难度下降",
                "flat": "整体基本持平", "unavailable": "数据不足，无法判断",
            }[overall],
            summary=_trend_summary(name, group_code, points, overall),
            missing_years=missing,
            note=(f"缺 {','.join(str(y) for y in missing)} 年数据，未做插值" if missing else ""),
        )

    # ---------- 内部 ----------

    def _archive(self, *, before: DataVersionConfig, after: DataVersionConfig, actor: str) -> DataVersionSnapshot:
        self._seq += 1
        snap = DataVersionSnapshot(id=self._seq, config=after, before=before, actor=actor)
        self._snapshots.append(snap)
        return snap

    def _audit(self, *, action: str, actor: str, payload: dict) -> None:
        self._repo.write_audit(action, actor, json.dumps(payload, ensure_ascii=False, default=str))

    def _invalidate(self) -> None:
        if isinstance(self._provider, DbDataVersionProvider):
            self._provider.invalidate()


# ============================================================
# 纯函数辅助
# ============================================================


def _overall_direction(points: list[TrendPoint]) -> str:
    """整体方向 = 首尾两个「有数据年份」的位次比较。"""
    valid = [p for p in points if p.min_rank is not None]
    if len(valid) < 2:
        return "unavailable"
    delta = valid[-1].min_rank - valid[0].min_rank
    if delta == 0:
        return "flat"
    return "down" if delta > 0 else "up"


def _trend_summary(name: str, group_code: int, points: list[TrendPoint], overall: str) -> str:
    label = f"{name}（专业组 {group_code}）" if name else f"该校专业组 {group_code}"
    segs = []
    for p in points:
        if p.min_rank is None:
            segs.append(f"{p.year} 年无数据")
        else:
            score_txt = f"{p.min_score} 分/" if p.min_score is not None else ""
            segs.append(f"{p.year} 年 {score_txt}位次 {p.min_rank}")
    head = "；".join(segs)
    tail = {
        "up": "位次逐年靠前，报考难度整体上升。",
        "down": "位次逐年放大，报考难度整体下降。",
        "flat": "位次基本稳定，报考难度无明显变化。",
        "unavailable": "可用年份不足，暂无法判断趋势。",
    }[overall]
    return f"{label}：{head}。{tail}"


# ============================================================
# 单例（生产）
# ============================================================

_service: DataVersionService | None = None
_provider: DbDataVersionProvider | None = None


def get_data_version_provider() -> DbDataVersionProvider:
    """生产用配置提供者（Redis 缓存 + MySQL + .env 兜底）。"""
    global _provider
    if _provider is None:
        _provider = DbDataVersionProvider(use_cache=True)
    return _provider


def get_data_version_service() -> DataVersionService:
    """生产用服务单例。"""
    global _service
    if _service is None:
        repo = SqlDataVersionRepository()
        _service = DataVersionService(repo, provider=get_data_version_provider())
    return _service


def reset_data_version_service() -> None:
    """重置单例（测试 / 配置变更后调用）。"""
    global _service, _provider
    _service = None
    _provider = None
