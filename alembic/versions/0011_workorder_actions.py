"""0011_workorder_actions：敏感工单处理记录表（Day6+ 批7）

Revision ID: 0011_workorder_actions
Revises: 0010_workorder_solution
Create Date: 2026-11-10

设计依据（用户 2026-11-10 裁决）：
- resolved 后另一角色（如医生）仍可补充/完善解决方案，追加而非覆盖，
  保留全部处理痕迹；详情据此展示时间线与「护工+医生 共同处理」徽标；
- 处理弹窗支持「请医生/护士协助」勾选，选择后进入两人共同处理流程；
- role/actor_name 为处理时刻快照；downgrade 对称；只做新增，不改既有结构。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_workorder_actions"
down_revision: Union[str, None] = "0010_workorder_solution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workorder_actions",
        sa.Column(
            "action_id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "workorder_id",
            sa.Uuid(),
            sa.ForeignKey("workorders.workorder_id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.account_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("actor_name", sa.String(80), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("solution", sa.Text(), nullable=False),
        sa.Column(
            "assist_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("action_id"),
    )
    op.create_index(
        "ix_workorder_actions_wo_created",
        "workorder_actions",
        ["workorder_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workorder_actions_wo_created", table_name="workorder_actions")
    op.drop_table("workorder_actions")
