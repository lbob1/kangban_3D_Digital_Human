"""用药计划与事件业务：录入校验 + 查询（Day7 步5）。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Elder, MedicationEvent, MedicationPlan
from app.models.enums import ElderStatus, MedicationPlanStatus
from app.schemas.medication import MedicationPlanCreate


class ElderNotFound(Exception):
    """长者不存在或不在住；路由层映射 404。"""


class PlanValidationError(Exception):
    """计划业务校验失败；路由层映射 422。"""


async def create_plan(session: AsyncSession, data: MedicationPlanCreate) -> MedicationPlan:
    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == data.elder_id))
    ).scalar_one_or_none()
    if elder is None or elder.status != ElderStatus.ACTIVE:
        raise ElderNotFound("长者不存在或不在住")
    if data.end_date is not None and data.end_date < data.start_date:
        raise PlanValidationError("end_date 不得早于 start_date")
    if not isinstance(data.schedule, dict) or not data.schedule:
        raise PlanValidationError("schedule 必须为非空 JSON 对象")

    plan = MedicationPlan(
        elder_id=data.elder_id,
        drug_name=data.drug_name,
        dose=data.dose,
        schedule=data.schedule,
        instructions=data.instructions,
        start_date=data.start_date,
        end_date=data.end_date,
        status=MedicationPlanStatus.PLANNED,
    )
    session.add(plan)
    await session.flush()
    return plan


async def list_plans(
    session: AsyncSession,
    *,
    elder_ids: list[UUID] | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[MedicationPlan]:
    stmt = select(MedicationPlan).order_by(MedicationPlan.created_at.desc()).limit(limit)
    if elder_ids is not None:
        stmt = stmt.where(MedicationPlan.elder_id.in_(elder_ids))
    if status is not None:
        stmt = stmt.where(MedicationPlan.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def list_events(
    session: AsyncSession,
    *,
    elder_id: UUID | None = None,
    plan_id: UUID | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[MedicationEvent]:
    stmt = select(MedicationEvent).order_by(MedicationEvent.scheduled_at.desc()).limit(limit)
    if elder_id is not None:
        stmt = stmt.where(MedicationEvent.elder_id == elder_id)
    if plan_id is not None:
        stmt = stmt.where(MedicationEvent.plan_id == plan_id)
    if status is not None:
        stmt = stmt.where(MedicationEvent.status == status)
    return list((await session.execute(stmt)).scalars().all())
