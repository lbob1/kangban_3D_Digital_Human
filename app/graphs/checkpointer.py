"""用药确认图 SQLite checkpointer 单例工厂（大纲 7.2 / Day7 步6）。

- 路径走 Settings.medication_checkpoint_db（默认 ./data/med_checkpoints.sqlite）；
- 每次构图新开 aiosqlite 连接（文件级持久化，跨连接/跨进程恢复断点），
  规避「模块级连接绑定单一事件循环」与 pytest 函数级事件循环冲突；
- AsyncSqliteSaver.setup() 幂等建表。

langgraph-checkpoint-sqlite 为大纲 7.2 指定选型；为什么不用 Postgres checkpointer：
当日交付优先最小依赖，SQLite 单文件即满足「重启 get_state 恢复」验收硬项（ADR-023 备案）。
"""

from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import get_settings


def checkpoint_db_path(override: str | None = None) -> Path:
    """解析 checkpoint 库路径（测试可传 override），确保父目录存在。"""
    path = Path(override or get_settings().medication_checkpoint_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def open_checkpointer(override: str | None = None) -> AsyncSqliteSaver:
    """新开一条 aiosqlite 连接的 AsyncSqliteSaver（调用方负责随图生命周期关闭）。"""
    conn = await aiosqlite.connect(checkpoint_db_path(override))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver


async def close_checkpointer(saver: AsyncSqliteSaver) -> None:
    """关闭 checkpointer 底层连接。"""
    await saver.conn.close()
