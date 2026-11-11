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
# 昨天/明天/后天=该日全天（00:00 起步进 3h）
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

DATE_LABELS: dict[str, str] = {
    "yesterday": "昨天",
    "today": "今天",
    "tomorrow": "明天",
    "day_after": "后天",
}

# past_days=1 + forecast_days=3 → daily/hourly 数组按 [昨天, 今天, 明天, 后天] 铺开
DAY_INDEX: dict[str, int] = {
    "yesterday": 0,
    "today": 1,
    "tomorrow": 2,
    "day_after": 3,
}


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
    data["hourly"] = _mock_hourly(
        {"yesterday": -1, "today": 0, "tomorrow": 1}.get(date, 2)
    )
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
        "hourly": (
            "temperature_2m,apparent_temperature,precipitation_probability,weather_code,"
            "relative_humidity_2m,wind_speed_10m,wind_direction_10m"
        ),
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
        "past_days": 1,      # 昨天（老人端放大视图新增 tab，open-meteo 原生支持）
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
    """hourly 切片起点（past_days=1 → 4 天窗 [昨天,今天,明天,后天] 各 24 点）。

    昨天/明天/后天=对应日 00:00（按 DAY_INDEX 定位）；今天=当前时刻所在小时
    （按日期前缀定位当日起点——不能固定扫前 24 点，past_days 后今天从 24 起）。
    """
    base = DAY_INDEX.get(date, 1) * 24
    if date != "today":
        return base
    times = hourly.get("time", [])
    prefix_day = (current_time or "")[:10]  # YYYY-MM-DD
    day_start = base  # current.time 缺失时按日序兜底
    if prefix_day:
        for i, t in enumerate(times):
            if str(t)[:10] == prefix_day:
                day_start = i
                break
    prefix_hour = (current_time or "")[:13]  # YYYY-MM-DDTHH
    for i in range(day_start, min(len(times), day_start + 24)):
        if str(times[i])[:13] >= prefix_hour:
            return i
    return day_start


def _day_aggregates(hourly: dict, day_start: int) -> dict[str, Any]:
    """非今天的代表值：从该日 24 个逐时「实报/实预报」点派生，全部为源数据
    真实值（不造假）：均温/均湿/均风速 + 14 时体感/14 时风向（缺失取首有效点）。
    修复口径：此前明天/后天的体感/湿度/风速置 None 导致前端模块空白。
    """
    times = hourly.get("time", [])
    sl = lambda key: [  # noqa: E731 —— 该日窗口内的小切片
        v for v in hourly.get(key, [])[day_start : day_start + 24] if v is not None
    ]
    temps = sl("temperature_2m")
    hums = sl("relative_humidity_2m")
    winds = sl("wind_speed_10m")
    # 14 时代表点（该日窗口内找不到则取首点）
    rep = day_start
    for i in range(day_start, min(len(times), day_start + 24)):
        if str(times[i])[11:13] == "14":
            rep = i
            break
    apparents = hourly.get("apparent_temperature", [])
    dirs = hourly.get("wind_direction_10m", [])
    app = apparents[rep] if rep < len(apparents) else None
    wd = dirs[rep] if rep < len(dirs) else None
    return {
        "temp_mean": round(sum(temps) / len(temps), 1) if temps else None,
        "apparent_rep": float(app) if app is not None else None,
        "humidity_mean": round(sum(hums) / len(hums)) if hums else None,
        "wind_mean": round(sum(winds) / len(winds), 1) if winds else None,
        "wind_dir_rep": _wind_dir_zh(wd),
    }


def _map_payload(
    latitude: float, longitude: float, payload: dict, date: str = "today"
) -> dict[str, Any]:
    daily = payload["daily"]
    hourly = payload.get("hourly", {})
    label = DATE_LABELS.get(date, "今天")
    idx = DAY_INDEX.get(date, 1)
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
            "high_c": float(daily["temperature_2m_max"][idx]),   # idx=1：past_days 后
            "low_c": float(daily["temperature_2m_min"][idx]),    # 今天不再是第 0 项
            "hourly": _hourly_points(hourly, today_start),
            "source": "open-meteo",
            "degraded": False,
        }
    # 昨天/明天/后天：无 current 实时观测（昨天=历史实报、明天/后天=预报），
    # 代表值由该日逐时数据派生（真实值，见 _day_aggregates）；逐 3h×8 点取该日全天
    code = int(daily["weather_code"][idx])
    agg = _day_aggregates(hourly, idx * 24)
    precip = daily.get("precipitation_sum")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "date_label": label,
        "summary": WEATHER_CODE_ZH.get(code, f"天气代码{code}"),
        "temp_c": agg["temp_mean"],
        "apparent_c": agg["apparent_rep"],
        "humidity": agg["humidity_mean"],
        "wind_kmh": agg["wind_mean"],
        "wind_dir": agg["wind_dir_rep"],
        "precipitation_mm": (
            float(precip[idx]) if precip and precip[idx] is not None else None
        ),
        "high_c": float(daily["temperature_2m_max"][idx]),
        "low_c": float(daily["temperature_2m_min"][idx]),
        "hourly": _hourly_points(hourly, idx * 24),
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
