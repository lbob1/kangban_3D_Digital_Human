"""MCP 天气 Tool（Day6 P1-6，大纲 6.3 第一个 Tool，D9 双路径汇合）。

口径（Day6 主文档 §4.4）：
- 官方 mcp SDK（FastMCP），stdio 传输；与主项目依赖隔离（本目录独立 pyproject）；
- 工具 get_weather(latitude, longitude, date) → open-meteo forecast（免 key）；
  中文天气描述映射在 server 侧做；date 支持 today/tomorrow/day_after
  （Day6+ 用户裁决：「明天天气」为常用诉求，预报数据源原生支持）；
- 网络失败重试 2 次（指数退避，Harness Recovery 口径）→ 仍失败降级确定性
  mock 数据（degraded=true + 可见话术标注字段），不白屏不静默；
- Tool schema 不含 elder_id（TDD §5.3：elder_id 由 Runtime Context 注入，
  天气为只读公共数据，schema 只有坐标与日期参数）。
"""

from __future__ import annotations

import asyncio
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
RETRY_ATTEMPTS = 2  # 失败后重试次数（首次 + 2 次重试，指数退避 0.5s/1s）
RETRY_BACKOFF_BASE_S = 0.5
REQUEST_TIMEOUT_S = 8.0

# 逐 3 小时 × 8 点（老人端天气模块温度曲线）：今天=当前小时起剩余时段，
# 明天/后天=该日全天（00:00 起步进 3h）
HOURLY_STEP_H = 3
HOURLY_POINT_N = 8

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

# 降级确定性 mock（「晴，22 度」口径）——degraded=true 由调用方拼可见话术。
# hourly 逐点由 _mock_data 按请求日期动态生成（同构：与真实返回字段一致）
MOCK_DATA: dict[str, Any] = {
    "summary": "晴",
    "temp_c": 22.0,
    "apparent_c": 23.5,
    "wind_kmh": 8.0,
    "wind_dir": "西南",
    "humidity": 60,
    "precipitation_mm": 0.0,
    "high_c": 26.0,
    "low_c": 17.0,
    "date_label": "今天",
    "source": "mock",
    "degraded": True,
    "note": "天气服务暂不可用，为您播报模拟数据",
}

# 风向度数 → 中文八向（N=0° 起顺时针，45° 一档）
_WIND_DIR_ZH = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")

# mock 逐小时的固定时刻（3h 步进 ×8 点，仅时段标签用途）
_MOCK_HOURS = ("02:00", "05:00", "08:00", "11:00", "14:00", "17:00", "20:00", "23:00")

DATE_LABELS: dict[str, str] = {"today": "今天", "tomorrow": "明天", "day_after": "后天"}


def _wind_dir_zh(deg: Any) -> str:
    """风向度数 → 中文八向；缺失/非法值如实返回「未知」。"""
    try:
        return _WIND_DIR_ZH[round(float(deg) / 45) % 8]
    except (TypeError, ValueError):
        return "未知"


def _mock_hourly(day_offset: int) -> list[dict[str, Any]]:
    """mock 逐 3 小时 × 8 点（确定性与真实结构同构：time/temp_c/apparent_c/precip_prob/code）。"""
    base = (date_cls.today() + timedelta(days=day_offset)).isoformat()
    return [
        {
            "time": f"{base}T{h}",
            "temp_c": 17.0 + 2.0 * (i % 4),
            "apparent_c": 18.5 + 2.0 * (i % 4),
            "precip_prob": (i * 5) % 30,
            "code": 0,
        }
        for i, h in enumerate(_MOCK_HOURS)
    ]


def _mock_data(latitude: float, longitude: float, date: str = "today") -> dict[str, Any]:
    data = dict(MOCK_DATA)
    data["latitude"] = latitude
    data["longitude"] = longitude
    data["date_label"] = DATE_LABELS.get(date, "今天")
    data["hourly"] = _mock_hourly({"today": 0, "tomorrow": 1}.get(date, 2))
    return data


async def fetch_open_meteo(
    latitude: float, longitude: float, date: str = "today"
) -> dict[str, Any]:
    """open-meteo 拉取 + 中文映射；重试 2 次（指数退避）后降级 mock（可见降级）。"""
    date = date if date in DATE_LABELS else "today"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
            "wind_speed_10m,wind_direction_10m,precipitation"
        ),
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 3,  # 今天/明天/后天（hourly 逐 3h×8 点曲线数据源）
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
            return _map_payload(latitude, longitude, payload, date)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
    # 重试耗尽 → 确定性 mock（可见降级，不静默；调用方据 degraded 拼话术标注）
    mock = _mock_data(latitude, longitude, date)
    mock["degraded_reason"] = f"{type(last_error).__name__}" if last_error else "unknown"
    return mock


def _hourly_points(hourly: dict, start_idx: int) -> list[dict[str, Any]]:
    """从 open-meteo hourly 数组取逐 3h × 8 点（越界如实截断，不补假点）。"""
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    apparents = hourly.get("apparent_temperature", [])
    probs = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])
    end = min(len(times), start_idx + HOURLY_STEP_H * HOURLY_POINT_N)
    points: list[dict[str, Any]] = []
    for i in range(start_idx, end, HOURLY_STEP_H):
        apparent = apparents[i] if i < len(apparents) else None
        prob = probs[i] if i < len(probs) else None
        code = codes[i] if i < len(codes) else None
        points.append(
            {
                "time": times[i],
                "temp_c": float(temps[i]) if temps[i] is not None else None,
                "apparent_c": float(apparent) if apparent is not None else None,
                "precip_prob": int(prob) if prob is not None else None,
                "code": int(code) if code is not None else 3,
            }
        )
    return points


def _hourly_start_index(date: str, hourly: dict, current_time: str) -> int:
    """hourly 切片起点：今天=当前小时（current.time 形如 2026-11-11T14:15），
    明天/后天=对应日 00:00（forecast_days=3，hourly 按日 24 点铺开）。"""
    if date == "tomorrow":
        return 24
    if date == "day_after":
        return 48
    prefix = (current_time or "")[:13]  # 截到「YYYY-MM-DDTHH」
    for i, t in enumerate(hourly.get("time", [])):
        if i >= 24:  # 今天窗口只在前 24 点内找
            break
        if str(t)[:13] >= prefix:
            return i
    return 0


def _map_payload(
    latitude: float, longitude: float, payload: dict, date: str = "today"
) -> dict[str, Any]:
    daily = payload["daily"]
    hourly = payload.get("hourly", {})
    label = DATE_LABELS.get(date, "今天")
    if date == "today":
        current = payload["current"]
        code = int(current["weather_code"])
        today_start = _hourly_start_index("today", hourly, current.get("time") or "")
        return {
            "latitude": latitude,
            "longitude": longitude,
            "date_label": label,
            "summary": WEATHER_CODE_ZH.get(code, f"天气代码{code}"),
            "temp_c": float(current["temperature_2m"]),
            "apparent_c": (
                float(current["apparent_temperature"])
                if current.get("apparent_temperature") is not None
                else None
            ),
            "humidity": int(current["relative_humidity_2m"]),
            "wind_kmh": float(current["wind_speed_10m"]),
            "wind_dir": _wind_dir_zh(current.get("wind_direction_10m")),
            "precipitation_mm": float(current.get("precipitation") or 0.0),
            "high_c": float(daily["temperature_2m_max"][0]),
            "low_c": float(daily["temperature_2m_min"][0]),
            "hourly": _hourly_points(hourly, today_start),
            "source": "open-meteo",
            "degraded": False,
        }
    # 明天/后天：无实时观测（体感/风向/降水置 None 如实呈现）；
    # 逐 3h×8 点取该日全天（00:00 起），曲线含该日温度/体感/降水概率
    idx = 1 if date == "tomorrow" else 2
    code = int(daily["weather_code"][idx])
    high = float(daily["temperature_2m_max"][idx])
    low = float(daily["temperature_2m_min"][idx])
    return {
        "latitude": latitude,
        "longitude": longitude,
        "date_label": label,
        "summary": WEATHER_CODE_ZH.get(code, f"天气代码{code}"),
        "temp_c": round((high + low) / 2, 1),
        "apparent_c": None,
        "humidity": None,
        "wind_kmh": None,
        "wind_dir": None,
        "precipitation_mm": None,
        "high_c": high,
        "low_c": low,
        "hourly": _hourly_points(hourly, 24 if date == "tomorrow" else 48),
        "source": "open-meteo",
        "degraded": False,
    }


mcp = FastMCP("weather")


@mcp.tool()
async def get_weather(latitude: float, longitude: float, date: str = "today") -> dict[str, Any]:
    """查询指定坐标的天气（长者社区播报用，中文描述）。

    date: "today" 今天（默认，含实时湿度风速）；"tomorrow" 明天；"day_after" 后天。
    """
    return await fetch_open_meteo(latitude, longitude, date)


if __name__ == "__main__":
    mcp.run(transport="stdio")
