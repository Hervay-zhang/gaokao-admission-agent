"""T17 · 用户档案（CRUD + 结构化偏好 + 敏感字段脱敏落库）。

职责
----
1. **档案 CRUD**：省份 / 报考年份 / 科类 / 再选科目 / 意向城市·专业·院校类型；
2. **敏感字段脱敏落库（PIPL 红线）**：分数 → `score_hash`（加盐 sha256）+ `score_bucket`（10 分区间）；
   位次 → `rank_hash` + `rank_bucket`（千分区间）。**绝不落明文**；
3. **明文仅内存态**：`ProfileView` 携带明文分数/位次供推荐计算，**不写库、不进日志**；
4. **偏好注入**：`build_preference_block(profile)` 生成结构化偏好片段，供 T18 注入 Prompt。

分层设计（可离线测试）
----------------------
- :class:`ProfileStore` Protocol 定义存储接口；
- :class:`SqlProfileStore`（生产，MySQL）+ :class:`InMemoryProfileStore`（测试，内存）；
- 业务逻辑（脱敏、偏好组装）与存储解耦，单测**完全不依赖 MySQL**。

红线
----
- 分数/位次**只以 hash + bucket 落库**，任何路径都不得写入明文（`test_*_no_plaintext_in_db` 守护）；
- 日志一律用 ``mask_score`` / ``mask_rank``（``app.core.logging``），本模块不直接 log 明文；
- 分数范围 [100, 750]、位次 > 0，非法值**拒绝写入**而非静默修正。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.logging import get_logger, mask_rank, mask_score

logger = get_logger(__name__)

MIN_SCORE = 100
MAX_SCORE = 750

# 分数桶宽度（10 分一档，与 mask_score 口径一致）；位次桶宽度（1000 一档）
_SCORE_BUCKET = 10
_RANK_BUCKET = 1000


# ============================================================
#  脱敏工具（唯一实现，禁止在别处重复造）
# ============================================================


def _salt() -> str:
    return settings.score_hash_salt or ""


def hash_score(score: int) -> str:
    """分数加盐哈希（不可逆，用于同档位聚合统计，禁止用于反查明文）。"""
    return hashlib.sha256(f"{_salt()}:score:{int(score)}".encode()).hexdigest()


def hash_rank(rank: int) -> str:
    """位次加盐哈希。"""
    return hashlib.sha256(f"{_salt()}:rank:{int(rank)}".encode()).hexdigest()


def score_bucket(score: int) -> str:
    """分数 → 10 分区间桶，如 612 → '610-619'（与 logging.mask_score 同口径）。"""
    low = (int(score) // _SCORE_BUCKET) * _SCORE_BUCKET
    return f"{low}-{low + _SCORE_BUCKET - 1}"


def rank_bucket(rank: int) -> str:
    """位次 → 千分区间桶，如 35412 → '35000-35999'。"""
    low = (int(rank) // _RANK_BUCKET) * _RANK_BUCKET
    return f"{low}-{low + _RANK_BUCKET - 1}"


# ============================================================
#  数据契约
# ============================================================


@dataclass
class ProfileInput:
    """档案**输入**（可含明文分数/位次，仅内存态流转）。"""

    province: str = "广东"
    current_year: int = 2026
    subject_type: str = ""             # 物理 / 历史
    elective_subjects: list[str] = field(default_factory=list)
    score: int | None = None           # 明文，仅内存
    rank: int | None = None            # 明文，仅内存
    preferred_cities: list[str] = field(default_factory=list)
    preferred_majors: list[str] = field(default_factory=list)
    preferred_types: list[str] = field(default_factory=list)
    anon_id: str | None = None
    owner_username: str | None = None


@dataclass
class ProfileView:
    """档案**视图**（落库后回读；**不含明文敏感字段**）。

    ``score`` / ``rank`` 字段**永不从库回读**（库里只有 hash+bucket）—— 回读时置 None，
    需要明文时由上层（如推荐请求）现用现传，绝不落库。
    """

    id: int | None
    province: str
    current_year: int
    subject_type: str
    elective_subjects: list[str] = field(default_factory=list)
    score_bucket: str | None = None
    rank_bucket: str | None = None
    preferred_cities: list[str] = field(default_factory=list)
    preferred_majors: list[str] = field(default_factory=list)
    preferred_types: list[str] = field(default_factory=list)
    anon_id: str | None = None
    owner_username: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "province": self.province,
            "current_year": self.current_year,
            "subject_type": self.subject_type,
            "elective_subjects": self.elective_subjects,
            "score_bucket": self.score_bucket,
            "rank_bucket": self.rank_bucket,
            "preferred_cities": self.preferred_cities,
            "preferred_majors": self.preferred_majors,
            "preferred_types": self.preferred_types,
            "anon_id": self.anon_id,
            "owner_username": self.owner_username,
        }


# ============================================================
#  存储接口（Protocol + 双实现）
# ============================================================


@runtime_checkable
class ProfileStore(Protocol):
    """档案存储接口（生产=MySQL，测试=内存）。"""

    def create(self, row: dict[str, Any]) -> int: ...

    def get(self, profile_id: int) -> dict[str, Any] | None: ...

    def find_by_owner(self, owner_username: str) -> dict[str, Any] | None: ...

    def update(self, profile_id: int, fields: dict[str, Any]) -> bool: ...

    def delete(self, profile_id: int) -> bool: ...


class SqlProfileStore:
    """MySQL 实现（``user_profiles`` 表）。"""

    def __init__(self, db) -> None:  # noqa: ANN001 - Session（避免循环导入）
        self.db = db

    def _commit(self) -> None:
        """提交；失败时立即 rollback 把 Session 拉回可用状态。

        为什么必须 rollback：Session 一旦 flush 失败就进入「待回滚」状态，之后同一个
        Session 上的任何操作都会抛 ``PendingRollbackError``。线上 2026-10-29 的事故
        就是「首次写入连接被 MySQL 断开 → 事务作废 → 后续请求全 500」被这个缺省放大的。
        """
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def create(self, row: dict[str, Any]) -> int:
        from app.db.models import UserProfile

        obj = UserProfile(**row)
        self.db.add(obj)
        self._commit()
        return int(obj.id)

    def get(self, profile_id: int) -> dict[str, Any] | None:
        from app.db.models import UserProfile

        obj = self.db.get(UserProfile, profile_id)
        return _orm_to_dict(obj) if obj else None

    def find_by_owner(self, owner_username: str) -> dict[str, Any] | None:
        from app.db.models import UserProfile

        obj = (
            self.db.query(UserProfile)
            .filter(UserProfile.owner_username == owner_username)
            .order_by(UserProfile.updated_at.desc(), UserProfile.id.desc())
            .first()
        )
        return _orm_to_dict(obj) if obj else None

    def update(self, profile_id: int, fields: dict[str, Any]) -> bool:
        from app.db.models import UserProfile

        obj = self.db.get(UserProfile, profile_id)
        if obj is None:
            return False
        for k, v in fields.items():
            setattr(obj, k, v)
        self._commit()
        return True

    def find_by_anon(self, anon_id: str) -> dict[str, Any] | None:
        from app.db.models import UserProfile

        obj = self.db.query(UserProfile).filter(UserProfile.anon_id == anon_id).first()
        return _orm_to_dict(obj) if obj else None

    def delete(self, profile_id: int) -> bool:
        from app.db.models import UserProfile

        obj = self.db.get(UserProfile, profile_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self._commit()
        return True


def _orm_to_dict(obj) -> dict[str, Any]:  # noqa: ANN001
    return {
        "id": obj.id,
        "anon_id": obj.anon_id,
        "owner_username": obj.owner_username,
        "province": obj.province,
        "current_year": obj.current_year,
        "subject_type": obj.subject_type,
        "elective_subjects": obj.elective_subjects or "",
        "score_hash": obj.score_hash,
        "score_bucket": obj.score_bucket,
        "rank_hash": obj.rank_hash,
        "rank_bucket": obj.rank_bucket,
        "preferred_cities": obj.preferred_cities,
        "preferred_majors": obj.preferred_majors,
        "preferred_types": obj.preferred_types,
    }


class InMemoryProfileStore:
    """内存实现（测试用，语义与 SQL 版一致）。"""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next = 1

    def create(self, row: dict[str, Any]) -> int:
        pid = self._next
        self._next += 1
        stored = dict(row)
        stored["id"] = pid
        self.rows[pid] = stored
        return pid

    def get(self, profile_id: int) -> dict[str, Any] | None:
        row = self.rows.get(profile_id)
        return dict(row) if row else None

    def find_by_owner(self, owner_username: str) -> dict[str, Any] | None:
        rows = [row for row in self.rows.values() if row.get("owner_username") == owner_username]
        if not rows:
            return None
        return dict(sorted(rows, key=lambda item: int(item.get("id") or 0), reverse=True)[0])

    def update(self, profile_id: int, fields: dict[str, Any]) -> bool:
        if profile_id not in self.rows:
            return False
        self.rows[profile_id].update(fields)
        return True

    def find_by_anon(self, anon_id: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if row.get("anon_id") == anon_id:
                return dict(row)
        return None

    def delete(self, profile_id: int) -> bool:
        if profile_id not in self.rows:
            return False
        del self.rows[profile_id]
        return True


# ============================================================
#  业务逻辑
# ============================================================


class ProfileService:
    """档案服务（脱敏 + CRUD + 偏好组装）。"""

    def __init__(self, store: ProfileStore) -> None:
        self.store = store

    # ---------- 写入 ----------

    def create_profile(self, inp: ProfileInput) -> ProfileView:
        """建立档案。**敏感字段脱敏后才落库**（明文不留存）。"""
        self._validate(inp)
        row: dict[str, Any] = {
            "anon_id": inp.anon_id,
            "owner_username": inp.owner_username,
            "province": inp.province or "广东",
            "current_year": inp.current_year,
            "subject_type": inp.subject_type,
            "elective_subjects": ",".join(inp.elective_subjects),
            # 脱敏：只落 hash + bucket
            "score_hash": hash_score(inp.score) if inp.score is not None else None,
            "score_bucket": score_bucket(inp.score) if inp.score is not None else None,
            "rank_hash": hash_rank(inp.rank) if inp.rank is not None else None,
            "rank_bucket": rank_bucket(inp.rank) if inp.rank is not None else None,
            "preferred_cities": json.dumps(inp.preferred_cities, ensure_ascii=False),
            "preferred_majors": json.dumps(inp.preferred_majors, ensure_ascii=False),
            "preferred_types": json.dumps(inp.preferred_types, ensure_ascii=False),
        }
        pid = self.store.create(row)
        # 日志：只出区间，不出明文
        logger.info(
            "档案已建立 id=%s 省份=%s 分数区间=%s 位次区间=%s",
            pid, row["province"], row["score_bucket"] or "未知", row["rank_bucket"] or "未知",
        )
        return self.get_profile(pid)  # type: ignore[return-value]

    def update_preferences(
        self,
        profile_id: int,
        *,
        preferred_cities: list[str] | None = None,
        preferred_majors: list[str] | None = None,
        preferred_types: list[str] | None = None,
    ) -> ProfileView | None:
        """更新偏好（非敏感字段，直接落库）。"""
        fields: dict[str, Any] = {}
        if preferred_cities is not None:
            fields["preferred_cities"] = json.dumps(preferred_cities, ensure_ascii=False)
        if preferred_majors is not None:
            fields["preferred_majors"] = json.dumps(preferred_majors, ensure_ascii=False)
        if preferred_types is not None:
            fields["preferred_types"] = json.dumps(preferred_types, ensure_ascii=False)
        if not fields:
            return self.get_profile(profile_id)
        ok = self.store.update(profile_id, fields)
        return self.get_profile(profile_id) if ok else None

    def update_score_rank(self, profile_id: int, *, score: int | None, rank: int | None) -> ProfileView | None:
        """更新分数/位次（**重新脱敏**后落库；明文不落库）。"""
        fields: dict[str, Any] = {}
        if score is not None:
            if not (MIN_SCORE <= int(score) <= MAX_SCORE):
                raise ValidationError(
                    f"分数超出合法范围 [{MIN_SCORE}, {MAX_SCORE}]：{score}",
                    detail={"code": "SCORE_OUT_OF_RANGE", "score": score},
                )
            fields["score_hash"] = hash_score(score)
            fields["score_bucket"] = score_bucket(score)
        if rank is not None:
            if int(rank) <= 0:
                raise ValidationError("位次必须为正整数", detail={"code": "RANK_INVALID"})
            fields["rank_hash"] = hash_rank(rank)
            fields["rank_bucket"] = rank_bucket(rank)
        if not fields:
            return self.get_profile(profile_id)
        ok = self.store.update(profile_id, fields)
        if ok:
            logger.info(
                "档案已更新 id=%s 分数区间=%s 位次区间=%s",
                profile_id, fields.get("score_bucket", "-"), fields.get("rank_bucket", "-"),
            )
        return self.get_profile(profile_id) if ok else None

    # ---------- 读取 ----------

    def get_profile(self, profile_id: int) -> ProfileView | None:
        row = self.store.get(profile_id)
        return _row_to_view(row) if row else None

    def get_by_anon(self, anon_id: str) -> ProfileView | None:
        row = self.store.find_by_anon(anon_id)
        return _row_to_view(row) if row else None

    def get_by_owner(self, owner_username: str) -> ProfileView | None:
        if not owner_username:
            return None
        row = self.store.find_by_owner(owner_username)
        return _row_to_view(row) if row else None

    def delete_profile(self, profile_id: int) -> bool:
        """删除档案（PIPL：用户有权删除）。删除后查询返回 None。"""
        ok = self.store.delete(profile_id)
        if ok:
            logger.info("档案已删除 id=%s", profile_id)
        return ok

    # ---------- 校验 ----------

    @staticmethod
    def _validate(inp: ProfileInput) -> None:
        if inp.score is not None and not (MIN_SCORE <= int(inp.score) <= MAX_SCORE):
            raise ValidationError(
                f"分数超出合法范围 [{MIN_SCORE}, {MAX_SCORE}]：{inp.score}",
                detail={"code": "SCORE_OUT_OF_RANGE", "score": inp.score},
            )
        if inp.rank is not None and int(inp.rank) <= 0:
            raise ValidationError("位次必须为正整数", detail={"code": "RANK_INVALID"})
        if inp.subject_type and inp.subject_type not in ("物理", "历史"):
            raise ValidationError(
                f"首选科目必须是「物理」或「历史」：{inp.subject_type}",
                detail={"code": "SUBJECT_INVALID"},
            )


def _row_to_view(row: dict[str, Any]) -> ProfileView:
    return ProfileView(
        id=row.get("id"),
        province=row.get("province", ""),
        current_year=row.get("current_year", 0),
        subject_type=row.get("subject_type", ""),
        elective_subjects=[s for s in (row.get("elective_subjects") or "").split(",") if s],
        score_bucket=row.get("score_bucket"),
        rank_bucket=row.get("rank_bucket"),
        preferred_cities=_load_json_list(row.get("preferred_cities")),
        preferred_majors=_load_json_list(row.get("preferred_majors")),
        preferred_types=_load_json_list(row.get("preferred_types")),
        anon_id=row.get("anon_id"),
        owner_username=row.get("owner_username"),
    )


def _load_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        obj = json.loads(raw)
        return [str(x) for x in obj] if isinstance(obj, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ============================================================
#  偏好注入（供 T18 Prompt 编排）
# ============================================================


def build_preference_block(profile: ProfileView | None) -> str:
    """把档案组装成**结构化偏好片段**，供 T18 注入 System Prompt。

    只输出**非敏感 + 区间化**信息（省份/科类/再选/意向城市专业/分数区间桶），
    **绝不输出明文分数/位次**（PIPL 红线）。无档案时返回空串（调用方据此省略该段）。
    """
    if profile is None:
        return ""
    lines: list[str] = []
    if profile.province:
        lines.append(f"- 报考省份：{profile.province}")
    if profile.current_year:
        lines.append(f"- 报考年份：{profile.current_year}")
    if profile.subject_type:
        lines.append(f"- 首选科目：{profile.subject_type}")
    if profile.elective_subjects:
        lines.append(f"- 再选科目：{'、'.join(profile.elective_subjects)}")
    if profile.score_bucket:
        lines.append(f"- 分数区间：{profile.score_bucket}")
    if profile.rank_bucket:
        lines.append(f"- 位次区间：{profile.rank_bucket}")
    if profile.preferred_cities:
        lines.append(f"- 意向城市：{'、'.join(profile.preferred_cities)}")
    if profile.preferred_majors:
        lines.append(f"- 意向专业：{'、'.join(profile.preferred_majors)}")
    if profile.preferred_types:
        lines.append(f"- 意向院校类型：{'、'.join(profile.preferred_types)}")
    if not lines:
        return ""
    return "【考生档案】\n" + "\n".join(lines)


def mask_for_log(score: int | None, rank: int | None) -> str:
    """日志友好的脱敏串（供别处复用，避免各处重复造轮子）。"""
    return f"分数={mask_score(score)} 位次={mask_rank(rank)}"


# ============================================================
#  生产便捷构造
# ============================================================


def get_profile_service(db=None) -> ProfileService:  # noqa: ANN001
    """生产构造：SQL 存储 + 档案服务。

    **必须传入请求级 Session**。历史教训：早期版本在 ``db is None`` 时自建
    ``SessionLocal()`` 并缓存成进程级单例，该连接永不归还连接池 →
    既绕过 ``pool_pre_ping`` 探活，又会在 MySQL ``wait_timeout`` 到期被断开后
    把坏事务永久留在单例上，导致建档接口此后 100% 失败。
    """
    if db is None:
        raise ValueError(
            "get_profile_service 需要请求级 Session（禁止自建进程级单例会话）"
        )
    return ProfileService(SqlProfileStore(db))


def is_mock() -> bool:
    return bool(settings.mock_mode)
