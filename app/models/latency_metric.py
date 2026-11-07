"""latency_metrics ORM：端到端延迟打点（Day5 课时 5.2 / POC-08 Day5 部分）。

每轮回复 1 行：记录各阶段耗时（ASR 终句等待 / LLM 首 token / LLM 总耗时 /
首句 TTS 首包 / 服务端口径端到端）与熔断占位标记，供 SQL percentile_cont
复算 P50/P95 延迟基线；熔断占位标记由 P1-4 接入。打点失败不阻断通话。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class LatencyMetric(Base):
    __tablename__ = "latency_metrics"

    turn_id: Mapped[uuid.UUID] = pk_uuid()
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.thread_id"), nullable=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    asr_wait_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_first_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_total_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    tts_first_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    e2e_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    placeholder_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
