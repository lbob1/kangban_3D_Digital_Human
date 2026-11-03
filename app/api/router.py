"""聚合 HTTP API 路由。业务 API 后续挂载在 /api/v1 前缀下（TDD 7.1）。"""

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)
