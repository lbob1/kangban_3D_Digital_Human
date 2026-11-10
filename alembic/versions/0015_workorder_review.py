"""0015_workorder_review：敏感事件审核终态制（前端玻璃拟态升级 D3）

Revision ID: 0015_workorder_review
Revises: 0014_medication_media
Create Date: 2026-11-10

设计依据（用户 2026-11-10 拍板 D3 审核终态制）：
- workorders 加 4 列：review_status（pending/approved/rejected，可空——历史行与
  open 工单无审核态）、reviewed_by、reviewed_at、review_note；
- workorder_actions 加 action 列（resolve/supplement/review_approve/review_reject，
  可空——历史行 NULL 按普通处理记录渲染），审核留痕走时间线；
- workorders.status 既有 CHECK 枚举不动（0009/0010 加列先例），downgrade 对称。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_workorder_review"
down_revision: Union[str, None] = "0014_medication_media"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workorders",
        sa.Column("review_status", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        "ck_workorders_review_status",
        "workorders",
        "review_status IN ('pending', 'approved', 'rejected')",
    )
    op.add_column(
        "workorders",
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workorders",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("workorders", sa.Column("review_note", sa.Text(), nullable=True))
    # 审核留痕：时间线动作类型（历史行 NULL = resolve/supplement 普通处理记录）
    op.add_column(
        "workorder_actions",
        sa.Column("action", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workorder_actions", "action")
    op.drop_column("workorders", "review_note")
    op.drop_column("workorders", "reviewed_at")
    op.drop_column("workorders", "reviewed_by")
    op.drop_constraint("ck_workorders_review_status", "workorders", type_="check")
    op.drop_column("workorders", "review_status")
