"""workorder_reads ORM：敏感工单按账号独立已读记录（Day6+ 批6）。

每人各记一条（唯一约束），可审计「谁在何时看过」；家属已读独立于护工/医生。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class WorkorderRead(Base):
    __tablename__ = "workorder_reads"
    __table_args__ = (
        UniqueConstraint("workorder_id", "account_id", name="uq_workorder_reads_wo_account"),
    )

    read_id: Mapped[uuid.UUID] = pk_uuid()
    workorder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workorders.workorder_id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
