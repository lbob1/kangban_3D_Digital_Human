"""护工看板 workqueue 契约（Day8 步6）：四类工单聚合视图与 HITL 操作。

四类映射（主文档 §4.4）：medication_missed（用药漏服）/ sensitive_alert（敏感事件，
nurse 排除 abuse 类目）/ health_alert（健康告警，接健康图 interrupt）/ help_request（求助）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# workqueue 聚合的四类工单 type
QUEUE_TYPES = ("medication_missed", "sensitive_alert", "health_alert", "help_request")


class WorkqueueItem(BaseModel):
    """workqueue 单条目；health_alert 额外带图挂起站点（前端据此渲染操作按钮）。"""

    model_config = ConfigDict(from_attributes=True)

    workorder_id: UUID
    elder_id: UUID
    elder_name: str
    type: str
    category: str = "general"
    status: str
    priority: str
    title: str
    description: str | None = None
    solution: str | None = None
    # 健康图挂起站点：nurse_confirm（护工可 resolve/escalate）/
    # doctor_confirm（仅医生可确认）；非 health_alert 或已流转为 None
    pending_site: str | None = None
    created_at: datetime


class WorkqueueResolve(BaseModel):
    """护工确认处理：原因说明与解决方案必填（与敏感事件 resolve 同口径）。"""

    note: str = Field(min_length=1, description="原因说明（审计留痕）")
    solution: str = Field(min_length=1, description="解决方案")


class WorkqueueEscalate(BaseModel):
    """护工升级（仅 health_alert）：resume escalate → 工单升 urgent 进医生确认。"""

    note: str | None = Field(default=None, description="升级说明（可选，写入图决策留痕）")


class DoctorConfirmBody(BaseModel):
    """医生确认（health_alert doctor_confirm 站点）：确认即放行通知与 mock 转诊。"""

    note: str | None = Field(default=None, description="医生意见（可选）")
