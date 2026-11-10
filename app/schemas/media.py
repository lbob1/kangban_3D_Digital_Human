"""媒体目录/播放上报请求响应结构（Day7 步5）。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MediaPlaySource


class MediaItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    media_id: UUID
    type: str
    title: str
    genre: str | None
    era: str | None
    artist: str | None
    media_uri: str | None
    duration_seconds: int | None
    copyright_status: str


class MediaPlayReport(BaseModel):
    """播放上报：elder_id 由服务端经设备绑定解析，请求体不收身份字段（安全红线 8）。"""

    media_id: UUID
    duration_seconds: int | None = Field(default=None, ge=0)
    source: MediaPlaySource


class MediaHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    elder_id: UUID
    media_id: UUID
    played_at: datetime
    duration_seconds: int | None
    source: str
