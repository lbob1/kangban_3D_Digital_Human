"""Day6 P1-7 CompanionAgent 单测：fake 决策/流式函数与 fake 工具，不连网不连真实 LLM。

覆盖口径（大纲 §4.5 + 验收项 10/11）：
- ReAct：决策 → guard+registry 执行 → 观察回填 → 流式终答（复杂例=天气+时间两工具）；
- 单轮直答免二次调用；决策轮失败降级直答流式；
- guard 接线（Day5 框架零改动生效）：同 Tool 60s 窗口第 3 次拒、单轮预算 8、
  reset_turn 窗口恢复；
- chat_with_tools 解析（OpenAI 兼容 tool_calls JSON）。
"""

import json

import httpx

from app.integrations.llm import LlmUnavailable, chat_with_tools
from app.services.companion import CompanionAgent
from app.tools import registry

# ---- fakes ----


def _install_fake_tools(monkeypatch, calls: list[tuple[str, dict]]):
    async def weather_handler(args, runtime_ctx):
        calls.append(("weather", dict(runtime_ctx)))
        return {"summary": "晴", "temp_c": 26.0, "degraded": False, "source": "fake"}

    async def time_handler(args, runtime_ctx):
        calls.append(("local_time", dict(runtime_ctx)))
        return {"time_text": "15点42分", "date_text": "11月09日", "weekday": "一"}

    monkeypatch.setitem(
        registry._REGISTRY,
        "weather",
        registry.ToolSpec(name="weather", description="fake", schema={}, handler=weather_handler),
    )
    monkeypatch.setitem(
        registry._REGISTRY,
        "local_time",
        registry.ToolSpec(
            name="local_time", description="fake", schema={}, handler=time_handler
        ),
    )


def _decision(content=None, tool_calls=None):
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "raw_tool_calls": _raw(tool_calls or []),
        "usage": {"total_tokens": 10},
    }


def _raw(tool_calls):
    return [
        {
            "id": tc["id"],
            "type": "function",
            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
        }
        for tc in tool_calls
    ]


def _fake_stream(chunks):
    async def stream(messages):
        for c in chunks:
            yield c

    return stream


# ---- ReAct 主流程 ----


async def test_direct_answer_single_call(monkeypatch):
    decisions = [_decision(content="今天太阳不错。")]

    async def fake_chat(messages, tools):
        return decisions.pop(0)

    streamed = []

    async def fail_stream(messages):
        streamed.append(messages)  # 不应走到流式
        yield "x"

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "你好"}],
        runtime_ctx={"elder_id": None},
        on_delta=None,
        chat_fn=fake_chat,
        stream_fn=fail_stream,
    )
    assert result.reply == "今天太阳不错。"
    assert result.tool_calls == 0 and result.rounds == 1
    assert streamed == []  # 直答不二次调用


async def test_react_two_tools_then_streamed_final(monkeypatch):
    _install_fake_tools(monkeypatch, calls := [])
    decisions = [
        _decision(
            tool_calls=[
                {"id": "c1", "name": "weather", "arguments": {}},
                {"id": "c2", "name": "local_time", "arguments": {}},
            ]
        ),
        _decision(content=None),
    ]

    async def fake_chat(messages, tools):
        assert tools and tools[0]["name"] == "weather"  # schema 来自注册表
        return decisions.pop(0)

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "外面天气如何，顺便现在几点了"}],
        runtime_ctx={"elder_id": "elder-1"},
        on_delta=None,
        chat_fn=fake_chat,
        stream_fn=_fake_stream(["外面晴，", "26 度，现在 15 点 42 分。"]),
    )
    assert result.tool_calls == 2
    assert result.tool_names == ["weather", "local_time"]
    assert calls[0][0] == "weather" and calls[1][0] == "local_time"
    assert calls[0][1] == {"elder_id": "elder-1"}  # Runtime Context 服务端注入
    assert result.reply == "外面晴，26 度，现在 15 点 42 分。"
    assert result.usage_total.get("total_tokens") == 20  # 两轮决策 token 累计


async def test_unknown_tool_returns_error_observation(monkeypatch):
    decisions = [
        _decision(tool_calls=[{"id": "c1", "name": "no_such_tool", "arguments": {}}]),
        _decision(content=None),
    ]

    async def fake_chat(messages, tools):
        return decisions.pop(0)

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "x"}],
        runtime_ctx={},
        on_delta=None,
        chat_fn=fake_chat,
        stream_fn=_fake_stream(["好"]),
    )
    assert result.tool_calls == 0  # 未执行任何真实工具
    assert result.reply == "好"


async def test_first_decision_failure_degrades_to_plain_stream(monkeypatch):
    async def broken_chat(messages, tools):
        raise LlmUnavailable("decision down")

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "陪我聊天"}],
        runtime_ctx={},
        on_delta=None,
        chat_fn=broken_chat,
        stream_fn=_fake_stream(["我在呢，", "慢慢说。"]),
    )
    assert result.reply == "我在呢，慢慢说。"
    assert result.tool_calls == 0


# ---- guard 接线（Day5 框架零改动生效）----


async def test_guard_same_tool_circuit_within_turn(monkeypatch):
    _install_fake_tools(monkeypatch, calls := [])

    tc = [{"id": "c1", "name": "weather", "arguments": {}}]

    async def fake_chat(messages, tools):
        # 每轮决策都要求调 weather（模型抽搐场景）
        return _decision(tool_calls=list(tc)) if len(messages) < 12 else _decision(content=None)

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "x"}],
        runtime_ctx={},
        on_delta=None,
        chat_fn=fake_chat,
        stream_fn=_fake_stream(["好"]),
    )
    assert len(calls) == 2  # 同 Tool 窗口内第 3 次起被拒（前 2 次放行）
    assert result.guard_blocked >= 1


async def test_guard_turn_budget_and_reset(monkeypatch):
    _install_fake_tools(monkeypatch, calls := [])

    async def fake_chat(messages, tools):
        if len(calls) < 10:  # 持续要求调同工具 → 预算耗尽后观察全为拒绝
            return _decision(
                tool_calls=[{"id": f"c{len(calls)}", "name": "weather", "arguments": {}}]
            )
        return _decision(content=None)

    agent = CompanionAgent()
    result = await agent.run_turn(
        [{"role": "user", "content": "x"}],
        runtime_ctx={},
        on_delta=None,
        chat_fn=fake_chat,
        stream_fn=_fake_stream(["好"]),
    )
    assert len(calls) <= 7  # 单轮全局预算 8：前 7 次放行
    assert result.guard_blocked >= 1
    # reset_turn 后窗口恢复（验收项 11：窗口恢复）
    agent.guard.reset_turn()
    assert agent.guard.record("weather").allowed is True


# ---- chat_with_tools 解析 ----


def _tool_call_payload():
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "weather", "arguments": '{"latitude": 39.9}'},
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 33},
    }


async def test_chat_with_tools_parses_openai_style_response(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://mock", raising=False)
    monkeypatch.setattr(get_settings(), "llm_model", "test-model", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'"tools"' in request.content and b'"tool_choice"' in request.content
        return httpx.Response(200, json=_tool_call_payload())

    transport = httpx.MockTransport(handler)
    result = await chat_with_tools(
        [{"role": "user", "content": "今天天气怎么样"}],
        [{"name": "weather", "description": "d", "parameters": {}}],
        transport=transport,
    )
    assert result["tool_calls"][0]["name"] == "weather"
    assert result["tool_calls"][0]["arguments"] == {"latitude": 39.9}
    assert result["raw_tool_calls"][0]["id"] == "call_1"
    assert result["usage"]["total_tokens"] == 33


async def test_chat_with_tools_bad_arguments_default_empty(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://mock", raising=False)
    monkeypatch.setattr(get_settings(), "llm_model", "test-model", raising=False)

    payload = _tool_call_payload()
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{bad json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = await chat_with_tools(
        [{"role": "user", "content": "x"}],
        [{"name": "weather", "description": "d", "parameters": {}}],
        transport=httpx.MockTransport(handler),
    )
    assert result["tool_calls"][0]["arguments"] == {}  # 坏参不致命


async def test_local_time_tool_registered_and_schema_clean():
    from app.tools.registry import register_default_tools

    register_default_tools()
    spec = registry.get_tool("local_time")
    assert spec is not None and spec.handler is not None
    assert "elder_id" not in json.dumps(spec.schema)
    data = await spec.handler({}, {"elder_id": "e1"})
    assert "点" in data["time_text"] and "月" in data["date_text"]
