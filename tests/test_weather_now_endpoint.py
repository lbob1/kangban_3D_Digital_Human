"""老人端天气 now 端点（老人端排版升级步 2）单测。

口径：
- 设备 Token 200：复用 MCP 客户端（fake 注入不连网），degraded 标记透传，
  city 取 Settings 默认「北京」，date 合法值透传；
- 人 JWT 403（设备/人身份互斥，与 /media 同口径）；
- 非法 date 一律回退 today（不信任客户端传参）。
"""

import pytest

from app.api.routes import weather as weather_route
from tests.conftest import login

NOW_URL = "/api/v1/weather/now"


class _FakeClient:
    """记录入参并返回固定结构（含 degraded=true 模拟降级）。"""

    def __init__(self) -> None:
        self.last_args: tuple = ()

    async def get_weather(self, latitude: float, longitude: float, date: str = "today"):
        self.last_args = (latitude, longitude, date)
        return {
            "latitude": latitude,
            "longitude": longitude,
            "date_label": "今天",
            "summary": "晴",
            "temp_c": 22.0,
            "apparent_c": 23.5,
            "humidity": 60,
            "wind_kmh": 8.0,
            "wind_dir": "西南",
            "precipitation_mm": 0.0,
            "high_c": 26.0,
            "low_c": 17.0,
            "hourly": [{"time": "2026-11-11T14:00", "temp_c": 22.0, "apparent_c": 23.5,
                        "precip_prob": 10, "code": 0}],
            "source": "mock",
            "degraded": True,
            "note": "天气服务暂不可用，为您播报模拟数据",
        }


@pytest.fixture
def fake_client(monkeypatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(weather_route, "get_weather_client", lambda: client)
    return client


async def _device_token(api, seed, device_key="single") -> str:
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    device = seed["devices"][device_key]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens", headers=admin
    )
    assert issued.status_code == 200, issued.text
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["access_token"]


async def test_device_token_ok_and_degraded_passthrough(api, seed, fake_client):
    token = await _device_token(api, seed)
    resp = await api.get(NOW_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is True  # 降级标记透传
    assert body["city"] == "北京"  # Settings 默认城市名
    assert body["summary"] == "晴" and body["temp_c"] == 22.0
    assert len(body["hourly"]) == 1 and body["hourly"][0]["temp_c"] == 22.0
    assert fake_client.last_args[2] == "today"  # 缺省 date=today


async def test_human_jwt_forbidden(api, seed, fake_client):
    human = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await api.get(NOW_URL, headers=human)
    assert resp.status_code == 403


async def test_invalid_date_falls_back_to_today(api, seed, fake_client):
    token = await _device_token(api, seed)
    resp = await api.get(
        NOW_URL, params={"date": "yesterday"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert fake_client.last_args[2] == "today"


async def test_valid_date_passthrough(api, seed, fake_client):
    token = await _device_token(api, seed)
    resp = await api.get(
        NOW_URL, params={"date": "tomorrow"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert fake_client.last_args[2] == "tomorrow"
