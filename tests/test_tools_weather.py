"""Day6 P1-6 MCP 天气单测：fake 工具与故障注入，不在单测里连真实 open-meteo。

覆盖口径（D9 双路径 + §4.4 回复管线）：
- server 侧：中文映射、网络失败重试 2 次后降级 mock（可见 degraded）；
- client 侧：子进程拉不起 → 重试后本地 mock 兜底；
- 注册表：schema 不含 elder_id（TDD §5.3）；
- 回复管线：完整数据 ws 事件全量下发 + LLM 精简答复 / LLM 不可用退模板句 /
  mock 降级可见话术标注；
- 双路径汇合：规则快路径与 S1 intent=weather 同一 handler 同一答复。
"""

import importlib.util
import json
import sys

import httpx
import pytest

from app.integrations.llm import LlmUnavailable
from app.router.cascade import CascadeRouter, _register_default_handlers
from app.router.rules import RuleMatch
from app.system_one.questions import IntentLabel, SystemOneResult
from app.tools import registry, weather_mcp

SERVER_PATH = weather_mcp.SERVER_PATH


def _load_server_module():
    spec = importlib.util.spec_from_file_location("kangban_weather_server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_DATA = {
    "latitude": 39.9093,
    "longitude": 116.3973,
    "summary": "晴",
    "temp_c": 26.0,
    "humidity": 55,
    "wind_kmh": 9.0,
    "today_max_c": 29.0,
    "today_min_c": 19.0,
    "source": "open-meteo",
    "degraded": False,
}


@pytest.fixture
def fake_weather_tool(monkeypatch):
    """注册表注入 fake weather 工具（不拉子进程不连网）。"""

    async def handler(args, runtime_ctx):
        return dict(FAKE_DATA)

    monkeypatch.setitem(
        registry._REGISTRY,
        "weather",
        registry.ToolSpec(name="weather", description="fake", schema={}, handler=handler),
    )


class _FakeS1:
    def __init__(self, intent: str, confidence: float) -> None:
        self.intent = intent
        self.confidence = confidence

    async def classify(self, text: str) -> SystemOneResult:
        intent = IntentLabel(self.intent)
        return SystemOneResult(
            intent=intent,
            is_sensitive=False,
            sensitive_types=[],
            confidence=self.confidence,
            needs_clarification=self.confidence < 0.85,
            privacy_restricted=False,
            is_out_of_domain=intent == IntentLabel.OTHER,
            secondary_intents=[],
            model_version="laya:fake@test1234",
        )


def _make_router(s1=None) -> CascadeRouter:
    router = CascadeRouter(system_one=s1)
    _register_default_handlers(router)
    router.register_handler("weather_fast_path", weather_mcp.build_weather_action_handler)
    return router


# ---- server 侧 ----


def test_map_payload_zh_mapping():
    mod = _load_server_module()
    payload = {
        "current": {
            "temperature_2m": 26.4,
            "relative_humidity_2m": 55,
            "weather_code": 0,
            "wind_speed_10m": 9.2,
        },
        "daily": {"temperature_2m_max": [29.1], "temperature_2m_min": [19.3]},
    }
    data = mod._map_payload(39.9093, 116.3973, payload)
    assert data["summary"] == "晴"
    assert data["temp_c"] == 26.4
    assert data["source"] == "open-meteo"
    assert data["degraded"] is False
    assert mod.WEATHER_CODE_ZH[61] == "小雨"  # 雨天码有中文映射


async def test_server_retries_then_mock_on_network_failure(monkeypatch):
    mod = _load_server_module()

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError("network down")

    monkeypatch.setattr(mod.httpx, "AsyncClient", FailingClient)
    data = await mod.fetch_open_meteo(39.9093, 116.3973)  # 内部重试 2 次（退避 ~1.5s）
    assert data["source"] == "mock"
    assert data["degraded"] is True
    assert "模拟数据" in data["note"]
    assert data["degraded_reason"] == "ConnectError"


# ---- client 侧 ----


async def test_client_falls_back_to_mock_on_spawn_failure():
    client = weather_mcp.WeatherMcpClient(
        command=[sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    data = await client.get_weather(39.9093, 116.3973)  # 重试 2 次（退避 ~0.9s）
    assert data["source"] == "mock"
    assert data["degraded"] is True


# ---- 注册表 ----


def test_weather_schema_contains_no_elder_id():
    spec = weather_mcp.build_tool_spec()
    assert "elder_id" not in json.dumps(spec.schema, ensure_ascii=False)
    assert set(spec.schema["properties"]) == {"latitude", "longitude"}


# ---- 回复管线（D9：完整信息 + LLM 精简答复）----


async def test_pipeline_full_event_and_condensed_reply(fake_weather_tool, monkeypatch):
    async def fake_condense(data, **kwargs):
        return "今儿个白天晴，26 度，出门戴顶帽子哈。"

    monkeypatch.setattr(weather_mcp, "condense_via_llm", fake_condense)
    rule = RuleMatch(
        rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
    )
    outcome = await weather_mcp.build_weather_action_handler(rule, None)
    assert outcome.reply_text == "今儿个白天晴，26 度，出门戴顶帽子哈。"
    assert outcome.tool_calls == 1
    assert outcome.escalated_to_agent is False  # 精简调用 ≠ 升级主控
    event = outcome.ws_events[0]
    assert event["type"] == "weather"
    assert event["data"]["source"] == "open-meteo"
    assert "天气实况" in event["full_text"] and "29.0" in event["full_text"]  # 全量数据可见


async def test_pipeline_llm_unavailable_falls_to_template(fake_weather_tool, monkeypatch):
    async def broken_condense(data, **kwargs):
        raise LlmUnavailable("llm down")

    monkeypatch.setattr(weather_mcp, "condense_via_llm", broken_condense)
    rule = RuleMatch(
        rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
    )
    outcome = await weather_mcp.build_weather_action_handler(rule, None)
    assert outcome.reply_text == "今天晴，气温 26.0 度，最高 29.0 度，最低 19.0 度。"
    assert "模拟" not in outcome.reply_text  # 真实数据无降级标注


async def test_pipeline_degraded_mock_gets_visible_note(fake_weather_tool, monkeypatch):
    async def degraded_handler(args, runtime_ctx):
        data = dict(FAKE_DATA)
        data.update(source="mock", degraded=True, note="天气服务暂不可用，为您播报模拟数据")
        return data

    monkeypatch.setitem(
        registry._REGISTRY,
        "weather",
        registry.ToolSpec(
            name="weather", description="fake", schema={}, handler=degraded_handler
        ),
    )
    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: False)  # 双重降级
    rule = RuleMatch(
        rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
    )
    outcome = await weather_mcp.build_weather_action_handler(rule, None)
    assert outcome.reply_text.startswith("天气服务暂不可用，为您播报模拟数据。")
    assert "今天晴" in outcome.reply_text  # 模板句兜底


async def test_pipeline_without_registered_tool_returns_none(monkeypatch):
    monkeypatch.delitem(registry._REGISTRY, "weather", raising=False)
    rule = RuleMatch(
        rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
    )
    outcome = await weather_mcp.build_weather_action_handler(rule, None)
    assert outcome is None  # 落下一层，不静默失败


# ---- 双路径汇合（D9）----


async def test_double_path_converges_same_handler_and_reply(fake_weather_tool, monkeypatch):
    async def fake_condense(data, **kwargs):
        return "今儿个白天晴，26 度。"

    monkeypatch.setattr(weather_mcp, "condense_via_llm", fake_condense)

    path_a = await _make_router().decide("今天天气怎么样")  # 规则快路径
    assert path_a.route_layer == "rule"
    assert path_a.intent == "weather_fast_path"
    assert path_a.tool_calls == 1
    assert path_a.escalated_to_agent is False

    path_b = await _make_router(_FakeS1("weather", 0.92)).decide("明天下雨吗")  # S1 变体
    assert path_b.route_layer == "system_one"
    assert path_b.intent == "weather"
    assert path_b.tool_calls == 1
    assert path_b.escalated_to_agent is False
    assert path_b.reply_text == path_a.reply_text  # 同一管线同一答复
    assert path_b.ws_events == path_a.ws_events


async def test_weather_still_reaches_agent_without_tool(monkeypatch):
    monkeypatch.delitem(registry._REGISTRY, "weather", raising=False)
    outcome = await _make_router().decide("今天天气怎么样")
    assert outcome.route_layer == "agent"
    assert outcome.escalated_to_agent is True
