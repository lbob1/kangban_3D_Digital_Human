"""0014_medication_media：medication_events + media_history 两表（Day7 用药/娱乐链路）

Revision ID: 0014_medication_media
Revises: 0013_memory_importance
Create Date: 2026-11-10

设计依据：《Day_plan/Day7_3D康伴数字人.md》§5.3（大纲 7.2/7.3，ER §3.6/§3.7「后续迁移」清单销项）。
- medication_events：用药确认图落库（POC-06 证据链），每次触发 1 行，
  状态随 LangGraph 图流转（pending→calling→waiting_confirm→confirmed/escalated），
  elder_id 冗余便于查询隔离；escalated 时 workorder_id 关联漏服工单
  （workorders category=medication，复用批 5/6 前端机制，D6 决策）；
- media_history：娱乐点播播放留痕（US-06），隐私同对话口径（本人+授权医护）；
- 两表均为新建，不回改 0001-0013（ADR-013）；downgrade 对称。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_medication_media"
down_revision: Union[str, None] = "0013_memory_importance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "medication_events",
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medication_plans.plan_id"),
            nullable=False,
        ),
        sa.Column(
            "elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        # 与 LangGraph 图节点对齐：pending/calling/waiting_confirm/confirmed/escalated/cancelled
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("thread_id", sa.String(80), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "workorder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workorders.workorder_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "status IN ('pending', 'calling', 'waiting_confirm', 'confirmed', "
            "'escalated', 'cancelled')",
            name="ck_medication_events_status",
        ),
        sa.CheckConstraint("source IN ('scheduler', 'dialog')", name="ck_medication_events_source"),
        sa.CheckConstraint("retry_count BETWEEN 0 AND 3", name="ck_medication_events_retry"),
    )
    op.create_index(
        "ix_medication_events_elder_sched", "medication_events", ["elder_id", "scheduled_at"]
    )
    op.create_index(
        "ix_medication_events_plan_sched", "medication_events", ["plan_id", "scheduled_at"]
    )
    op.create_index("ix_medication_events_status", "medication_events", ["status"])

    op.create_table(
        "media_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID),
        sa.Column(
            "elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False
        ),
        sa.Column(
            "media_id",
            UUID(as_uuid=True),
            sa.ForeignKey("media_library.media_id"),
            nullable=False,
        ),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint("source IN ('voice_rule', 'ui')", name="ck_media_history_source"),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_media_history_duration",
        ),
    )
    op.create_index("ix_media_history_elder_played", "media_history", ["elder_id", "played_at"])
    op.create_index("ix_media_history_media", "media_history", ["media_id"])


def downgrade() -> None:
    op.drop_index("ix_media_history_media", table_name="media_history")
    op.drop_index("ix_media_history_elder_played", table_name="media_history")
    op.drop_table("media_history")
    op.drop_index("ix_medication_events_status", table_name="medication_events")
    op.drop_index("ix_medication_events_plan_sched", table_name="medication_events")
    op.drop_index("ix_medication_events_elder_sched", table_name="medication_events")
    op.drop_table("medication_events")
