"""workorder_actions ORM：敏感工单处理记录（Day6+ 批7 / 0011 迁移）。

一单可多次处理：resolve 写首条；resolved 后另一角色（或同角色）可补充，
追加不覆盖，保留全部处理痕迹；详情据此展示时间线与「护工+医生 共同处理」。
role 为账号角色快照（admin/nurse/doctor），不随账号后续变更回溯。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class WorkorderAction(Base):
    __tablename__ = "workorder_actions"
    __table_args__ = (
        Index("ix_workorder_actions_wo_created", "workorder_id", "created_at"),
    )

    action_id: Mapped[uuid.UUID] = pk_uuid()
    workorder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workorders.workorder_id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )
    # 角色快照：resolve/补充时刻的账号角色（admin/nurse/doctor）
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # 处理人姓名快照（display_name，账号改名不回溯）
    actor_name: Mapped[str] = mapped_column(String(80), nullable=False)
    # 原因说明（resolve 必填；补充可选）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 解决方案（resolve 必填；补充必填——「完善解决方案」；审核记录存审核意见）
    solution: Mapped[str] = mapped_column(Text, nullable=False)
    # 动作类型（0015 迁移）：resolve/supplement/review_approve/review_reject；
    # 历史行 NULL 按普通处理记录渲染
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 该处理人是否请求了另一角色协助（nurse→doctor / doctor→nurse）
    assist_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
