"""敏感事件 HITL 契约（Day6 P1-8）：workorders(type=sensitive_alert) 的 admin 视图。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SensitiveEventResponse(BaseModel):
    """敏感事件（复用护工工单表；status open→resolved 表达 pending→已处理）。"""

    model_config = ConfigDict(from_attributes=True)

    workorder_id: UUID
    elder_id: UUID
    status: str
    priority: str
    title: str
    description: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class SensitiveEventResolve(BaseModel):
    """标记已处理：可选处理备注（追加到 description，审计留痕）。"""

    note: str | None = None
