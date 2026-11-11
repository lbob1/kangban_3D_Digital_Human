"""老人端天气 HTTP 端点（老人端排版升级步 2）。

GET /api/v1/weather/now：设备 Token 鉴权（人 JWT 403），复用 MCP 天气客户端
（含重试与 mock 降级），返回完整结构化 dict——供老人端天气模块放大视图 /
手动刷新；语音链路的弹窗数据仍走既有 WS weather 事件（D9 汇合处下发），
本端点为同源补充通道，两者数据结构完全一致。
坐标/城市名取 Settings（WEATHER_LAT/LON/WEATHER_CITY_NAME，.env 可覆盖）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_device
from app.core.config import get_settings
from app.tools.weather_mcp import VALID_DATES, get_weather_client

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("/now")
async def weather_now(
    device_id: UUID = Depends(get_current_device),
    date: str = Query("today", description="查询日期：today/tomorrow/day_after"),
) -> dict[str, Any]:
    del device_id  # 天气为只读公共数据，设备鉴权仅作通道门禁，不参与数据
    date = date if date in VALID_DATES else "today"
    settings = get_settings()
    data = await get_weather_client().get_weather(
        settings.weather_lat, settings.weather_lon, date
    )
    data["city"] = settings.weather_city_name
    return data
