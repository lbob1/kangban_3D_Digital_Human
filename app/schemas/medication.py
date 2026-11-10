"""用药计划/事件请求响应结构（Day7 步5）。"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MedicationPlanCreate(BaseModel):
    elder_id: UUID
    drug_name: str = Field(min_length=1, max_length=160)
    dose: str = Field(min_length=1, max_length=100)
    # JSONB 结构校验在服务层（Day7 步7 scheduler 读取 slots）
    schedule: dict
    instructions: str | None = None
    start_date: date
    end_date: date | None = None


class MedicationPlanUpdate(BaseModel):
    """用药计划修改（Day7+ 权限口径：doctor/admin 可写）——全字段可选，缺省不改动。"""

    drug_name: str | None = Field(default=None, min_length=1, max_length=160)
    dose: str | None = Field(default=None, min_length=1, max_length=100)
    schedule: dict | None = None
    instructions: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None


class MedicationPlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID
    elder_id: UUID
    drug_name: str
    dose: str
    schedule: dict
    instructions: str | None
    start_date: date
    end_date: date | None
    status: str
    created_at: datetime
    updated_at: datetime


class MedicationEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    plan_id: UUID
    elder_id: UUID
    scheduled_at: datetime
    status: str
    retry_count: int
    thread_id: str | None
    confirmed_at: datetime | None
    source: str
    workorder_id: UUID | None
    created_at: datetime
    updated_at: datetime
