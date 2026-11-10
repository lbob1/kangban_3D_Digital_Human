"""工具注册表（Day6 P1-6，TDD §5.3 / 主文档 §4.4）。

口径：
- `name → {handler, schema, description}`；规则快路径、System One 路由与
  主控 Agent（P1-7）共用同一注册表；
- handler 签名统一 `async (args: dict, runtime_ctx: dict) -> dict`——elder_id
  由调用方经 runtime_ctx 服务端注入（AGENTS 安全禁区 8），**Tool schema 不含
  elder_id**（TDD §5.3，schema 人工审查后才可注册）；
- Day7+ 加 Tool 只加注册项（register_default_tools 追加一行 + 模块文件）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

ToolHandler = Callable[[dict, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    """一个可被路由层/主控 Agent 调用的工具。"""

    name: str
    description: str
    schema: dict = field(default_factory=dict)  # JSON Schema（人审后注册，禁含 elder_id）
    handler: ToolHandler | None = None


_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    """注册/覆盖一个工具；schema 必须已过人工审查（高风险清单 app/tools/**）。"""
    _REGISTRY[spec.name] = spec


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def default_runtime_ctx(elder_id: str | None = None) -> dict:
    """服务端 Runtime Context：elder_id 只能来自服务端（AGENTS 安全禁区 8）。"""
    return {"elder_id": elder_id}


def register_default_tools() -> None:
    """Day6 默认工具集；幂等（重复注册同名为覆盖）。"""
    from app.tools import emergency_report, local_time, medication_confirm, weather_mcp

    register_tool(weather_mcp.build_tool_spec())
    register_tool(local_time.build_tool_spec())
    register_tool(emergency_report.build_tool_spec())  # Day6+ 第四道防线（宁多勿漏）
    register_tool(medication_confirm.build_tool_spec())  # Day7 步7：用药确认复入同一图
