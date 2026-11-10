"""0012_memory_embedding_dim：memory_chunks.embedding 固定 1024 维 + ivfflat ANN 索引

Revision ID: 0012_memory_embedding_dim
Revises: 0011_workorder_actions
Create Date: 2026-11-10

设计依据（Day7 主文档 §5.1，D2 方案 B 用户裁决）：
- embedding 模型选定百炼 qwen3.7-text-embedding-flash（维度档位 256/512/768/1024），
  EMBEDDING_DIMENSIONS=1024 与本迁移、ORM（app/models/memory.py）三处一次对齐；
- 0002 建表时为无维度 vector 列（「维度待 Embedding 模型确定后由后续迁移固定」），
  表内无存量数据，直改安全；
- ivfflat + vector_cosine_ops（余弦距离，召回排序口径），lists=100 适配小规模记忆
  （数千行内足够；pgvector 支持 hnsw 可后续可选升级，不在本迁移扩scope）；
- embedding_model 列统一标注选定模型名（存量本为空，无数据回填问题）；
- downgrade 对称：删索引、回无维度列。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_memory_embedding_dim"
down_revision: Union[str, None] = "0011_workorder_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"


def upgrade() -> None:
    # 维度固定：无存量数据，USING 直转安全
    op.execute(
        "ALTER TABLE memory_chunks ALTER COLUMN embedding"
        " TYPE vector(1024) USING embedding::vector(1024)"
    )
    # ANN 索引：余弦距离（召回 top-5 排序口径）；lists=100 适配当前数据量级
    op.execute(
        "CREATE INDEX ix_memory_chunks_embedding ON memory_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    # 模型标注统一（元数据列，非数据回填）
    op.execute(
        sa.text(
            "UPDATE memory_chunks SET embedding_model = :m WHERE embedding_model IS NULL"
        ).bindparams(m=_EMBEDDING_MODEL)
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_chunks_embedding")
    # 对称回 0002 的无维度 vector 列
    op.execute(
        "ALTER TABLE memory_chunks ALTER COLUMN embedding TYPE vector USING embedding::vector"
    )
