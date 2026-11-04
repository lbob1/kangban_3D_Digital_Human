"""设备绑定与自查请求/响应结构。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BindingUpdate(BaseModel):
    """设备绑定：房间必填，bed_no 省略表示房间级设备。"""

    room_id: uuid.UUID
    bed_no: int | None = Field(default=None, ge=1, le=2)


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: uuid.UUID
    device_code: str
    room_id: uuid.UUID | None
    bed_no: int | None
    status: str


class RegistrationTokenResponse(BaseModel):
    """一次性注册凭证：明文仅此一次返回。"""

    device_id: uuid.UUID
    registration_token: str
    expires_at: datetime


class ElderCandidate(BaseModel):
    elder_id: uuid.UUID
    name: str
    bed_no: int


class DeviceSelfResponse(BaseModel):
    """devices/me 自查：单人间返回 elder（锁定），双人间返回 candidates。"""

    device_id: uuid.UUID
    device_code: str
    room_id: uuid.UUID
    building_code: str
    room_number: str
    bed_count: int
    mode: str  # single / double
    elder: ElderCandidate | None = None
    candidates: list[ElderCandidate] = Field(default_factory=list)
