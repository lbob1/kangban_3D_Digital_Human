"""0008_router_logs：三级路由决策留痕表（decision_logs）

Revision ID: 0008_router_logs
Revises: 0007_latency_metrics
Create Date: 2026-11-09

设计依据：《Day_plan/Day6_3D康伴数字人.md》§4.1/§4.2（大纲 6.3，D4/D7 决策）。
- 每轮 utterance 无论走哪层路由都落 1 行：route_layer/intent/confidence/
  latency_ms/model_version/tool_calls，与 latency_metrics 打点对齐；
- 「说『停』零模型」验收证据：route_layer=rule + model_version=null + latency<5ms；
- 敏感类别命中记入 sensitive_hit（Text，逗号分隔）；S1 不可用/超时降级记
  degraded=true（可见降级，不静默）；
- input_text 存脱敏后文本（身份证/剂量类模式打码，脱敏在服务层完成）；
- 敏感事件工单复用既有 workorders 表（type=sensitive_alert，ER 命名映射
  work_orders→workorders，用户 2026-11-09 裁决），本迁移不新建工单表；
- 仅新增表，不回改 0001-0007（ADR-013）；downgrade 对称。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_router_logs"
down_revision: Union[str, None] = "0007_latency_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "decision_logs",
        sa.Column(
            "decision_id",
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
        sa.Column(
            "elder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id"),
            nullable=True,
        ),
        # rule | safety | system_one | agent（级联命中层；safety=敏感一票否决短路）
        sa.Column("route_layer", sa.String(16), nullable=False),
        sa.Column("intent", sa.String(24), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("model_version", sa.String(120), nullable=True),
        sa.Column("tool_calls", sa.Integer(), nullable=True),
        sa.Column("escalated_to_agent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sensitive_hit", sa.Text(), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
    )
    # 分层占比/闭环率按时间窗聚合；POC-08 复测按 route_layer 过滤
    op.create_index("ix_decision_logs_created", "decision_logs", ["created_at"])
    op.create_index(
        "ix_decision_logs_route_created", "decision_logs", ["route_layer", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_decision_logs_route_created", table_name="decision_logs")
    op.drop_index("ix_decision_logs_created", table_name="decision_logs")
    op.drop_table("decision_logs")
