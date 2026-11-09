"""MCP 天气 Tool 客户端与回复管线（Day6 P1-6，主文档 §4.4，D9 双路径汇合）。

结构：
- WeatherMcpClient：stdio 子进程会话管理（官方 mcp SDK，绑定当前事件循环），
  调用失败重试 2 次（指数退避）→ 仍失败降级本地确定性 mock（可见降级不静默）；
  server 侧另有 open-meteo 网络重试与 mock 降级（双层防线，任何一层兜底均可见）；
- weather_tool_handler：注册表标准 handler（args/runtime_ctx → 结构化数据 dict）；
- 回复管线（完整信息 + LLM 精简答复）：完整数据随 ws 事件全量下发（字幕/日志
  可见，不截断）+ 1 次受控精简 LLM 调用（短 prompt 不注入历史；LLM 不可用退
  确定性模板句）；精简调用 ≠ 升级主控（escalated_to_agent 仍 false）；
- build_weather_action_handler：级联层 handler 适配（rule/S1 双路径共用同一实例）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.llm import LlmUnavailable, stream_chat
from app.integrations.llm import is_configured as llm_is_configured
from app.router.cascade import CascadeOutcome, stream_emitter

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = _REPO_ROOT / "mcp-servers" / "weather" / "server.py"

CLIENT_RETRY_ATTEMPTS = 2  # 调用失败后再试次数（指数退避 0.3s/0.6s）
CLIENT_BACKOFF_BASE_S = 0.3
CONDENSE_MAX_CHARS = 80  # 精简答复截断保险丝（prompt 已限 1–2 短句）
_SENT_END_RE = re.compile(r"[。！？；!?;\n…]")  # 句末切分（与 voice._SentenceAssembler 同口径）


def _local_mock(latitude: float, longitude: float, reason: str) -> dict[str, Any]:
    """客户端兜底 mock（server 进程拉不起/协议失败时；server 内也有同款降级）。"""
    return {
        "latitude": latitude,
        "longitude": longitude,
        "summary": "晴",
        "temp_c": 22.0,
        "wind_kmh": 8.0,
        "humidity": 60,
        "today_max_c": 26.0,
        "today_min_c": 17.0,
        "source": "mock",
        "degraded": True,
        "note": "天气服务暂不可用，为您播报模拟数据",
        "degraded_reason": reason,
    }


class WeatherMcpClient:
    """stdio 子进程 MCP 客户端：懒启动 + 会话复用 + 重试 + mock 兜底。

    会话绑定首次调用所在的事件循环（uvicorn 运行循环，AGENTS 实时规则 1）；
    测试可注入 transport-free fake（直接替换 get_weather 方法或注入 fake session）。
    """

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

    async def get_weather(self, latitude: float, longitude: float) -> dict[str, Any]:
        """调用 MCP get_weather；重试 2 次后降级本地 mock（degraded=true 可见）。"""
        last_error: Exception | None = None
        for attempt in range(CLIENT_RETRY_ATTEMPTS + 1):
            if attempt > 0:
                await asyncio.sleep(CLIENT_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            try:
                session = await self._ensure_session()
                result = await session.call_tool(
                    "get_weather", {"latitude": latitude, "longitude": longitude}
                )
                if result.isError or not result.content:
                    raise RuntimeError("mcp tool returned error/empty")
                return json.loads(result.content[0].text)
            except Exception as exc:  # noqa: BLE001 —— 任何失败都走重试→mock 兜底
                last_error = exc
                await self._teardown()  # 会话可能已损坏，整体重建
        reason = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown"
        logger.warning(
            "weather mcp unavailable, fallback to mock",
            extra={"event": "weather_mcp_fallback", "error": reason[:200]},
        )
        return _local_mock(latitude, longitude, reason)

    async def close(self) -> None:
        await self._teardown()


# ---- 进程级单例（会话跨轮复用，避免每轮拉子进程）----

_client: WeatherMcpClient | None = None


def get_weather_client() -> WeatherMcpClient:
    global _client
    if _client is None:
        cmd = get_settings().weather_mcp_command
        _client = WeatherMcpClient(cmd.split() if cmd else None)
    return _client


# ---- 注册表标准 handler（args/runtime_ctx → dict）----


async def weather_tool_handler(args: dict, runtime_ctx: dict) -> dict[str, Any]:
    """注册表 handler：坐标取 args，缺省走 Settings 社区坐标（长者档案无城市字段）。"""
    del runtime_ctx  # 天气为只读公共数据，不使用 elder_id
    settings = get_settings()
    latitude = float(args.get("latitude", settings.weather_lat))
    longitude = float(args.get("longitude", settings.weather_lon))
    return await get_weather_client().get_weather(latitude, longitude)


TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "latitude": {"type": "number", "description": "纬度（缺省社区坐标）"},
        "longitude": {"type": "number", "description": "经度（缺省社区坐标）"},
    },
    # TDD §5.3：schema 不含 elder_id（只读公共数据，也不信任客户端身份传参）
}


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="weather",
        description="查询社区当前天气与今日温度范围（open-meteo，中文描述）",
        schema=TOOL_SCHEMA,
        handler=weather_tool_handler,
    )


# ---- 回复管线：完整信息 + LLM 精简答复（D9）----


def format_full_text(data: dict[str, Any]) -> str:
    """完整天气信息（字幕/日志全量展示，不截断）。"""
    head = "（模拟数据）" if data.get("degraded") else ""
    return (
        f"{head}天气实况：{data.get('summary', '未知')}，"
        f"气温 {data.get('temp_c')}℃，湿度 {data.get('humidity')}%，"
        f"风速 {data.get('wind_kmh')} km/h，"
        f"今日 {data.get('today_min_c')}~{data.get('today_max_c')}℃"
        f"（数据源：{data.get('source')}）"
    )


def template_reply(data: dict[str, Any]) -> str:
    """LLM 不可用时的确定性模板句（数据字段直接填入固定句式）。"""
    return (
        f"今天{data.get('summary', '天气不错')}，气温 {data.get('temp_c')} 度，"
        f"最高 {data.get('today_max_c')} 度，最低 {data.get('today_min_c')} 度。"
    )


def _condense_messages(data: dict[str, Any]) -> list[dict[str, str]]:
    """受控精简 prompt：结构化数据 + 人设口吻，不注入对话历史。"""
    prompt = (
        "你是养老社区的陪伴数字人「康伴」。根据以下天气数据，用 1~2 句口语化短句"
        "向长者播报，语气温和亲切，可给一句贴心提醒，不超过 40 字，直接输出正文。\n"
        + json.dumps(data, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": "只输出播报正文，不要任何解释、标点开头或列表。"},
        {"role": "user", "content": prompt},
    ]


async def condense_via_llm(data: dict[str, Any], *, transport=None) -> str | None:
    """1 次受控精简调用：等完整回复返回（单测/bench/无发射器场景）。"""
    if not llm_is_configured():
        return None
    parts: list[str] = []
    async for delta in stream_chat(_condense_messages(data), transport=transport):
        parts.append(delta)
        if sum(len(p) for p in parts) > CONDENSE_MAX_CHARS * 4:  # 异常长输出保险丝
            break
    reply = "".join(parts).strip()
    return reply[:CONDENSE_MAX_CHARS] if reply else None


async def condense_via_llm_streaming(
    data: dict[str, Any], on_sentence: Any
) -> str | None:
    """流式精简：凑到句末标点即发射 TTS（首句起播不等全文），返回已播全文。

    与 Day5 主控管线同口径：LLM 吐第一句就送 TTS；异常长输出保险丝同上。
    """
    if not llm_is_configured():
        return None
    spoken: list[str] = []
    buf = ""
    async for delta in stream_chat(_condense_messages(data)):
        buf += delta
        while True:
            match = _SENT_END_RE.search(buf)
            if match is None:
                break
            sentence, buf = buf[: match.end()], buf[match.end():]
            if sentence.strip():
                spoken.append(sentence)
                await on_sentence(sentence)
        if sum(len(s) for s in spoken) + len(buf) > CONDENSE_MAX_CHARS * 4:
            break
    if buf.strip():  # 冲刷无句末标点的余量
        spoken.append(buf)
        await on_sentence(buf)
    reply = "".join(spoken).strip()
    return reply[:CONDENSE_MAX_CHARS] if reply else None


async def build_weather_action_handler(rule, last_reply: str | None) -> CascadeOutcome | None:
    """级联层 handler（rule 快路径与 S1 intent=weather 共用）：Tool → 完整信息 + 精简答复。

    流式优化（POC-08 首包治理）：voice 已注入发射器时，降级提示先播、精简句
    凑到句末即逐句 TTS（不等全文生成）；无发射器（单测/bench）走整段原路径。
    """
    del rule, last_reply
    started = time.perf_counter()
    from app.tools.registry import default_runtime_ctx, get_tool

    spec = get_tool("weather")
    if spec is None or spec.handler is None:
        return None  # 工具未注册 → 级联落下一层（不静默失败于缺失配置）
    data = await spec.handler({}, default_runtime_ctx())
    full_text = format_full_text(data)
    emitter = stream_emitter.get()
    llm_reply: str | None = None
    streamed = False
    emitted: list[str] = []  # 已播全文（不变量：reply_text == 实际播出全文，供落库/字幕一致）
    condense_spoken = {"n": 0}  # 精简流实际发射句数（区分「已播一部分」与「完全没播出」）

    async def _emit(sentence: str, *, is_condense: bool = False) -> None:
        emitted.append(sentence)
        if is_condense:
            condense_spoken["n"] += 1
        await emitter(sentence)

    if emitter is not None:
        # 流式路径：降级提示先播 → 精简句凑到句末即逐句 TTS → LLM 不可用时一次性播模板
        note_text = (
            f"{data.get('note', '天气服务暂不可用，以下是模拟数据')}。"
            if data.get("degraded")
            else ""
        )
        if note_text:
            await _emit(note_text)
        try:
            llm_reply = await condense_via_llm_streaming(
                data, lambda s: _emit(s, is_condense=True)
            )
        except LlmUnavailable:
            llm_reply = None
        if llm_reply is None and condense_spoken["n"] == 0:
            # LLM 不可用/空输出且一句都没播：补播模板句（note 已单句播过，不再重复前缀）
            await _emit(template_reply(data))
        # 中途断连已播一部分：沿用已播不重说模板（Day5 主控同语义）
        reply = "".join(emitted).strip()
        streamed = True
    else:
        # 无发射器（单测/bench）：整段原路径，行为与流式改造前完全一致
        try:
            llm_reply = await condense_via_llm(data)
        except LlmUnavailable:
            llm_reply = None  # 退确定性模板句，链路不阻塞
        reply = llm_reply or template_reply(data)
        if data.get("degraded"):
            reply = f"{data.get('note', '天气服务暂不可用，以下是模拟数据')}。{reply}"
    logger.info(
        "weather answered",
        extra={
            "event": "weather_answered",
            "source": data.get("source"),
            "degraded": bool(data.get("degraded")),
            "llm_used": llm_reply is not None,  # 精简调用 token 单列计量（成本对比口径）
            "streamed": streamed,
            "reply_chars": len(reply),
            "mcp_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return CascadeOutcome(
        reply_text=reply,
        ws_events=[{"type": "weather", "data": data, "full_text": full_text}],
        tool_calls=1,
        streamed=streamed,
    )
