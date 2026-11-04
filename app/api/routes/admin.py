"""管理接口：三张授权关联表（护工-楼栋 / 负责医生-长者 / 家属-长者）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db.session import get_session
from app.models import BuildingAssignment, DoctorElderAssignment, FamilyElderLink
from app.models.enums import AssignmentStatus
from app.schemas.assignment import (
    BuildingAssignmentCreate,
    BuildingAssignmentResponse,
    DoctorAssignmentCreate,
    DoctorAssignmentResponse,
    FamilyLinkCreate,
    FamilyLinkResponse,
)
from app.services.authorization import (
    AssignmentConflict,
    AssignmentValidationError,
    ObjectInactive,
    ObjectNotFound,
    create_building_assignment,
    create_doctor_assignment,
    create_family_link,
    suspend_assignment,
)

router = APIRouter(prefix="/admin/assignments", tags=["admin"])


def _map_service_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ObjectNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (AssignmentValidationError, ObjectInactive)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    if isinstance(exc, AssignmentConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="内部错误")


# ---- 护工-楼栋 ----


@router.post("/buildings", response_model=BuildingAssignmentResponse, status_code=201)
async def create_building(
    body: BuildingAssignmentCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> BuildingAssignmentResponse:
    try:
        row = await create_building_assignment(session, body)
    except (ObjectNotFound, AssignmentValidationError, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return BuildingAssignmentResponse.model_validate(row)


@router.get("/buildings", response_model=list[BuildingAssignmentResponse])
async def list_buildings(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    building_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[BuildingAssignmentResponse]:
    stmt = select(BuildingAssignment)
    if account_id is not None:
        stmt = stmt.where(BuildingAssignment.account_id == account_id)
    if building_status is not None:
        stmt = stmt.where(BuildingAssignment.status == building_status)
    rows = (await session.execute(stmt.order_by(BuildingAssignment.created_at))).scalars().all()
    return [BuildingAssignmentResponse.model_validate(r) for r in rows]


@router.delete("/buildings/{assignment_id}", status_code=204)
async def delete_building(
    assignment_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "building", assignment_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()


# ---- 负责医生-长者 ----


@router.post("/doctors", response_model=DoctorAssignmentResponse, status_code=201)
async def create_doctor(
    body: DoctorAssignmentCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DoctorAssignmentResponse:
    try:
        row = await create_doctor_assignment(session, body)
    except (ObjectNotFound, AssignmentValidationError, ObjectInactive, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return DoctorAssignmentResponse.model_validate(row)


@router.get("/doctors", response_model=list[DoctorAssignmentResponse])
async def list_doctors(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    elder_id: uuid.UUID | None = None,
    assignment_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[DoctorAssignmentResponse]:
    stmt = select(DoctorElderAssignment)
    if account_id is not None:
        stmt = stmt.where(DoctorElderAssignment.account_id == account_id)
    if elder_id is not None:
        stmt = stmt.where(DoctorElderAssignment.elder_id == elder_id)
    if assignment_status is not None:
        stmt = stmt.where(DoctorElderAssignment.status == assignment_status)
    rows = (await session.execute(stmt.order_by(DoctorElderAssignment.created_at))).scalars().all()
    return [DoctorAssignmentResponse.model_validate(r) for r in rows]


@router.delete("/doctors/{assignment_id}", status_code=204)
async def delete_doctor(
    assignment_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "doctor", assignment_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()


# ---- 家属-长者 ----


@router.post("/families", response_model=FamilyLinkResponse, status_code=201)
async def create_family(
    body: FamilyLinkCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> FamilyLinkResponse:
    try:
        row = await create_family_link(session, body)
    except (ObjectNotFound, AssignmentValidationError, ObjectInactive, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return FamilyLinkResponse.model_validate(row)


@router.get("/families", response_model=list[FamilyLinkResponse])
async def list_families(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    elder_id: uuid.UUID | None = None,
    link_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[FamilyLinkResponse]:
    stmt = select(FamilyElderLink)
    if account_id is not None:
        stmt = stmt.where(FamilyElderLink.account_id == account_id)
    if elder_id is not None:
        stmt = stmt.where(FamilyElderLink.elder_id == elder_id)
    if link_status is not None:
        stmt = stmt.where(FamilyElderLink.status == link_status)
    rows = (await session.execute(stmt.order_by(FamilyElderLink.created_at))).scalars().all()
    return [FamilyLinkResponse.model_validate(r) for r in rows]


@router.delete("/families/{link_id}", status_code=204)
async def delete_family(
    link_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "family", link_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()
