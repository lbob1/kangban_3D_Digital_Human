"""MCP 家属消息 Tool 客户端与注册表 handler（Day9 步 6，主文档 §4.3）。

结构：
- FamilyNotifyMcpClient：仿 workorder_mcp——stdio 子进程会话、单次 3s 超时、
  重试 2 次（指数退避）→ 失败返回 None（调用方降级结构化日志，不静默）；
- notify_family_handler：注册表标准 handler（args/runtime_ctx → dict）——
  elder_id 只取 runtime_ctx（服务端注入，AGENTS 禁区 8）；MCP 失败/关闭 →
  结构化日志降级（家属消息不丢失：离线兜底=落日志供值班排查）；
- TOOL_SCHEMA 人审通过后注册（registry.register_default_tools）：**无 elder_id**
  （主控模型「告诉我儿子…」类意图只提供 message/severity 两个业务参数）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("tools.family_notify")

_REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = _REPO_ROOT / "mcp-servers" / "family-notify" / "server.py"

CLIENT_RETRY_ATTEMPTS = 2  # 调用失败后再试次数（指数退避 0.3s/0.6s）
CLIENT_BACKOFF_BASE_S = 0.3
CALL_TIMEOUT_S = 3.0  # 主文档 §4.3：单次调用客户端超时 3s
MESSAGE_MAX = 200

VALID_SEVERITIES = ("info", "warning", "urgent")


class FamilyNotifyMcpClient:
    """stdio 子进程 MCP 客户端：懒启动 + 会话复用 + 重试 + 失败返回 None。"""

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command or [sys.executable, str(SERVER_PATH)]
        self._stack: contextlib.AsyncExitStack | None = None
        self._session = None

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self._command[0], args=self._command[1:])
        self._stack = contextlib.AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        return session

    async def _teardown(self) -> None:
        self._session = None
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._stack = None

    async def push_family_message(
        self, *, elder_id: str, message: str, severity: str
    ) -> dict[str, Any] | None:
        """调用 MCP push_family_message；重试 2 次后仍失败返回 None（调用方降级）。"""
        last_error: Exception | None = None
        for attempt in range(CLIENT_RETRY_ATTEMPTS + 1):
            if attempt > 0:
                await asyncio.sleep(CLIENT_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            try:
                session = await self._ensure_session()
                result = await asyncio.wait_for(
                    session.call_tool(
                        "push_family_message",
                        {"elder_id": elder_id, "message": message, "severity": severity},
                    ),
                    timeout=CALL_TIMEOUT_S,
                )
                if result.isError or not result.content:
                    raise RuntimeError("mcp tool returned error/empty")
                return json.loads(result.content[0].text)
            except Exception as exc:  # noqa: BLE001 —— 任何失败都走重试→返回 None
                last_error = exc
                await self._teardown()  # 会话可能已损坏，整体重建
        logger.warning(
            "family notify mcp unavailable",
            extra={
                "event": "family_notify_mcp_fallback",
                "error": f"{type(last_error).__name__}: {last_error}"[:200],
            },
        )
        return None

    async def close(self) -> None:
        await self._teardown()


# ---- 进程级单例（会话跨轮复用，避免每轮拉子进程）----

_client: FamilyNotifyMcpClient | None = None


def get_family_notify_client() -> FamilyNotifyMcpClient:
    global _client
    if _client is None:
        cmd = get_settings().family_notify_mcp_command
        _client = FamilyNotifyMcpClient(cmd.split() if cmd else None)
    return _client


# ---- 注册表标准 handler（args/runtime_ctx → dict）----


async def notify_family_handler(args: dict, runtime_ctx: dict) -> dict[str, Any]:
    """主控 LLM「告诉我儿子…」类意图 → 经 MCP 推家属消息（教学 mock 微信）。

    elder_id 只取 runtime_ctx（schema 禁含 elder_id——模型传参被忽略，禁区 8）；
    未验证长者拒绝；MCP 失败降级结构化日志（可见降级不静默，不丢业务语义）。
    """
    message = str(args.get("message", "")).strip()[:MESSAGE_MAX]
    severity = args.get("severity", "info")
    severity = severity if severity in VALID_SEVERITIES else "info"
    if not message:
        return {"notified": False, "reason": "empty_message"}

    elder_id = (runtime_ctx or {}).get("elder_id")
    if not elder_id:
        logger.warning(
            "family notify without verified elder, dropped",
            extra={"event": "family_notify_dropped", "severity": severity},
        )
        return {"notified": False, "reason": "elder_not_verified"}

    if get_settings().family_notify_mcp_enabled:
        try:
            result = await get_family_notify_client().push_family_message(
                elder_id=str(elder_id), message=message, severity=severity
            )
        except Exception as exc:  # noqa: BLE001 —— 客户端意外异常同样降级（不静默）
            result = None
            logger.warning(
                "family notify mcp client raised",
                extra={"event": "family_notify_mcp_fallback",
                       "error": f"{type(exc).__name__}: {exc}"[:200]},
            )
        if result is not None:
            logger.info(
                "family message via mcp",
                extra={
                    "event": "family_notify_mcp_result",
                    "elder_id": str(elder_id),
                    "severity": severity,
                    "status": result.get("status"),
                    "reason": result.get("reason"),
                },
            )
            notified = result.get("status") == "sent"
            out: dict[str, Any] = {"notified": notified, "status": result.get("status")}
            if result.get("reason"):
                out["reason"] = result["reason"]
            return out
        logger.warning(
            "family notify unavailable, degraded to structured log only",
            extra={"event": "family_notify_degraded", "elder_id": str(elder_id),
                   "severity": severity, "message_chars": len(message)},
        )

    # 降级路径（开关关闭/MCP 不可用）：结构化日志留痕（教学口径不丢语义）
    logger.info(
        "family message degraded to log channel",
        extra={
            "event": "family_notify_log_only",
            "elder_id": str(elder_id),
            "severity": severity,
            "message_chars": len(message),
        },
    )
    return {"notified": False, "status": "log_only"}


# ---- 注册表 schema（2026-11-12 人工审查通过后注册；禁含 elder_id）----

TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "要转达给家属的留言内容（≤200字），如「告诉儿子我今晚想他了」",
        },
        "severity": {
            "type": "string",
            "enum": list(VALID_SEVERITIES),
            "description": "紧急程度：info 日常留言、warning 需要家属关注、urgent 紧急情况",
        },
    },
    "required": ["message", "severity"],
    # elder_id 不在 schema：服务端 Runtime Context 注入（AGENTS 禁区 8）
}


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="notify_family",
        description=(
            "把长者的留言转达给家属（如「告诉我儿子我想他了」）。只推送留言本身，"
            "不查询家属资料、不发送健康明细。"
        ),
        schema=TOOL_SCHEMA,
        handler=notify_family_handler,
    )
