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
from app.router.cascade import CascadeRouter, _register_default_handlers, stream_emitter
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
    "high_c": 29.0,
    "low_c": 19.0,
    "date_label": "今天",
    "source": "open-meteo",
    "degraded": False,
}


@pytest.fixture
def fake_weather_tool(monkeypatch):
    """注册表注入 fake weather 工具（不拉子进程不连网），并捕获最近一次 args。"""
    state = _WEATHER_ARGS_STATE

    async def handler(args, runtime_ctx):
        state["last_args"] = dict(args)
        return dict(FAKE_DATA)

    monkeypatch.setitem(
        registry._REGISTRY,
        "weather",
        registry.ToolSpec(name="weather", description="fake", schema={}, handler=handler),
    )


_WEATHER_ARGS_STATE: dict = {}  # fake handler 最近一次入参（日期透传断言用）


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


# ---- server 侧：老人端排版升级扩字段（体感/风向/降水/逐 3h×8 点）----


def test_wind_dir_zh_mapping():
    mod = _load_server_module()
    assert mod._wind_dir_zh(0) == "北"
    assert mod._wind_dir_zh(45) == "东北"
    assert mod._wind_dir_zh(225) == "西南"
    assert mod._wind_dir_zh(350) == "北"  # 350/45≈7.8 → round=8 → %8=0 归北
    assert mod._wind_dir_zh(315) == "西北"
    assert mod._wind_dir_zh(None) == "未知"
    assert mod._wind_dir_zh("bad") == "未知"


def _fake_open_meteo_payload() -> dict:
    """构造 open-meteo 三日响应（current + hourly 72 点 + daily），字段名与官方一致。"""
    hours = []
    for day in range(3):
        for h in range(0, 24, 1):
            hours.append(f"2026-11-{11 + day:02d}T{h:02d}:00")
    n = len(hours)
    return {
        "current": {
            "time": "2026-11-11T14:15",
            "temperature_2m": 11.4,
            "relative_humidity_2m": 42,
            "apparent_temperature": 9.8,
            "weather_code": 0,
            "wind_speed_10m": 12.3,
            "wind_direction_10m": 225,
            "precipitation": 0.2,
        },
        "hourly": {
            "time": hours,
            "temperature_2m": [10.0 + (i % 5) for i in range(n)],
            "apparent_temperature": [8.0 + (i % 5) for i in range(n)],
            "precipitation_probability": [(i * 7) % 100 for i in range(n)],
            "weather_code": [0 if i % 3 else 61 for i in range(n)],
        },
        "daily": {
            "weather_code": [0, 61, 3],
            "temperature_2m_max": [16.0, 14.0, 12.0],
            "temperature_2m_min": [0.0, 2.0, 1.0],
        },
    }


def test_map_payload_extended_fields_today():
    mod = _load_server_module()
    data = mod._map_payload(39.9093, 116.3973, _fake_open_meteo_payload(), "today")
    # 既有字段零回归
    assert data["temp_c"] == 11.4 and data["humidity"] == 42 and data["wind_kmh"] == 12.3
    # 新字段：体感/风向/降水
    assert data["apparent_c"] == 9.8
    assert data["wind_dir"] == "西南"  # 225°
    assert data["precipitation_mm"] == 0.2
    # 逐 3h×8 点：从当前小时 14:00 起的今天剩余时段
    hourly = data["hourly"]
    assert len(hourly) == 8
    assert hourly[0]["time"] == "2026-11-11T14:00"
    assert hourly[1]["time"] == "2026-11-11T17:00"
    for pt in hourly:
        assert set(pt) == {"time", "temp_c", "apparent_c", "precip_prob", "code"}


def test_map_payload_tomorrow_has_full_day_hourly():
    mod = _load_server_module()
    data = mod._map_payload(39.9093, 116.3973, _fake_open_meteo_payload(), "tomorrow")
    assert data["date_label"] == "明天"
    # 无实时观测：体感/湿度/风/降水如实置 None
    assert data["apparent_c"] is None
    assert data["humidity"] is None and data["wind_kmh"] is None
    assert data["wind_dir"] is None and data["precipitation_mm"] is None
    # 该日全天逐 3h×8 点（00:00 起）
    hourly = data["hourly"]
    assert len(hourly) == 8
    assert hourly[0]["time"] == "2026-11-12T00:00"
    assert hourly[7]["time"] == "2026-11-12T21:00"
    assert hourly[0]["apparent_c"] is not None  # 逐点体感来自 hourly 字段（曲线双线可用）


def test_mock_degraded_same_shape():
    """mock 降级与新字段同构：逐点 8 项 + 实时字段齐全（degraded=true 保持）。"""
    mod = _load_server_module()
    data = mod._mock_data(39.9093, 116.3973, "today")
    for key in ("apparent_c", "wind_dir", "precipitation_mm", "hourly"):
        assert key in data
    assert len(data["hourly"]) == 8
    for pt in data["hourly"]:
        assert set(pt) == {"time", "temp_c", "apparent_c", "precip_prob", "code"}
    tomorrow = mod._mock_data(39.9093, 116.3973, "tomorrow")
    assert len(tomorrow["hourly"]) == 8
    tomorrow_base = (mod.date_cls.today() + mod.timedelta(days=1)).isoformat()
    assert tomorrow["hourly"][0]["time"].startswith(tomorrow_base)


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
    assert set(spec.schema["properties"]) == {"latitude", "longitude", "date"}  # Day6+ 日期


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
    assert "今天天气" in event["full_text"] and "29.0" in event["full_text"]  # 全量数据可见


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


# ---- 流式精简（POC-08 首包治理：首句即 TTS，不等全文生成）----


def _patch_stream(monkeypatch, deltas):
    async def fake_stream(messages, **kwargs):
        for delta in deltas:
            yield delta

    monkeypatch.setattr(weather_mcp, "stream_chat", fake_stream)
    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: True)


async def test_streaming_condense_emits_per_sentence(fake_weather_tool, monkeypatch):
    """有发射器：凑到句末即发射，首句起播不等全文；reply_text=已播全文。"""
    _patch_stream(monkeypatch, ["今儿个白天晴。", "26 度，出门记得", "戴顶帽子哈。"])
    emitted: list[str] = []

    async def emitter(sentence: str) -> None:
        emitted.append(sentence)

    token = stream_emitter.set(emitter)
    try:
        rule = RuleMatch(
            rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
        )
        outcome = await weather_mcp.build_weather_action_handler(rule, None)
    finally:
        stream_emitter.reset(token)
    assert emitted == ["今儿个白天晴。", "26 度，出门记得戴顶帽子哈。"]  # 第一句即发射
    assert outcome.streamed is True
    assert outcome.reply_text == "今儿个白天晴。26 度，出门记得戴顶帽子哈。"
    assert outcome.tool_calls == 1  # 流式 ≠ 升级主控，决策留痕口径不变


async def test_streaming_degraded_note_speaks_first(fake_weather_tool, monkeypatch):
    """降级提示先于精简句播出（顺序正确且首包更快）；LLM 不可用时 note+模板均入已播。"""

    async def degraded_handler(args, runtime_ctx):
        data = dict(FAKE_DATA)
        data.update(source="mock", degraded=True, note="天气服务暂不可用，为您播报模拟数据")
        return data

    monkeypatch.setitem(
        registry._REGISTRY,
        "weather",
        registry.ToolSpec(name="weather", description="fake", schema={}, handler=degraded_handler),
    )
    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: False)  # LLM 也不可用
    emitted: list[str] = []

    async def emitter(sentence: str) -> None:
        emitted.append(sentence)

    token = stream_emitter.set(emitter)
    try:
        rule = RuleMatch(
            rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
        )
        outcome = await weather_mcp.build_weather_action_handler(rule, None)
    finally:
        stream_emitter.reset(token)
    assert emitted[0] == "天气服务暂不可用，为您播报模拟数据。"  # note 先播
    assert "今天晴" in emitted[1]  # 模板句兜底（不再重复 note 前缀）
    assert outcome.reply_text == "".join(emitted)  # 不变量：reply_text == 实际播出全文
    assert outcome.streamed is True


async def test_streaming_llm_broken_midway_keeps_spoken(fake_weather_tool, monkeypatch):
    """精简流中途断连：沿用已播部分，不重说模板（与 Day5 主控语义一致）。"""

    async def fake_stream(messages, **kwargs):
        yield "今天晴。"
        raise LlmUnavailable("midway down")

    monkeypatch.setattr(weather_mcp, "stream_chat", fake_stream)
    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: True)
    emitted: list[str] = []

    async def emitter(sentence: str) -> None:
        emitted.append(sentence)

    token = stream_emitter.set(emitter)
    try:
        rule = RuleMatch(
            rule_id="weather_today", action="weather_fast_path", reply_text=None, latency_ms=0.1
        )
        outcome = await weather_mcp.build_weather_action_handler(rule, None)
    finally:
        stream_emitter.reset(token)
    assert emitted == ["今天晴。"]
    assert outcome.reply_text == "今天晴。"  # 沿用已播，不补模板
    assert outcome.streamed is True


# ---- 日期支持（Day6+：明天/后天天气）----


def test_parse_date_hint():
    assert weather_mcp.parse_date_hint("明天天气怎么样") == "tomorrow"
    assert weather_mcp.parse_date_hint("明日天气如何") == "tomorrow"
    assert weather_mcp.parse_date_hint("后天天气好吗") == "day_after"
    assert weather_mcp.parse_date_hint("大后天天气如何") == "day_after"
    assert weather_mcp.parse_date_hint("今天天气怎么样") == "today"
    assert weather_mcp.parse_date_hint("天气怎么样") == "today"


async def test_tomorrow_weather_passes_date_through(fake_weather_tool):
    """「明天天气怎么样」→ 规则快路径，date=tomorrow 透传工具（不再落主控说「抱歉」）。"""
    outcome = await _make_router().decide("明天天气怎么样")
    assert outcome.route_layer == "rule"
    assert _WEATHER_ARGS_STATE["last_args"] == {"date": "tomorrow"}
