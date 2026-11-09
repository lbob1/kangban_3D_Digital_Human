"""LLM 流式客户端（System Two：DeepSeek/Qwen 兼容 API，OpenAI chat/completions SSE）。

与 app/services/ai_analysis.py（非流式）共用同一组 Settings（llm_api_key /
llm_base_url / llm_model / llm_timeout_seconds）。语音链路时延敏感：失败不重试，
任何未配置/网络/协议错误统一抛 LlmUnavailable，由上层可见降级到模板回复
（AGENTS 规则：缺配置时应用照常启动）。
Day6 P1-7：新增非流式 chat_with_tools（OpenAI 兼容 tools 参数，百炼 qwen
function calling）；既有 stream_chat 接口与行为零改动。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class LlmUnavailable(Exception):
    """LLM 未配置或调用失败；调用方必须降级，不得中断通话。"""


def is_configured() -> bool:
    s = get_settings()
    return bool(s.llm_api_key and s.llm_base_url and s.llm_model)


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[str]:
    """流式调用 chat/completions，逐段产出 delta 文本。

    transport 参数仅供测试注入 httpx.MockTransport；生产走真实网络。
    """
    s = get_settings()
    if not is_configured():
        raise LlmUnavailable("LLM 未配置（llm_api_key/llm_base_url/llm_model 缺失）")
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    base_body = {"model": s.llm_model, "messages": messages, "stream": True}
    # Qwen3 等思考型模型默认开思考模式，首 token 延迟 3s+（语音链路不可接受）：
    # 显式关闭；服务端不认识该字段返回 400 时去掉重试一次（Day5 实测优化）。
    attempt_bodies = [{**base_body, "enable_thinking": False}, base_body]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=s.llm_timeout_seconds, transport=transport) as client:
            for attempt, req_body in enumerate(attempt_bodies):
                async with client.stream(
                    "POST", url, json=req_body, headers={"Authorization": f"Bearer {s.llm_api_key}"}
                ) as resp:
                    if resp.status_code != 200:
                        # 不读 body 细节，只记状态码（避免密钥/报文入日志）
                        if resp.status_code == 400 and attempt == 0:
                            logger.warning(
                                "llm rejected enable_thinking, retrying without it",
                                extra={"event": "llm_extra_field_rejected"},
                            )
                            continue
                        raise LlmUnavailable(f"LLM HTTP {resp.status_code}")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            logger.warning("忽略无法解析的 LLM SSE 分片")
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue  # Qwen 首个分片常只含 role/usage，无正文，静默跳过
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield delta
                    return  # 流正常结束
    except LlmUnavailable:
        raise
    except httpx.HTTPError as exc:
        logger.warning(
            "llm stream failed",
            extra={
                "event": "llm_stream_failed",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            },
        )
        raise LlmUnavailable(f"LLM 调用失败：{type(exc).__name__}") from exc


# ---- Day6 P1-7：非流式 function calling（CompanionAgent 决策轮专用）----


async def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """非流式带工具决策调用（OpenAI 兼容 tools 参数）。

    返回：
    - content: 模型正文（无工具意图时即最终回答，可能为 None）
    - tool_calls: [{"id", "name", "arguments"(dict)}]（解析后的工具调用列表）
    - raw_tool_calls: 原始 tool_calls（回填 assistant 消息用，含索引等字段）
    - usage: token 用量（可用的原样透传，供成本单列计量）

    失败口径与 stream_chat 一致：统一 LlmUnavailable，不重试。
    """
    s = get_settings()
    if not is_configured():
        raise LlmUnavailable("LLM 未配置（llm_api_key/llm_base_url/llm_model 缺失）")
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    tool_defs = [{"type": "function", "function": t} for t in tools]
    base_body: dict[str, Any] = {
        "model": s.llm_model,
        "messages": messages,
        "tools": tool_defs,
        "tool_choice": "auto",
        "stream": False,
    }
    attempt_bodies = [{**base_body, "enable_thinking": False}, base_body]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=s.llm_timeout_seconds, transport=transport) as client:
            payload: dict[str, Any] | None = None
            for attempt, req_body in enumerate(attempt_bodies):
                resp = await client.post(
                    url, json=req_body, headers={"Authorization": f"Bearer {s.llm_api_key}"}
                )
                if resp.status_code != 200:
                    if resp.status_code == 400 and attempt == 0:
                        logger.warning(
                            "llm rejected enable_thinking, retrying without it",
                            extra={"event": "llm_extra_field_rejected"},
                        )
                        continue
                    raise LlmUnavailable(f"LLM HTTP {resp.status_code}")
                payload = resp.json()
                break
            if payload is None:
                raise LlmUnavailable("LLM 决策调用无可用响应")
    except LlmUnavailable:
        raise
    except httpx.HTTPError as exc:
        logger.warning(
            "llm tools call failed",
            extra={
                "event": "llm_tools_failed",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            },
        )
        raise LlmUnavailable(f"LLM 调用失败：{type(exc).__name__}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise LlmUnavailable("LLM 决策响应缺少 choices")
    message = choices[0].get("message") or {}
    raw = message.get("tool_calls") or []
    tool_calls = []
    for tc in raw:
        fn = tc.get("function") or {}
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError:
            arguments = {}  # 坏参不致命：执行侧按空参处理并回填观察
        tool_calls.append(
            {"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": arguments}
        )
    return {
        "content": message.get("content"),
        "tool_calls": tool_calls,
        "raw_tool_calls": raw,
        "usage": payload.get("usage"),
    }
