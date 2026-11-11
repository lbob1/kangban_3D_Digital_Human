"""0017_health_triage：health_records 增加 triage_level / triage_reason 两列（Day8 健康分级）

Revision ID: 0017_health_triage
Revises: 0016_media_music_type
Create Date: 2026-11-11

设计依据（Day8 主文档 §5 / D7）：
- 健康分级结论需持久化供护工看板 / 医生趋势页读取；Day1 建表（0002）时无该两列；
- 只增不改（不新增 CHECK：分级写入方唯一=健康图，应用层校验；旧数据 NULL 兼容）；
- downgrade 对称（drop_column）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_health_triage"
down_revision: Union[str, None] = "0016_media_music_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "health_records",
        sa.Column(
            "triage_level",
            sa.String(10),
            nullable=True,
            comment="green/yellow/red；NULL=旧数据或未分级",
        ),
    )
    op.add_column(
        "health_records",
        sa.Column(
            "triage_reason",
            sa.Text(),
            nullable=True,
            comment="分级依据（规则命中明细/人工复核标注）",
        ),
    )


def downgrade() -> None:
    op.drop_column("health_records", "triage_reason")
    op.drop_column("health_records", "triage_level")
