"""0013_memory_importance：memory_chunks 增加 importance 列（Day7 记忆五步链路④⑤）

Revision ID: 0013_memory_importance
Revises: 0012_memory_embedding_dim
Create Date: 2026-11-10

设计依据（Day7 主文档 §4.1，2026-11-10 用户裁决方案 A）：
- 五步链路中「④LLM 不可用降级取 top2 最高重要性」「⑤importance 打分 + 90 天衰减」
  需要 importance 可持久化、可排序；Day1 建表时无该列（ER §3.5 亦未定义），
  萃取 JSON schema 输出的 importance 此前无落库位置——本迁移补齐；
- 仅加列（SMALLINT NOT NULL DEFAULT 3，历史/缺省行为中权重 3），
  不回改 0001-0012（ADR-013）；downgrade 对称（删约束 + 删列）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_memory_importance"
down_revision: Union[str, None] = "0012_memory_embedding_dim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_chunks",
        sa.Column(
            "importance",
            sa.SmallInteger(),
            nullable=False,
            server_default="3",
            comment="萃取重要性 1-5（④降级 top2 排序 / ⑤遗忘衰减打分）",
        ),
    )
    op.create_check_constraint(
        "ck_memory_chunks_importance",
        "memory_chunks",
        "importance BETWEEN 1 AND 5",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_chunks_importance", "memory_chunks", type_="check")
    op.drop_column("memory_chunks", "importance")
