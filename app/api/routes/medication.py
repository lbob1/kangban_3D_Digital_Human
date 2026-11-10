"""用药计划/事件路由（Day7 步5）：计划录入（admin）、列表（含家属关联只读）、事件查询。

权限口径（Day7 §7）：计划录入仅 admin（管理端无页面，走 CLI/curl）；
列表 admin/护工/医生全量、家属仅关联长者（family_elder_links）只读；
事件查询不含家属（护工/医生处理漏服闭环）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.models import FamilyElderLink
from app.models.enums import AccountRole
from app.schemas.medication import (
    MedicationEventResponse,
    MedicationPlanCreate,
    MedicationPlanResponse,
)
from app.services import medication as medication_service

router = APIRouter(prefix="/medication", tags=["medication"])

_STAFF_ROLES = (AccountRole.ADMIN, AccountRole.NURSE, AccountRole.DOCTOR)
_VIEWER_ROLES = (*_STAFF_ROLES, AccountRole.FAMILY)


def _role_str(actor: UserIdentity) -> str:
    return actor.role.value if hasattr(actor.role, "value") else str(actor.role)


async def _family_linked_elder_ids(
    session: AsyncSession, account_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await session.execute(
        select(FamilyElderLink.elder_id).where(FamilyElderLink.account_id == account_id)
    )
    return [r[0] for r in rows.all()]


@router.post("/plans", response_model=MedicationPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan_route(
    body: MedicationPlanCreate,
    actor: UserIdentity = Depends(require_role(AccountRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MedicationPlanResponse:
    """录入用药计划（仅 admin）。"""
    try:
        plan = await medication_service.create_plan(session, body)
    except medication_service.ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except medication_service.PlanValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return MedicationPlanResponse.model_validate(plan)


@router.get("/plans", response_model=list[MedicationPlanResponse])
async def list_plans_route(
    actor: UserIdentity = Depends(require_role(*_VIEWER_ROLES)),
    elder_id: uuid.UUID | None = Query(default=None),
    plan_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[MedicationPlanResponse]:
    """用药计划列表：家属仅关联长者（关联只读）。"""
    elder_ids: list[uuid.UUID] | None = None
    if _role_str(actor) == AccountRole.FAMILY:
        linked = await _family_linked_elder_ids(session, actor.account_id)
        if elder_id is not None:
            elder_ids = [elder_id] if elder_id in linked else []
        else:
            elder_ids = linked or [uuid.uuid4()]  # 无关联→空集
    elif elder_id is not None:
        elder_ids = [elder_id]

    plans = await medication_service.list_plans(
        session, elder_ids=elder_ids, status=plan_status, limit=limit
    )
    return [MedicationPlanResponse.model_validate(p) for p in plans]


@router.get("/events", response_model=list[MedicationEventResponse])
async def list_events_route(
    actor: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    elder_id: uuid.UUID | None = Query(default=None),
    plan_id: uuid.UUID | None = Query(default=None),
    event_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[MedicationEventResponse]:
    """用药事件列表（漏服证据链，POC-06）：status/elder/plan 过滤。"""
    events = await medication_service.list_events(
        session, elder_id=elder_id, plan_id=plan_id, status=event_status, limit=limit
    )
    return [MedicationEventResponse.model_validate(e) for e in events]
