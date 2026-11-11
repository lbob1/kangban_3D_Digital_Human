"""0018_sleep_details：health_records 增加 5 列睡眠细维度（Day8 睡眠数据批次）

Revision ID: 0018_sleep_details
Revises: 0017_health_triage
Create Date: 2026-11-11

设计依据（用户 2026-11-11 裁决「扩表加细维度 / 5列含浅睡」）：
- sleep_bed_time / sleep_wake_time：入睡、起床时刻（timestamptz，北京时间口径展示）；
- sleep_awakenings：夜醒次数；sleep_deep_minutes / sleep_light_minutes：深睡、浅睡分钟；
- 全 nullable（旧数据与血压单查记录兼容）；业务一致性（deep+light ≤ minutes）
  由写入方（外呼采集/种子脚本）保证，不加 CHECK（只增不改原则）；
- downgrade 对称（drop_column）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_sleep_details"
down_revision: Union[str, None] = "0017_health_triage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "health_records",
        sa.Column(
            "sleep_bed_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="入睡时刻（UTC 存储，北京时间展示）",
        ),
    )
    op.add_column(
        "health_records",
        sa.Column(
            "sleep_wake_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="起床时刻（UTC 存储，北京时间展示）",
        ),
    )
    op.add_column(
        "health_records",
        sa.Column("sleep_awakenings", sa.SmallInteger(), nullable=True, comment="夜醒次数"),
    )
    op.add_column(
        "health_records",
        sa.Column(
            "sleep_deep_minutes",
            sa.Integer(),
            nullable=True,
            comment="深睡分钟（写入方保证 deep+light ≤ sleep_minutes）",
        ),
    )
    op.add_column(
        "health_records",
        sa.Column("sleep_light_minutes", sa.Integer(), nullable=True, comment="浅睡分钟"),
    )


def downgrade() -> None:
    op.drop_column("health_records", "sleep_light_minutes")
    op.drop_column("health_records", "sleep_deep_minutes")
    op.drop_column("health_records", "sleep_awakenings")
    op.drop_column("health_records", "sleep_wake_time")
    op.drop_column("health_records", "sleep_bed_time")
