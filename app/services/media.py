"""媒体目录与播放留痕业务（Day7 步5）。

版权边界：列表与上报只接受 licensed/teaching/public_domain 且 active 的素材
（DB CHECK 兜底，服务层同口径过滤）。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Elder, MediaHistory, MediaLibrary
from app.models.enums import (
    ElderStatus,
    MediaCopyrightStatus,
    MediaPlaySource,
    MediaStatus,
)
from app.schemas.media import MediaPlayReport

_PLAYABLE_COPYRIGHT = (
    MediaCopyrightStatus.LICENSED,
    MediaCopyrightStatus.TEACHING,
    MediaCopyrightStatus.PUBLIC_DOMAIN,
)


class MediaNotFound(Exception):
    """媒体不存在；路由层映射 404。"""


class MediaInactive(Exception):
    """素材未上架或版权未授权；路由层映射 422。"""


class DeviceNotBound(Exception):
    """设备未绑定房间或房间无在住长者；路由层映射 404。"""


class ElderAmbiguous(Exception):
    """多床位房间无法唯一确定播放长者；路由层映射 409。"""


async def list_library(
    session: AsyncSession,
    *,
    media_type: str | None = None,
    genre: str | None = None,
    era: str | None = None,
    limit: int = 100,
) -> list[MediaLibrary]:
    stmt = (
        select(MediaLibrary)
        .where(
            MediaLibrary.status == MediaStatus.ACTIVE,
            MediaLibrary.copyright_status.in_(_PLAYABLE_COPYRIGHT),
        )
        .order_by(MediaLibrary.created_at.desc())
        .limit(limit)
    )
    if media_type is not None:
        stmt = stmt.where(MediaLibrary.type == media_type)
    if genre is not None:
        stmt = stmt.where(MediaLibrary.genre == genre)
    if era is not None:
        stmt = stmt.where(MediaLibrary.era == era)
    return list((await session.execute(stmt)).scalars().all())


async def resolve_device_elder(session: AsyncSession, device_id: UUID) -> Elder:
    """设备 → 房间 → 在住长者（服务端确定身份，安全红线 8）；单人间唯一。"""
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None or device.room_id is None:
        raise DeviceNotBound("设备未绑定房间")

    elders = list(
        (
            await session.execute(
                select(Elder).where(
                    Elder.room_id == device.room_id, Elder.status == ElderStatus.ACTIVE
                )
            )
        ).scalars().all()
    )
    if not elders:
        raise DeviceNotBound("房间无在住长者")
    if len(elders) > 1:
        raise ElderAmbiguous("多床位房间需会话上下文确定长者")
    return elders[0]


async def record_play(
    session: AsyncSession, *, device_id: UUID, data: MediaPlayReport
) -> MediaHistory:
    """播放上报落库：elder_id 服务端解析；素材必须 active 且授权。"""
    elder = await resolve_device_elder(session, device_id)
    media = (
        await session.execute(
            select(MediaLibrary).where(MediaLibrary.media_id == data.media_id)
        )
    ).scalar_one_or_none()
    if media is None:
        raise MediaNotFound("媒体不存在")
    if media.status != MediaStatus.ACTIVE or media.copyright_status not in _PLAYABLE_COPYRIGHT:
        raise MediaInactive("素材未上架或版权未授权")

    history = MediaHistory(
        elder_id=elder.elder_id,
        media_id=media.media_id,
        played_at=datetime.now(UTC),
        duration_seconds=data.duration_seconds,
        source=MediaPlaySource(data.source),
    )
    session.add(history)
    await session.flush()
    return history
