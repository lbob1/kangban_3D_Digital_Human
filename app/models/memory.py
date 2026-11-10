"""memory_chunks ORM：长期记忆块，pgvector 向量列固定 1024 维（0012 迁移对齐）。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import MemoryStatus, MemoryVisibility


class MemoryChunk(TimestampMixin, Base):
    """长期记忆块：必属一个 elder；embedding 维度待向量模型确定后由后续迁移固定。"""

    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "category IN ('preference', 'health', 'event', 'relationship', 'routine', 'other')",
            name="ck_memory_chunks_category",
        ),
        CheckConstraint(
            "visibility IN ('private', 'care_team')",
            name="ck_memory_chunks_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'suppressed', 'deleted')",
            name="ck_memory_chunks_status",
        ),
        CheckConstraint(
            "importance BETWEEN 1 AND 5",
            name="ck_memory_chunks_importance",
        ),
        Index(
            "ix_memory_chunks_elder_status_category_created",
            "elder_id",
            "status",
            "category",
            "created_at",
        ),
        Index("ix_memory_chunks_elder_visibility", "elder_id", "visibility"),
    )

    chunk_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MemoryVisibility.PRIVATE,
        server_default=MemoryVisibility.PRIVATE,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemoryStatus.ACTIVE, server_default=MemoryStatus.ACTIVE
    )
    # Day7（0013）：萃取重要性 1-5（④降级 top2 排序 / ⑤遗忘衰减打分）
    importance: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=3, server_default="3"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
