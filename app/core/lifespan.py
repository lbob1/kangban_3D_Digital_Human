"""应用生命周期：启动时初始化日志，关闭时释放数据库引擎。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = get_logger(__name__)
    logger.info("应用启动", extra={"event": "startup"})
    # Day7 步7：用药提醒调度（Settings.medication_scheduler_enabled 可关；启动失败不阻断）
    try:
        from app.scheduler import start_scheduler

        start_scheduler()
    except Exception:  # noqa: BLE001 —— 调度不可用走可见降级，应用照常启动
        logger.exception("scheduler start failed")
    yield
    # 关闭时释放调度器与数据库引擎连接池
    try:
        from app.scheduler import stop_scheduler

        await stop_scheduler()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler stop failed")
    from app.db.session import dispose_engine

    await dispose_engine()
    logger.info("应用关闭，引擎已释放", extra={"event": "shutdown"})
