"""API 路由聚合。"""

from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    devices,
    doctor,
    elders,
    health,
    health_profile,
    health_records,
    media,
    medication,
    nurse,
    voice,
    weather,
)
from app.realtime.routes import router as realtime_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router, prefix="/api/v1")
api_router.include_router(elders.router, prefix="/api/v1")
api_router.include_router(devices.router, prefix="/api/v1")
api_router.include_router(devices.admin_router, prefix="/api/v1")
api_router.include_router(admin.router, prefix="/api/v1")  # /admin/assignments/*
api_router.include_router(admin.rooms_router, prefix="/api/v1")
api_router.include_router(admin.personas_router, prefix="/api/v1")
api_router.include_router(admin.accounts_router, prefix="/api/v1")
api_router.include_router(admin.sensitive_router, prefix="/api/v1")  # Day6 HITL 敏感事件
api_router.include_router(admin.conversations_router, prefix="/api/v1")  # Day6+ 对话记录
api_router.include_router(health_profile.router, prefix="/api/v1")  # 健康档案主体
api_router.include_router(health_profile.nurse_router, prefix="/api/v1")
api_router.include_router(health_profile.admin_router, prefix="/api/v1")
api_router.include_router(health_profile.config_router, prefix="/api/v1")
api_router.include_router(voice.router, prefix="/api/v1")  # Day3 语音：WS + enroll
api_router.include_router(realtime_router, prefix="/api/v1")  # Day3 WebRTC 信令
api_router.include_router(medication.router, prefix="/api/v1")  # Day7 用药计划/事件
api_router.include_router(media.router, prefix="/api/v1")  # Day7 媒体库/播放留痕
api_router.include_router(weather.router, prefix="/api/v1")  # 老人端天气模块数据通道
api_router.include_router(doctor.router, prefix="/api/v1")  # Day8 医生端健康趋势
api_router.include_router(health_records.router, prefix="/api/v1")  # Day8 健康数值登记
api_router.include_router(nurse.router, prefix="/api/v1")  # Day8 护工 workqueue 四类聚合
