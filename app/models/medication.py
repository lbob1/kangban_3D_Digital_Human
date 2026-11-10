"""medication ORM：用药计划（Day1）+ 用药确认事件（Day7 0014）。"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import (
    MedicationEventSource,
    MedicationEventStatus,
    MedicationPlanStatus,
)


class MedicationPlan(TimestampMixin, Base):
    __tablename__ = "medication_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}')".format(
                MedicationPlanStatus.PLANNED,
                MedicationPlanStatus.ACTIVE,
                MedicationPlanStatus.PAUSED,
                MedicationPlanStatus.COMPLETED,
                MedicationPlanStatus.CANCELLED,
            ),
            name="ck_medication_plans_status",
        ),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_medication_plans_date_range",
        ),
        Index("ix_medication_plans_elder_status", "elder_id", "status"),
    )

    plan_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    drug_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dose: Mapped[str] = mapped_column(String(100), nullable=False)
    schedule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MedicationPlanStatus.PLANNED,
        server_default=MedicationPlanStatus.PLANNED,
    )


class MedicationEvent(TimestampMixin, Base):
    """medication_events：用药确认图落库（Day7 0014，POC-06 证据链）。

    用药图每次触发写 1 行，状态随图流转更新（pending→calling→waiting_confirm→
    confirmed/escalated）；elder_id 冗余便于按长者查询隔离；escalated 时关联漏服工单。
    """

    __tablename__ = "medication_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}')".format(
                MedicationEventStatus.PENDING,
                MedicationEventStatus.CALLING,
                MedicationEventStatus.WAITING_CONFIRM,
                MedicationEventStatus.CONFIRMED,
                MedicationEventStatus.ESCALATED,
                MedicationEventStatus.CANCELLED,
            ),
            name="ck_medication_events_status",
        ),
        CheckConstraint(
            "source IN ('{0}', '{1}')".format(
                MedicationEventSource.SCHEDULER, MedicationEventSource.DIALOG
            ),
            name="ck_medication_events_source",
        ),
        CheckConstraint("retry_count BETWEEN 0 AND 3", name="ck_medication_events_retry"),
        Index("ix_medication_events_elder_sched", "elder_id", "scheduled_at"),
        Index("ix_medication_events_plan_sched", "plan_id", "scheduled_at"),
        Index("ix_medication_events_status", "status"),
    )

    event_id: Mapped[uuid.UUID] = pk_uuid()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_plans.plan_id"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=MedicationEventStatus.PENDING,
        server_default=MedicationEventStatus.PENDING,
    )
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # 图 checkpointer thread（med-{plan_id}-{date}-{slot}），可空
    thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    workorder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workorders.workorder_id"), nullable=True
    )

