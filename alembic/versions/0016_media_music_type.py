"""0016_media_music_type：media_library.type 扩展 'music'（Day7+ 用户自备音乐）

0014 落了媒体两表后，点播链路只支持戏曲（opera）。Day7+ 引入用户自备
音乐目录（music/）→「我想听音乐」→ type=music 播放。仅扩 CHECK 约束，
不动其他列；经人工评审后执行（安全禁区第 7 条：不回改已执行迁移）。

Revision ID: 0016_media_music_type
Revises: 0015_workorder_review
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_media_music_type"
down_revision: str | None = "0015_workorder_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_media_library_type", "media_library", type_="check")
    op.create_check_constraint(
        "ck_media_library_type",
        "media_library",
        "type IN ('opera', 'movie', 'music')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_media_library_type", "media_library", type_="check")
    op.create_check_constraint(
        "ck_media_library_type",
        "media_library",
        "type IN ('opera', 'movie')",
    )
    # 回滚后遗留的 music 行将违反约束——演示库按需手工清理，不自动删数据
