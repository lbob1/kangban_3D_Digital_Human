"""媒体目录与播放留痕业务（Day7 步5）。

版权边界：列表与上报只接受 licensed/teaching/public_domain 且 active 的素材
（DB CHECK 兜底，服务层同口径过滤）。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Elder, MediaHistory, MediaLibrary, MemoryChunk
from app.models.enums import (
    ElderStatus,
    MediaCopyrightStatus,
    MediaPlaySource,
    MediaStatus,
    MemoryCategory,
    MemoryStatus,
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


# ---- Day7 步8：对话点播（voice_rule 直达，偏好优先确定性选曲）----

# 长者偏好记忆 → 媒体类型确定性映射（零模型零 token；demo 覆盖越剧/京剧/电影）
_PREF_KEYWORDS: dict[str, str] = {
    "越剧": "opera",
    "京剧": "opera",
    "戏曲": "opera",
    "电影": "movie",
}


async def preference_keywords(session: AsyncSession, elder_id: UUID) -> list[str]:
    """从 active preference 记忆中提取已映射的偏好关键词（如「越剧」）。"""
    rows = await session.execute(
        select(MemoryChunk.content).where(
            MemoryChunk.elder_id == elder_id,
            MemoryChunk.category == MemoryCategory.PREFERENCE,
            MemoryChunk.status == MemoryStatus.ACTIVE,
        )
    )
    contents = [r[0] or "" for r in rows.all()]
    return [kw for kw in _PREF_KEYWORDS if any(kw in c for c in contents)]


async def pick_for_elder(
    session: AsyncSession,
    *,
    elder_id: UUID,
    media_type: str | None = None,
    exclude_ids: list[UUID] | None = None,
) -> MediaLibrary | None:
    """点播选曲：active+授权候选内，偏好记忆命中优先，其次最新上架（确定性）。"""
    candidates = await list_library(session, media_type=media_type, limit=200)
    if exclude_ids:
        candidates = [m for m in candidates if m.media_id not in set(exclude_ids)]
    if not candidates:
        return None
    keywords = await preference_keywords(session, elder_id)

    def score(m: MediaLibrary) -> tuple[int, datetime]:
        text = "".join(filter(None, [m.genre, m.era, m.artist, m.title]))
        hit = any(kw in text for kw in keywords)
        return (1 if hit else 0, m.created_at)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


async def record_voice_play(
    session: AsyncSession, *, elder_id: UUID, media: MediaLibrary
) -> MediaHistory:
    """voice_rule 点播留痕（时长未知→None；UI 点击走 REST /media/history）。"""
    history = MediaHistory(
        elder_id=elder_id,
        media_id=media.media_id,
        played_at=datetime.now(UTC),
        duration_seconds=None,
        source=MediaPlaySource.VOICE_RULE,
    )
    session.add(history)
    await session.flush()
    return history


async def latest_history(session: AsyncSession, elder_id: UUID) -> MediaHistory | None:
    """最近一条播放留痕（「换一段」排除当前曲目 / 判断点播类型）。"""
    return (
        await session.execute(
            select(MediaHistory)
            .where(MediaHistory.elder_id == elder_id)
            .order_by(MediaHistory.played_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
