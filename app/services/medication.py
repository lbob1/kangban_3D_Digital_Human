"""用药计划与事件业务：录入校验 + 查询（Day7 步5）。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Elder, MedicationEvent, MedicationPlan
from app.models.enums import ElderStatus, MedicationPlanStatus
from app.schemas.medication import MedicationPlanCreate, MedicationPlanUpdate


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


def _validate_schedule_and_dates(
    *, schedule: dict | None, start_date, end_date
) -> None:
    if schedule is not None and (not isinstance(schedule, dict) or not schedule):
        raise PlanValidationError("schedule 必须为非空 JSON 对象")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise PlanValidationError("end_date 不得早于 start_date")


async def get_plan(session: AsyncSession, plan_id: UUID) -> MedicationPlan | None:
    return await session.get(MedicationPlan, plan_id)


async def update_plan(
    session: AsyncSession, plan: MedicationPlan, data: MedicationPlanUpdate
) -> MedicationPlan:
    """按字段增量修改用药参数（doctor/admin）；status 可流转（如暂停/恢复/停用）。"""
    _validate_schedule_and_dates(
        schedule=data.schedule,
        start_date=data.start_date or plan.start_date,
        end_date=data.end_date if data.end_date is not None else plan.end_date,
    )
    if data.status is not None:
        try:
            MedicationPlanStatus(data.status)
        except ValueError as exc:
            raise PlanValidationError(f"非法状态：{data.status}") from exc
        plan.status = data.status
    for field in ("drug_name", "dose", "schedule", "instructions", "start_date", "end_date"):
        value = getattr(data, field)
        if value is not None:
            setattr(plan, field, value)
    await session.flush()
    # onupdate 的 updated_at 不会经 UPDATE RETURNING 自动回读，异步下惰性加载
    # 会触发 MissingGreenlet——显式 refresh 后再序列化
    await session.refresh(plan)
    return plan


async def cancel_plan(session: AsyncSession, plan: MedicationPlan) -> MedicationPlan:
    """软删：置 cancelled 保留证据链（用药事件不可物理回溯），幂等。"""
    plan.status = MedicationPlanStatus.CANCELLED
    await session.flush()
    await session.refresh(plan)
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
