"""0009_workorder_category：workorders 敏感工单类目列（Day6+ 分级可见性矩阵）

Revision ID: 0009_workorder_category
Revises: 0008_router_logs
Create Date: 2026-11-09

设计依据（用户 2026-11-09 Day6+ 裁决）：
- 紧急工单可见性矩阵：除虐待（abuse）外全角色可见；
- abuse（虐待指控）仅 admin+family 可见（排除 nurse/doctor——施害者可能是
  工作人员本身，工单不能回流到疑似施害者的工作台）；
- category 取值与 app/router/safety.py SENSITIVE_WORDS 六类对齐
  （acute_symptom/fall/lost/emotional_crisis/emergency_call/abuse），
  非敏感工单（报修等）为 general；
- 仅新增列（server_default='general'，历史行自动归位），不改既有列，
  不回改 0001-0008（ADR-013）；downgrade 对称。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_workorder_category"
down_revision: Union[str, None] = "0008_router_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workorders",
        sa.Column("category", sa.String(32), nullable=False, server_default="general"),
    )
    # 敏感事件按类目过滤（abuse 排除矩阵 + 前端类目筛选）
    op.create_index("ix_workorders_category", "workorders", ["category"])


def downgrade() -> None:
    op.drop_index("ix_workorders_category", table_name="workorders")
    op.drop_column("workorders", "category")
