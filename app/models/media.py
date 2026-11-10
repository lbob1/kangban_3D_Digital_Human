"""media ORM：媒体目录（Day1）+ 播放留痕（Day7 0014）。

Day1 只建结构，不导入任何媒体；CHECK 保证仅 licensed/teaching/public_domain 可进入 active。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, pk_uuid
from app.models.enums import (
    MediaCopyrightStatus,
    MediaPlaySource,
    MediaStatus,
    MediaType,
)


class MediaLibrary(TimestampMixin, Base):
    __tablename__ = "media_library"
    __table_args__ = (
        CheckConstraint(
            "type IN ('{0}', '{1}')".format(MediaType.OPERA, MediaType.MOVIE),
            name="ck_media_library_type",
        ),
        CheckConstraint(
            "copyright_status IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}')".format(
                MediaCopyrightStatus.UNKNOWN,
                MediaCopyrightStatus.LICENSED,
                MediaCopyrightStatus.TEACHING,
                MediaCopyrightStatus.PUBLIC_DOMAIN,
                MediaCopyrightStatus.RESTRICTED,
                MediaCopyrightStatus.EXPIRED,
            ),
            name="ck_media_library_copyright_status",
        ),
        CheckConstraint(
            "status IN ('{0}', '{1}')".format(MediaStatus.ACTIVE, MediaStatus.HIDDEN),
            name="ck_media_library_status",
        ),
        CheckConstraint(
            "status <> '{0}' OR copyright_status IN ('{1}', '{2}', '{3}')".format(
                MediaStatus.ACTIVE,
                MediaCopyrightStatus.LICENSED,
                MediaCopyrightStatus.TEACHING,
                MediaCopyrightStatus.PUBLIC_DOMAIN,
            ),
            name="ck_media_library_active_requires_licensed",
        ),
        Index("ix_media_library_type_status", "type", "status"),
        Index("ix_media_library_title", "title"),
    )

    media_id: Mapped[uuid.UUID] = pk_uuid()
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(80), nullable=True)
    era: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 未授权媒体不填地址；media_uri 可空
    media_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    copyright_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MediaCopyrightStatus.UNKNOWN,
        server_default=MediaCopyrightStatus.UNKNOWN,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MediaStatus.HIDDEN, server_default=MediaStatus.HIDDEN
    )


class MediaHistory(CreatedAtMixin, Base):
    """media_history：娱乐点播播放留痕（Day7 0014，US-06）。

    隐私同对话口径：仅本人 + 授权医护可见；source 记录触发来源（语音规则/UI）。
    """

    __tablename__ = "media_history"
    __table_args__ = (
        CheckConstraint(
            "source IN ('{0}', '{1}')".format(MediaPlaySource.VOICE_RULE, MediaPlaySource.UI),
            name="ck_media_history_source",
        ),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="ck_media_history_duration"),
        Index("ix_media_history_elder_played", "elder_id", "played_at"),
        Index("ix_media_history_media", "media_id"),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    media_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_library.media_id"), nullable=False
    )
    played_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)

