"""workorders ORM：护工工单，必属一个 elder。

无 staff 表，assignee_ref 暂存外部引用，不伪造外键。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import WorkorderPriority, WorkorderStatus


class Workorder(TimestampMixin, Base):
    __tablename__ = "workorders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}')".format(
                WorkorderStatus.OPEN,
                WorkorderStatus.ACKNOWLEDGED,
                WorkorderStatus.IN_PROGRESS,
                WorkorderStatus.RESOLVED,
                WorkorderStatus.CANCELLED,
            ),
            name="ck_workorders_status",
        ),
        CheckConstraint(
            "priority IN ('{0}', '{1}', '{2}', '{3}')".format(
                WorkorderPriority.LOW,
                WorkorderPriority.NORMAL,
                WorkorderPriority.HIGH,
                WorkorderPriority.URGENT,
            ),
            name="ck_workorders_priority",
        ),
        Index("ix_workorders_status_created_at", "status", "created_at"),
        Index("ix_workorders_elder_status", "elder_id", "status"),
    )

    workorder_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 类目（0009 迁移）：sensitive_alert 工单与 safety 词表六类对齐；报修等=general。
    # abuse（虐待指控）仅 admin+family 可见（Day6+ 可见性矩阵，排除 nurse/doctor）。
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="general", default="general"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=WorkorderStatus.OPEN,
        server_default=WorkorderStatus.OPEN,
    )
    priority: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkorderPriority.NORMAL,
        server_default=WorkorderPriority.NORMAL,
    )
    assignee_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 解决方案（0010 迁移）：resolve 时与原因说明（note，拼入 description）分离的结构化落地，
    # 家属端据此展示「医生/护工怎么处理的」。
    solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
