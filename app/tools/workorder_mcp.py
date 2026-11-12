"""MCP 建工单 Tool 客户端（Day9 步 5，主文档 §4.3）：仿 weather_mcp 模式。

结构：
- WorkorderMcpClient：stdio 子进程会话管理（官方 mcp SDK，绑定当前事件循环），
  单次调用 3s 超时；调用失败重试 2 次（指数退避）→ 仍失败返回 None，
  由调用方（emergency_report）降级回本地 ORM 原逻辑（可见降级不静默）；
- elder_id 不进 tool 业务 schema：客户端经 meta 字典附加（server 仅作归属记录，
  不作信任来源）；idempotency_key 由客户端按「主体+类目+描述摘要」确定性派生
  （同内容重放返回同工单号，新事件新工单）；
- 会话跨轮复用（进程级单例，避免每轮拉子进程，与 weather 同口径）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("tools.workorder_mcp")

_REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = _REPO_ROOT / "mcp-servers" / "workorder" / "server.py"

CLIENT_RETRY_ATTEMPTS = 2  # 调用失败后再试次数（指数退避 0.3s/0.6s）
CLIENT_BACKOFF_BASE_S = 0.3
CALL_TIMEOUT_S = 3.0  # 主文档 §4.3：单次调用客户端超时 3s


def build_idempotency_key(elder_id: str | None, category: str, description: str) -> str:
    """确定性幂等键：同（主体,类目,描述摘要）重复上报返回同工单号。"""
    digest = hashlib.sha1(f"{elder_id}|{category}|{description}".encode("utf-8")).hexdigest()[:16]
    return f"{elder_id or 'unknown'}-{category}-{digest}"


class WorkorderMcpClient:
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

    async def create_workorder(
        self,
        *,
        category: str,
        title: str,
        description: str,
        priority: str,
        elder_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """调用 MCP create_workorder；重试 2 次后仍失败返回 None（调用方降级）。"""
        last_error: Exception | None = None
        for attempt in range(CLIENT_RETRY_ATTEMPTS + 1):
            if attempt > 0:
                await asyncio.sleep(CLIENT_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            try:
                session = await self._ensure_session()
                result = await asyncio.wait_for(
                    session.call_tool(
                        "create_workorder",
                        {
                            "category": category,
                            "title": title,
                            "description": description,
                            "priority": priority,
                            "idempotency_key": idempotency_key,
                            # elder_id 经 meta 外层附加（归属记录；不进业务 schema，禁区 8）
                            "meta": {"elder_id": elder_id},
                        },
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
            "workorder mcp unavailable",
            extra={
                "event": "workorder_mcp_fallback",
                "error": f"{type(last_error).__name__}: {last_error}"[:200],
            },
        )
        return None

    async def close(self) -> None:
        await self._teardown()


# ---- 进程级单例（会话跨轮复用，避免每轮拉子进程）----

_client: WorkorderMcpClient | None = None


def get_workorder_client() -> WorkorderMcpClient:
    global _client
    if _client is None:
        cmd = get_settings().workorder_mcp_command
        _client = WorkorderMcpClient(cmd.split() if cmd else None)
    return _client
