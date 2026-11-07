"""0007_latency_metrics：端到端延迟打点表

Revision ID: 0007_latency_metrics
Revises: 0006_conversation_messages
Create Date: 2026-11-07

设计依据：《Day_plan/Day5_3D康伴数字人.md》§4.2（大纲课时 5.2 + POC-08 Day5 部分，D3 决策）。
- 每轮回复 1 行：各阶段耗时 + 熔断占位标记 + 成功标记，POC-08 用 SQL percentile_cont
  复算 P50/P95 延迟基线。
- 仅新增表，不回改 0001-0006（ADR-013）；字段全部可空（文字轮无 ASR、降级轮无 LLM）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_latency_metrics"
down_revision: Union[str, None] = "0006_conversation_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "latency_metrics",
        sa.Column(
            "turn_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.thread_id"),
            nullable=True,
        ),
        sa.Column("device_id", UUID(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(16), nullable=True),
        sa.Column("asr_wait_ms", sa.Float(), nullable=True),
        sa.Column("llm_first_ms", sa.Float(), nullable=True),
        sa.Column("llm_total_ms", sa.Float(), nullable=True),
        sa.Column("tts_first_ms", sa.Float(), nullable=True),
        sa.Column("e2e_ms", sa.Float(), nullable=True),
        sa.Column("placeholder_used", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
    )
    # POC-08 跑批按时间窗聚合 P50/P95
    op.create_index(
        "ix_latency_metrics_created", "latency_metrics", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_latency_metrics_created", table_name="latency_metrics")
    op.drop_table("latency_metrics")
