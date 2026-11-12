"""应用工厂：注册生命周期、中间件、异常处理器、API 路由与静态页面托管。"""

import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.api.routes.metrics import router as metrics_router  # Day10 步骤 3：/metrics
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.logging import setup_logging
from app.core.metrics import inc, record_http_duration
from app.middleware.trace_id import TraceIdMiddleware
from app.realtime.notify_ws import router as notify_ws_router  # Day9 步7：通知 WS

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def no_cache_static(request: Request, call_next) -> Response:
    """HTML/JS/CSS 禁用启发式缓存：静态资源更新后浏览器必须回源校验。

    避免"HTML 已更新而 JS/CSS 仍是旧缓存"的版本错位（会引发前端脚本报错、
    页面交互失效）。no-cache 为标准 HTTP 头，主流浏览器均支持；接口响应不受影响。
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith(("text/html", "application/javascript", "text/css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


async def metrics_middleware(request: Request, call_next) -> Response:
    """Day10 步骤 3：进程内请求计数 + 耗时环形缓冲（/metrics 自身不计）。"""
    settings = get_settings()
    if not settings.metrics_enabled or request.url.path == "/metrics":
        return await call_next(request)
    start = time.perf_counter()
    response = await call_next(request)
    inc("http_requests_total")
    record_http_duration(time.perf_counter() - start)
    return response


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="康伴 3D 数字人康养陪伴系统",
        lifespan=lifespan,
    )
    app.add_middleware(TraceIdMiddleware)
    app.middleware("http")(no_cache_static)
    app.middleware("http")(metrics_middleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(metrics_router)  # Day10 步骤 3：Prometheus 文本格式
    app.include_router(notify_ws_router)  # Day9 步7：/ws/notify 通知实时下行
    # Day7+ 修复「素材加载失败 404」：媒体库 media_uri 统一写 /static/media/xxx
    # （import_media.py / import_user_media.py / 点播链路口径），而 / 挂载下真实路径
    # 是 /media/xxx——补 /static 别名挂载让既有 media_uri 直接可播，不必迁数据。
    # ⚠️ 必须注册在 / 挂载之前：Mount("/") 前缀匹配一切路径，后注册的 /static 永不命中。
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static-alias")
    # 静态页面最后挂载到 /（html=True）：API 路由注册在先，优先匹配不冲突
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# 保留模块级 app 对象，支持 `uvicorn app.main:app`
app = create_app()
