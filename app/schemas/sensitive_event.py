"""敏感事件 HITL 契约（Day6 P1-8）：workorders(type=sensitive_alert) 的视图。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SensitiveEventResponse(BaseModel):
    """敏感事件（复用护工工单表；status open→resolved 表达 pending→已处理）。"""

    model_config = ConfigDict(from_attributes=True)

    workorder_id: UUID
    elder_id: UUID
    status: str
    priority: str
    # 类目（0009 迁移）：acute_symptom/fall/lost/emotional_crisis/emergency_call/abuse；
    # abuse 仅 admin+family 可见（Day6+ 可见性矩阵）
    category: str = "general"
    title: str
    description: str | None = None
    # 解决方案（0010 迁移）：resolve 时填写，家属端据此展示处理方式
    solution: str | None = None
    # 批6：当前请求账号是否已读（按账号独立，workorder_reads 表推导）
    read_by_me: bool = False
    resolved_at: datetime | None = None
    created_at: datetime


class SensitiveEventResolve(BaseModel):
    """标记已处理（批6 开放 admin/nurse/doctor）：原因说明与解决方案均必填。"""

    note: str = Field(min_length=1, description="原因说明（追加到 description，审计留痕）")
    solution: str = Field(min_length=1, description="解决方案（独立列，家属端可见）")
