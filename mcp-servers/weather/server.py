"""MCP 天气 Tool（Day6 P1-6，大纲 6.3 第一个 Tool，D9 双路径汇合）。

口径（Day6 主文档 §4.4）：
- 官方 mcp SDK（FastMCP），stdio 传输；与主项目依赖隔离（本目录独立 pyproject）；
- 工具 get_weather(latitude, longitude) → open-meteo forecast（免 key）；
  中文天气描述映射在 server 侧做；
- 网络失败重试 2 次（指数退避，Harness Recovery 口径）→ 仍失败降级确定性
  mock 数据（degraded=true + 可见话术标注字段），不白屏不静默；
- Tool schema 不含 elder_id（TDD §5.3：elder_id 由 Runtime Context 注入，
  天气为只读公共数据，schema 只有坐标两个参数）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
RETRY_ATTEMPTS = 2  # 失败后重试次数（首次 + 2 次重试，指数退避 0.5s/1s）
RETRY_BACKOFF_BASE_S = 0.5
REQUEST_TIMEOUT_S = 8.0

# WMO weather code → 中文描述（open-meteo 官方码表精简映射）
WEATHER_CODE_ZH: dict[int, str] = {
    0: "晴",
    1: "基本晴",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "浓毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "雷阵雨伴冰雹",
}

# 降级确定性 mock（「晴，22 度」口径）——degraded=true 由调用方拼可见话术
MOCK_DATA: dict[str, Any] = {
    "summary": "晴",
    "temp_c": 22.0,
    "wind_kmh": 8.0,
    "humidity": 60,
    "today_max_c": 26.0,
    "today_min_c": 17.0,
    "source": "mock",
    "degraded": True,
    "note": "天气服务暂不可用，为您播报模拟数据",
}


def _mock_data(latitude: float, longitude: float) -> dict[str, Any]:
    data = dict(MOCK_DATA)
    data["latitude"] = latitude
    data["longitude"] = longitude
    return data


async def fetch_open_meteo(latitude: float, longitude: float) -> dict[str, Any]:
    """open-meteo 拉取 + 中文映射；重试 2 次（指数退避）后降级 mock（可见降级）。"""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 1,
    }
    last_error: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS + 1):
        if attempt > 0:
            await asyncio.sleep(RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)))
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
                resp = await client.get(OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
            return _map_payload(latitude, longitude, payload)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last_error = exc
    # 重试耗尽 → 确定性 mock（可见降级，不静默；调用方据 degraded 拼话术标注）
    mock = _mock_data(latitude, longitude)
    mock["degraded_reason"] = f"{type(last_error).__name__}" if last_error else "unknown"
    return mock


def _map_payload(latitude: float, longitude: float, payload: dict) -> dict[str, Any]:
    current = payload["current"]
    daily = payload["daily"]
    code = int(current["weather_code"])
    return {
        "latitude": latitude,
        "longitude": longitude,
        "summary": WEATHER_CODE_ZH.get(code, f"天气代码{code}"),
        "temp_c": float(current["temperature_2m"]),
        "humidity": int(current["relative_humidity_2m"]),
        "wind_kmh": float(current["wind_speed_10m"]),
        "today_max_c": float(daily["temperature_2m_max"][0]),
        "today_min_c": float(daily["temperature_2m_min"][0]),
        "source": "open-meteo",
        "degraded": False,
    }


mcp = FastMCP("weather")


@mcp.tool()
async def get_weather(latitude: float, longitude: float) -> dict[str, Any]:
    """查询指定坐标的当前天气与今日温度范围（长者社区播报用，中文描述）。"""
    return await fetch_open_meteo(latitude, longitude)


if __name__ == "__main__":
    mcp.run(transport="stdio")
