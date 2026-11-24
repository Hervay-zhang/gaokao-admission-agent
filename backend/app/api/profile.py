"""T23 · 画像测评与档案接口。

- ``POST /api/profile`` —— 测评表单建档（**脱敏落库**，回执只含区间）
- ``GET /api/rank``    —— 分数 → 位次 + 近三年等位分（位次直填优先于换算）

红线
----
- 模式由**省份映射**（前端配置），后端只收科类/科目，不收「模式」字段（防错）；
- 位次用户可直填，**优先于**按分数换算；
- 明文分数/位次仅内存态流转，落库前脱敏（hash + bucket），回执**永不回读明文**；
- 未落地数据省份 → 200 + DATA_MISSING（明确文案「数据待补齐」，不伪造位次）；
- 缺年等位分 → 该年 ``score=null`` + note「数据待补」，整体不失败。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_account
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.profile import EquivScoreOut, ProfileCreateRequest, ProfileOut, RankResponse
from app.services.profile import InMemoryProfileStore, ProfileInput, ProfileService
from app.services.score import DataMissingError, get_score_engine

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["profile"])

# ============================================================
# 档案服务（请求级构造；测试可注入 InMemory 单例）
# ============================================================

_profile_service: ProfileService | None = None


def get_profile_service(db: Session | None = None) -> ProfileService:
    """用**当前请求的 Session** 构造档案服务，绝不缓存持会话的实例。

    事故复盘（2026-10-29）：这里曾把服务缓存在模块级单例里，其内部 Session 长期不关闭，
    连接从不归还连接池 → 绕过 ``pool_pre_ping`` → 被 MySQL ``wait_timeout``（8h）断开后
    首次写入报 ``Lost connection``；失败事务又永久留在单例上，之后所有建档请求都抛
    ``PendingRollbackError`` 500。改为请求级后，连接随请求关闭，探活照常生效。
    """
    if _profile_service is not None:  # 测试注入优先
        return _profile_service
    if db is None:
        raise RuntimeError("get_profile_service 需要请求级 Session")
    from app.services.profile import SqlProfileStore

    return ProfileService(SqlProfileStore(db))


def set_profile_service(service: ProfileService | None) -> None:
    """测试注入入口。"""
    global _profile_service
    _profile_service = service


def build_in_memory_service() -> ProfileService:
    """离线构造（测试 / MOCK 模式）。"""
    return ProfileService(InMemoryProfileStore())


# ============================================================
# POST /api/profile —— 建档
# ============================================================


@router.post("/profile", response_model=ProfileOut, summary="画像测评建档（脱敏落库，回执只含区间）")
async def create_profile(
    req: ProfileCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProfileOut:
    service = get_profile_service(db)
    account = get_current_account(request, db, required=False)
    view = service.create_profile(
        ProfileInput(
            province=req.province,
            current_year=req.current_year,
            subject_type=req.subject_type,
            elective_subjects=req.elective_subjects,
            score=req.score,
            rank=req.rank,
            preferred_cities=req.preferred_cities,
            preferred_majors=req.preferred_majors,
            preferred_types=req.preferred_types,
            anon_id=req.anon_id,
            owner_username=account.username if account else None,
        )
    )
    # 日志只出区间（mask_for_log 语义一致，防明文泄漏）
    logger.info(
        "档案已建立 id=%s 省份=%s 科类=%s 分数区间=%s 位次区间=%s",
        view.id, view.province, view.subject_type,
        view.score_bucket or "未知", view.rank_bucket or "未知",
    )
    return ProfileOut(**view.to_dict())


@router.get("/profile/mine", summary="当前登录账号的最近档案（不含明文分数/位次）")
async def get_my_profile(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    view = get_profile_service(db).get_by_owner(account.username)
    return {
        "profile": view.to_dict() if view else None,
    }


# ============================================================
# GET /api/rank —— 位次换算 + 近三年等位分
# ============================================================


@router.get("/rank", response_model=RankResponse, summary="分数→位次 + 近三年等位分（位次直填优先）")
async def query_rank(
    subject_type: str = Query(..., description="科类：物理 / 历史"),
    score: int | None = Query(None, ge=0, le=750, description="高考分数（与 rank 至少传一个）"),
    rank: int | None = Query(None, ge=1, description="考生位次（直填，优先于按分数换算）"),
    province: str = Query("广东", description="省份（当前数据仅广东）"),
    current_year: int = Query(settings.current_year, description="报考年份"),
) -> RankResponse:
    """位次口径（唯一实现 = T07 ScoreEngine）。

    - ``rank`` 直填优先（用户实测位次比换算准）；
    - 只给 ``score`` → 按当年一分一段表换算（``capped`` 表示取公开数据上限）；
    - 等位分覆盖 ``REF_YEARS``（近三年）；缺年 → 该年 score=null + note（不伪造）。
    """
    if rank is None and score is None:
        from app.core.exceptions import ValidationError

        raise ValidationError("score 与 rank 至少传一个", detail={"code": "RANK_INPUT_REQUIRED"})

    engine = get_score_engine()

    manual = rank is not None
    if manual:
        effective_rank = int(rank)  # type: ignore[arg-type]
        res = None
    else:
        res = engine.query_rank(current_year, province, subject_type, int(score))  # type: ignore[arg-type]
        effective_rank = res.rank

    equiv = _equiv_scores(engine, effective_rank, province, subject_type)

    return RankResponse(
        year=current_year,
        province=province,
        subject_type=subject_type,
        score=score if manual or res is None else res.score,
        rank=effective_rank,
        rank_source="manual" if manual else "computed",
        capped=bool(res and res.capped),
        note=res.note if res else "",
        equiv_scores=equiv,
        caliber_text="位次=同分累计人数（上限口径）；等位分按一分一段表线性插值；"
        "投档线≠录取线；官方未公布数据不推算。",
    )


def _equiv_scores(engine, rank: int, province: str, subject_type: str) -> list[EquivScoreOut]:
    """近三年等位分；**缺年显式标注**（score=null + note），整体不失败。"""
    out: list[EquivScoreOut] = []
    for year in settings.ref_year_list:
        try:
            r = engine.equiv_score(year, province, subject_type, rank)
            out.append(
                EquivScoreOut(
                    year=year,
                    score=round(r.score, 1),
                    capped=r.capped,
                    note=r.note,
                    low_score=r.low_score,
                    high_score=r.high_score,
                )
            )
        except DataMissingError:
            out.append(EquivScoreOut(year=year, score=None, note="该年数据待补齐，暂不换算"))
    return out
