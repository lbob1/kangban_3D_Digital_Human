"""0010_workorder_solution_and_reads：工单解决方案列 + 按账号已读表（Day6+ 批6）

Revision ID: 0010_workorder_solution
Revises: 0009_workorder_category
Create Date: 2026-11-09

设计依据（用户 2026-11-09 Day6+ 批6 裁决）：
- workorders.solution：resolve 时与原因说明（note，拼入 description）分离的
  结构化落地，家属端据此展示「医生/护工怎么处理的」；可空，历史行 NULL；
- workorder_reads：按账号独立已读记录（每账号各自看各自已读状态），
  (workorder_id, account_id) 唯一约束保证幂等；只做新增，不改既有结构
  （ADR-013），downgrade 对称。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_workorder_solution"
down_revision: Union[str, None] = "0009_workorder_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workorders", sa.Column("solution", sa.Text(), nullable=True))
    op.create_table(
        "workorder_reads",
        sa.Column(
            "read_id",
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
        sa.Column(
            "read_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("read_id"),
        sa.UniqueConstraint("workorder_id", "account_id", name="uq_workorder_reads_wo_account"),
    )


def downgrade() -> None:
    op.drop_table("workorder_reads")
    op.drop_column("workorders", "solution")
