"""decision_logs ORM：三级路由决策留痕（Day6 课时 6.3，大纲 §4.1）。

每轮 utterance 1 行：级联命中层（safety/rule/system_one/agent）、意图与置信度、
本层延迟、模型版本、工具调用数、升级/降级标记；「说『停』零模型」验收证据
= route_layer=rule + model_version=null + latency<5ms。
敏感事件工单复用既有 workorders 表（type=sensitive_alert，用户 2026-11-09 裁决）。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class DecisionLog(Base):
    __tablename__ = "decision_logs"

    decision_id: Mapped[uuid.UUID] = pk_uuid()
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.thread_id"), nullable=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=True
    )
    route_layer: Mapped[str] = mapped_column(String(16), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(24), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalated_to_agent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    sensitive_hit: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
