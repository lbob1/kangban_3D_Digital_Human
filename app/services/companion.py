"""CompanionAgent（Day6 P1-7，大纲 §4.5）：主控 Agent ReAct 骨架。

口径：
- 决策轮：非流式 chat_with_tools（带注册表工具 schema）；模型无工具意图且已出
  正文 → 直接采用（免二次调用，省时延）；有工具调用 → 逐个执行并回填观察；
- 终答轮：工具观察后 stream_chat 流式输出（人设/历史在 messages 内，复用
  voice 的 _build_system_prompt 与 5 轮历史；on_delta 回调由 voice 喂句级装配器）；
- guard 接线（Day5 框架零改动生效）：每轮 reset_turn()，每次调用前 record()——
  同 Tool 60s 窗口第 3 次拒、单轮全局预算 8 次（前 7 次放行）、窗口恢复；
- Runtime Context：elder_id 由调用方（voice，服务端）注入 runtime_ctx，本模块
  不信任模型/客户端传参（AGENTS 安全禁区 8）；
- Day6 工具面：weather（MCP 只读）+ local_time（本地确定性）——无高危副作用；
  高危副作用 HITL 确认为 Day7+（guard.py 头注释口径）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.integrations.llm import LlmUnavailable, chat_with_tools, stream_chat
from app.services.guard import MAX_TOOL_CALLS_PER_TURN, ToolCallGuard

logger = get_logger(__name__)

OBSERVATION_MAX_CHARS = 2000  # 观察回填截断（防上下文膨胀，guard.py 头注释口径）
MAX_DECISION_ROUNDS = 12  # 决策轮硬上限（模型持续要工具时强制终答，防死循环）


@dataclass
class AgentTurnResult:
    """一轮主控结果：reply 为全文；tool_calls 为实际执行的调用数。"""

    reply: str
    tool_calls: int = 0
    guard_blocked: int = 0
    rounds: int = 0
    tool_names: list[str] = field(default_factory=list)
    usage_total: dict[str, Any] = field(default_factory=dict)


class CompanionAgent:
    """单会话实例（与 VoiceSession 一对一，guard 随会话生命周期）。"""

    def __init__(self, guard: ToolCallGuard | None = None) -> None:
        self.guard = guard or ToolCallGuard()

    async def run_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        runtime_ctx: dict,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        chat_fn: Callable[..., Any] | None = None,
        stream_fn: Callable[..., Any] | None = None,
    ) -> AgentTurnResult:
        """ReAct 一轮：思考（tool_calls）→ 行动（guard+registry）→ 观察 → 终答。

        chat_fn/stream_fn 供调用方注入（voice 传其模块级引用，单测 monkeypatch 生效）；
        首轮决策调用失败（LlmUnavailable）→ 降级为「无工具直答」流式，链路不断。
        """
        from app.tools import registry as tool_registry

        chat_with_tools_fn = chat_fn or chat_with_tools
        stream_chat_fn = stream_fn or stream_chat
        specs = [s for s in tool_registry.list_tools() if s.handler is not None]
        tool_defs = [
            {"name": s.name, "description": s.description, "parameters": s.schema} for s in specs
        ]
        self.guard.reset_turn()
        work = list(messages)
        executed = 0
        blocked = 0
        tool_names: list[str] = []
        usage_acc: dict[str, Any] = {}

        started = time.perf_counter()
        try:
            decision = await chat_with_tools_fn(work, tool_defs)
        except LlmUnavailable:
            if executed == 0:
                decision = {"content": None, "tool_calls": [], "raw_tool_calls": []}
            else:
                raise
        rounds = 1
        _acc_usage(usage_acc, decision.get("usage"))
        while (
            decision["tool_calls"]
            and executed < MAX_TOOL_CALLS_PER_TURN
            and rounds < MAX_DECISION_ROUNDS
        ):
            work.append(
                {
                    "role": "assistant",
                    "content": decision.get("content") or "",
                    "tool_calls": decision["raw_tool_calls"],
                }
            )
            for tc in decision["tool_calls"]:
                observation, was_executed = await self._execute(tc, runtime_ctx)
                executed += 1 if was_executed else 0
                blocked += 0 if was_executed else 1
                if was_executed:
                    tool_names.append(tc["name"])
                work.append(
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": observation}
                )
            decision = await chat_with_tools_fn(work, tool_defs)
            rounds += 1
            _acc_usage(usage_acc, decision.get("usage"))

        content = (decision.get("content") or "").strip()
        if content and executed == 0:
            # 单轮直答：决策轮已产出正文（无工具意图），直接采用免二次流式调用
            if on_delta is not None:
                await on_delta(content)
            return AgentTurnResult(reply=content, tool_calls=0, guard_blocked=0, rounds=rounds)

        # 工具观察后流式终答（人设/历史/观察都在 work 内）
        parts: list[str] = []
        async for delta in stream_chat_fn(work):
            parts.append(delta)
            if on_delta is not None:
                await on_delta(delta)
        reply = "".join(parts).strip()
        logger.info(
            "agent turn done",
            extra={
                "event": "agent_turn",
                "rounds": rounds,
                "tool_calls": executed,
                "guard_blocked": blocked,
                "tools": ",".join(tool_names) or "-",
                "agent_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return AgentTurnResult(
            reply=reply,
            tool_calls=executed,
            guard_blocked=blocked,
            rounds=rounds,
            tool_names=tool_names,
            usage_total=usage_acc,
        )

    async def _execute(self, tc: dict, runtime_ctx: dict) -> tuple[str, bool]:
        """单次工具执行：guard.record 前置 → registry 分发 → 观察 JSON（截断）。"""
        from app.tools import registry as tool_registry

        name = tc.get("name", "")
        verdict = self.guard.record(name)
        if not verdict.allowed:
            logger.warning(
                "guard rejected tool call",
                extra={"event": "guard_tool_rejected", "tool": name, "reason": verdict.reason},
            )
            _inc_hallucination_block()  # Day10 步骤 3：幻觉工具调用拦截计数
            return json.dumps(
                {"error": "tool_rejected", "reason": verdict.reason}, ensure_ascii=False
            ), False
        spec = tool_registry.get_tool(name)
        if spec is None or spec.handler is None:
            _inc_hallucination_block()  # Day10 步骤 3：编造未注册工具名拦截计数
            return json.dumps({"error": "unknown_tool", "tool": name}, ensure_ascii=False), False
        try:
            data = await spec.handler(tc.get("arguments") or {}, runtime_ctx)
            return json.dumps(data, ensure_ascii=False, default=str)[:OBSERVATION_MAX_CHARS], True
        except Exception as exc:  # noqa: BLE001 —— 工具失败回填错误观察，模型自行兜底措辞
            logger.warning(
                "tool execution failed",
                extra={"event": "tool_exec_failed", "tool": name, "error": type(exc).__name__},
            )
            return json.dumps({"error": type(exc).__name__, "tool": name}, ensure_ascii=False), True


def _inc_hallucination_block() -> None:
    """Day10 步骤 3：幻觉工具调用拦截计数（guard 拒绝 / 未注册工具名）。

    与 eval 幻觉场景同一拦截面（escalated + tool_calls=0）；指标失败不影响拦截本身。
    """
    try:
        from app.core.metrics import inc

        inc("hallucination_block_total")
    except Exception:  # noqa: BLE001
        pass


def _acc_usage(acc: dict[str, Any], usage: dict[str, Any] | None) -> None:
    """决策轮 token 累计（成本单列计量；字段按供应商返回原样求和）。"""
    if not usage:
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            acc[key] = acc.get(key, 0) + value


# ---- 进程级单例（guard 每轮 reset，跨会话共享可接受；Day7 按会话隔离）----

_agent: CompanionAgent | None = None


def get_companion_agent() -> CompanionAgent:
    global _agent
    if _agent is None:
        _agent = CompanionAgent()
    return _agent
