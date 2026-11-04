"""应用工厂：注册生命周期、中间件、异常处理器、API 路由与静态页面托管。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.logging import setup_logging
from app.middleware.trace_id import TraceIdMiddleware

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="康伴 3D 数字人康养陪伴系统",
        lifespan=lifespan,
    )
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    # 静态页面最后挂载到 /（html=True）：API 路由注册在先，优先匹配不冲突
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# 保留模块级 app 对象，支持 `uvicorn app.main:app`
app = create_app()
