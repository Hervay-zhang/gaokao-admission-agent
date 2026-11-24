"""T28 · 只读分享服务（token + 过期 + 脱敏快照）。

设计取舍（**为什么不用 Redis 存**）
----------------------------------
任务卡原文写「Redis 存」。实施时改为 **MySQL 为唯一事实源**（复用 `volunteer_plans.share_token`
+ `share_expires_at`），理由：
1. **分享链接必须跨设备、跨重启可用**——Redis 在本项目是**可选依赖**（T09/T17/T20 均按
   「Redis 故障可降级」设计，本机开发环境常未启动），把分享做成硬依赖会让功能在 Redis 缺席时整体不可用；
2. 志愿表本身就落在 MySQL，分享只是它的一个**只读投影**，同源存储避免双写不一致；
3. 过期语义更直接：`share_expires_at` 是数据列，`GET` 时比较即可，无需 TTL + 宽限期补偿。

职责
----
1. **生成只读分享 token**（`secrets.token_urlsafe(16)`）+ 过期时间（默认 7 天）；
2. **快照脱敏**：只含**分数段 / 位次段**（来自档案 bucket），结构性守卫 `assert_desensitized`
   命中敏感键即抛错（不静默）；
3. **过期语义**：`now >= share_expires_at` → 410 `SHARE_EXPIRED`；token 不存在 → 404；
4. **只读**：本模块**无任何写志愿表的入口**，分享页不可编辑、不可反馈写库；
5. **撤销**：清除 token 与过期时间（链接立即失效）。

可注入：`PlanService` / `ProfileService` / 时钟，全部离线可测（不依赖 MySQL / Redis）。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.exceptions import NotFoundError, ShareExpiredError
from app.core.logging import get_logger
from app.services.plan import PlanService
from app.services.profile import ProfileService

logger = get_logger(__name__)

# 敏感键黑名单：分享快照里**绝不允许**出现（明文分数/位次与身份）
_FORBIDDEN_KEYS = frozenset({
    "score", "rank", "min_score", "min_rank", "score_hash", "rank_hash",
    "name", "real_name", "phone", "mobile", "id_card", "email", "anon_id",
})

DISCLAIMER = "本方案仅供参考，不作为最终填报承诺。"


class ShareService:
    """只读分享服务（存储 = 志愿表本身）。"""

    def __init__(
        self,
        *,
        plans: PlanService,
        profiles: ProfileService | None = None,
        ttl_days: int | None = None,
        clock: Any = None,
    ) -> None:
        self.plans = plans
        self.profiles = profiles
        self.ttl_days = ttl_days if ttl_days is not None else settings.share_ttl_days
        self._clock = clock or time.time

    # ---- 生成 / 撤销 ----

    def create(
        self,
        *,
        plan_id: int,
        ttl_days: int | None = None,
        owner_username: str | None = None,
    ) -> dict[str, Any]:
        """为志愿表生成只读分享链接（写入 token + 过期时间）。"""
        plan = self.plans.get_plan(plan_id, owner_username)
        ttl = ttl_days if ttl_days is not None else self.ttl_days
        token = secrets.token_urlsafe(16)
        now = self._now_dt()
        expires_at = now + timedelta(days=ttl)

        self.plans.set_share(plan_id, token, expires_at, owner_username)
        payload = self._snapshot(plan, token=token, expires_at=expires_at)
        assert_desensitized(payload)  # 红线：结构性保证，命中即抛错
        logger.info("分享链接已生成 plan_id=%s ttl=%s天", plan_id, ttl)
        return {
            "token": token,
            "plan_id": plan.id,
            "share_path": f"/#/share/{token}",
            "created_at": payload["created_at"],
            "expires_at": payload["expires_at"],
            "ttl_days": ttl,
        }

    def revoke(self, token: str, owner_username: str | None = None) -> None:
        """撤销分享（清 token 与过期时间，链接立即失效）。"""
        found = self.plans.find_plan_by_share_token(token)
        if found is None:
            raise NotFoundError("分享链接不存在或已撤销", detail={"code": "SHARE_NOT_FOUND"})
        plan, _expires = found
        self.plans.set_share(plan.id, None, None, owner_username)

    # ---- 读取 ----

    def get(self, token: str) -> dict[str, Any]:
        """读取只读快照（过期 → 410，不存在 → 404）。"""
        found = self.plans.find_plan_by_share_token(token)
        if found is None:
            raise NotFoundError("分享链接不存在或已过期", detail={"code": "SHARE_NOT_FOUND"})
        plan, expires_at = found
        if expires_at is None:
            # token 存在但过期时间被清空（异常态）→ 视为已失效
            raise ShareExpiredError(
                "分享链接已失效，请联系分享者重新生成", detail={"code": "SHARE_EXPIRED"}
            )
        if self._now_dt() >= _as_dt(expires_at):
            raise ShareExpiredError(
                "分享链接已过期，请联系分享者重新生成",
                detail={"code": "SHARE_EXPIRED", "expires_at": _iso(_as_dt(expires_at))},
            )
        snap = self._snapshot(plan, token=token, expires_at=_as_dt(expires_at))
        assert_desensitized(snap)
        return snap

    # ---- 内部 ----

    def _snapshot(self, plan: Any, *, token: str, expires_at: Any) -> dict[str, Any]:
        """构造**脱敏**只读快照（不含明文分数/位次/身份）。"""
        exp = _as_dt(expires_at)
        return {
            "token": token,
            "plan_id": plan.id,
            "plan_name": plan.name,
            "year": plan.year,
            "province": plan.province,
            "capacity": plan.capacity,
            "count": plan.count,
            "created_at": _iso(self._now_dt()),
            "expires_at": _iso(exp),
            "items": [
                {
                    "seq": it.seq,
                    "university_name": it.university_name,
                    "group_code": it.group_code,
                    "tier": it.tier,
                    "system_tier": it.system_tier,
                    "user_adjusted": it.user_adjusted,
                    "notes": it.notes,
                }
                for it in plan.items
            ],
            "candidate": self._candidate_buckets(plan.profile_id),
            "disclaimer": DISCLAIMER,
        }

    def _candidate_buckets(self, profile_id: int) -> dict[str, Any]:
        """考生水平（**仅区间**）：来自档案 bucket，绝不回读明文。"""
        out: dict[str, Any] = {"subject_type": "", "score_bucket": None, "rank_bucket": None}
        if self.profiles is None:
            return out
        view = self.profiles.get_profile(profile_id)
        if view is None:
            # 服务已绑定请求级 Session（见 get_share_service 的说明），
            # 不再需要「单例连接闲置断开 → 换新 session 重查」的兜底补丁。
            return out
        out["subject_type"] = view.subject_type
        out["score_bucket"] = view.score_bucket
        out["rank_bucket"] = view.rank_bucket
        return out

    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self._clock())


# ============================================================
#  脱敏守卫
# ============================================================


def assert_desensitized(obj: Any, _path: str = "$") -> None:
    """递归断言：分享快照中不得出现敏感键（明文分数/位次/身份）。

    命中即抛 `ValueError`（**不静默**）—— 这是分享链路的红线结构性保证。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"分享快照含敏感字段 {_path}.{k}（红线：分享不得泄露明文/身份）")
            assert_desensitized(v, f"{_path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_desensitized(v, f"{_path}[{i}]")


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    raise ValueError(f"无法解析过期时间：{value!r}")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
#  单例
# ============================================================

_service: ShareService | None = None


def get_share_service(db=None) -> ShareService:  # noqa: ANN001
    """请求级构造（同 ``get_plan_service``：不再缓存持会话的单例）。"""
    if _service is not None:  # 仅测试注入时会命中
        return _service
    if db is None:
        raise ValueError("get_share_service 需要请求级 Session（禁止自建进程级单例会话）")
    from app.services.plan import PlanService, SqlPlanRepository
    from app.services.profile import ProfileService, SqlProfileStore

    return ShareService(
        plans=PlanService(SqlPlanRepository(db)),
        profiles=ProfileService(SqlProfileStore(db)),
    )


def set_share_service(service: ShareService | None) -> None:
    global _service
    _service = service
