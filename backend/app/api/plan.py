"""T26 · 志愿表 API（生成 / 列表 / 增删改 / 排序 / 容量 + 乐观锁）。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST   | `/api/plan` | 创建志愿表 |
| GET    | `/api/plan?profile_id=N` | 该档案的全部方案（多方案） |
| GET    | `/api/plan/{plan_id}` | 方案详情（含条目） |
| DELETE | `/api/plan/{plan_id}` | 删除方案 |
| POST   | `/api/plan/{plan_id}/items` | 加入院校专业组（容量 / 重复校验） |
| PATCH  | `/api/plan/{plan_id}/items/{item_id}` | 改档位/备注（system_tier 只读） |
| DELETE | `/api/plan/{plan_id}/items/{item_id}?version=N` | 删除条目（重排 seq） |
| POST   | `/api/plan/{plan_id}/reorder` | 排序持久化 |

红线：容量超额拦截；`system_tier` 不可被用户覆盖；所有变更走乐观锁校验；排序持久化。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_account
from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.plan import (
    ItemAddRequest,
    ItemUpdateRequest,
    PlanCreateRequest,
    PlanListResponse,
    PlanView,
    ReorderRequest,
)
from app.services.plan import get_plan_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/plan", tags=["plan"])


@router.post("", response_model=PlanView, summary="创建志愿表")
async def create_plan(
    request: Request,
    payload: PlanCreateRequest = Body(...),
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).create_plan(
        profile_id=payload.profile_id,
        name=payload.name,
        year=payload.year,
        owner_username=account.username,
    )


@router.get("", response_model=PlanListResponse, summary="档案下的全部志愿表（多方案）")
async def list_plans(
    request: Request,
    profile_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
) -> PlanListResponse:
    account = get_current_account(request, db, required=True)
    assert account is not None
    plans = get_plan_service(db).list_plans(profile_id, owner_username=account.username)
    return PlanListResponse(total=len(plans), plans=plans)


@router.get("/mine", response_model=PlanListResponse, summary="当前账号全部志愿表")
async def list_my_plans(
    request: Request,
    db: Session = Depends(get_db),
) -> PlanListResponse:
    account = get_current_account(request, db, required=True)
    assert account is not None
    plans = get_plan_service(db).list_owned_plans(account.username)
    return PlanListResponse(total=len(plans), plans=plans)


@router.get("/{plan_id}", response_model=PlanView, summary="志愿表详情")
async def get_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).get_plan(plan_id, owner_username=account.username)


@router.delete("/{plan_id}", summary="删除志愿表")
async def delete_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    account = get_current_account(request, db, required=True)
    assert account is not None
    get_plan_service(db).delete_plan(plan_id, owner_username=account.username)
    return {"ok": True, "deleted": plan_id}


@router.post("/{plan_id}/items", response_model=PlanView, summary="加入院校专业组")
async def add_item(
    plan_id: int,
    request: Request,
    payload: ItemAddRequest = Body(...),
    version: int = Query(..., ge=0),
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).add_item(
        plan_id=plan_id,
        version=version,
        university_code=payload.university_code,
        university_name=payload.university_name,
        group_code=payload.group_code,
        tier=payload.tier,
        notes=payload.notes,
        owner_username=account.username,
    )


@router.patch("/{plan_id}/items/{item_id}", response_model=PlanView, summary="改档位/备注（system_tier 只读）")
async def update_item(
    plan_id: int,
    item_id: int,
    request: Request,
    payload: ItemUpdateRequest = Body(...),
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).update_item(
        plan_id=plan_id, item_id=item_id, version=payload.version,
        tier=payload.tier, notes=payload.notes, owner_username=account.username,
    )


@router.delete("/{plan_id}/items/{item_id}", response_model=PlanView, summary="删除条目（重排顺序）")
async def delete_item(
    plan_id: int,
    item_id: int,
    request: Request,
    version: int = Query(..., ge=0),
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).delete_item(
        plan_id=plan_id,
        item_id=item_id,
        version=version,
        owner_username=account.username,
    )


@router.post("/{plan_id}/reorder", response_model=PlanView, summary="排序持久化")
async def reorder(
    plan_id: int,
    request: Request,
    payload: ReorderRequest = Body(...),
    db: Session = Depends(get_db),
) -> PlanView:
    account = get_current_account(request, db, required=True)
    assert account is not None
    return get_plan_service(db).reorder(
        plan_id=plan_id,
        item_ids=payload.item_ids,
        version=payload.version,
        owner_username=account.username,
    )
