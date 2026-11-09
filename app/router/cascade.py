"""级联路由（Day6 P1-5，大纲 §4.1）：安全 → 规则 → System One → 主控。

顺序铁律（违反即架构错误）：
- ① safety.check_sensitive 确定性词表：命中即一票否决——安抚话术 + workorders
  (type=sensitive_alert, status=open) 落库，绝不调主控、绝不让 LLM 生成回复；
- ② rules.match_rule：命中且有确定性 handler → 直达（「停」零模型）；
  命中但 handler 未实装（weather_fast_path，P1-6 接 MCP）→ 落到下一层；
- ③ System One：高置信（≥0.85）路由到对应 handler；低置信/域外 → 受控澄清反问，
  不硬猜不升级；不可用/超时 → 记 degraded 跳 ④（词表仍在 ① 兜底）；
- ④ 主控 Agent：本层只产出 escalated_to_agent=True 的决策，实际生成回复
  由 voice.py 既有 LLM/模板管线承担（P1-7 CompanionAgent 接入点）。

决策留痕：每轮 1 行 decision_logs（input_text 落库前脱敏：身份证/剂量打码）。
本模块只做决策与话术/事件产出；落库、WS 发送、TTS 由调用方（voice.py）执行。
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.config import get_settings
from app.core.logging import get_logger
from app.router.rules import RULES, RuleMatch, match_rule
from app.router.safety import COMFORT_TEXT, check_sensitive
from app.system_one.provider_laya import LayaProvider, SystemOneUnavailable
from app.system_one.questions import IntentLabel, SystemOneResult

logger = get_logger(__name__)

# 低置信/域外澄清反问（受控话术，不硬猜不升级）
CLARIFY_TEXT = "不好意思，我没太听明白您的意思，您能换个说法再说一遍吗？"
# repeat_last 无历史可重复时的兜底话术
REPEAT_EMPTY_TEXT = "刚才那条我没记下来，您再问我一遍好不好？"

# 规则动作 → WS 下行事件（前端 P2-9 加 volume/stop_playback 分支）
ACTION_WS_EVENTS: dict[str, list[dict]] = {
    "stop_playback": [{"type": "stop_playback"}],
    "volume_up": [{"type": "volume", "direction": "up"}],
    "volume_down": [{"type": "volume", "direction": "down"}],
}

# System One 高置信意图 → 规则层同名 handler（D9 双路径汇合：weather 同一管线）
S1_INTENT_HANDLERS: dict[IntentLabel, str] = {
    IntentLabel.WEATHER: "weather_fast_path",  # P1-6 接 MCP 后双路径汇合
    IntentLabel.MEDICATION: "medication_preview",
    IntentLabel.OPERA: "opera_preview",
}


@dataclass
class CascadeOutcome:
    """一轮决策结果：字段与 decision_logs 一一对应，另附回复/事件给调用方。"""

    route_layer: str = ""  # safety | rule | system_one | agent（handler 占位实例可为空）
    intent: str | None = None
    confidence: float | None = None
    latency_ms: float = 0.0
    model_version: str | None = None
    tool_calls: int = 0
    escalated_to_agent: bool = False
    degraded: bool = False
    sensitive_hit: str | None = None
    # 以下字段不落库，仅供调用方执行
    reply_text: str | None = None  # 需 TTS 播报的回复（None=纯事件，如「停」）
    ws_events: list[dict] = field(default_factory=list)
    create_workorder: bool = False  # 敏感命中 → 调用方落 workorders 工单


# ---- input_text 脱敏（落库前；身份证/剂量类模式打码）----

_ID_CARD_RE = re.compile(r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-2]\d|3[01])\d{3}[\dXx]")
_DOSE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|毫克|ml|毫升|片|粒|单位|iu)", re.IGNORECASE)


def redact_input(text: str) -> str:
    """身份证号保留前 2 后 1，剂量数字打码为 *。"""
    redacted = _ID_CARD_RE.sub(lambda m: m.group()[:2] + "*" * 13 + m.group()[-1], text)
    redacted = _DOSE_RE.sub(lambda m: re.sub(r"\d", "*", m.group()), redacted)
    return redacted


# ---- 决策器 ----


class CascadeRouter:
    """三级级联决策器：decide() 纯决策（无 IO），handler 注册制便于单测注入。"""

    def __init__(
        self,
        system_one: LayaProvider | None = None,
        handlers: dict[str, str] | None = None,
    ) -> None:
        # handler 表：action_id → 确定性回复文本或可调用（P1-6 天气 handler 注入 MCP 管线）
        self._handlers: dict[str, object] = dict(handlers or {})
        self._system_one = system_one

    # ---- handler 注册（P1-6 天气 MCP、Day7 媒体接管时注册）----

    def register_handler(self, action: str, handler: object) -> None:
        self._handlers[action] = handler

    # ---- System One 惰性接入 ----

    def attach_system_one(self, provider: LayaProvider) -> None:
        self._system_one = provider

    # ---- 各层执行 ----

    async def decide(self, text: str, *, last_reply: str | None = None) -> CascadeOutcome:
        """级联决策入口：顺序铁律 安全→规则→S1→主控，任何路径恰好命中一层。"""
        t0 = time.perf_counter()

        # ① 安全一票否决
        hit = check_sensitive(text)
        if hit is not None:
            return self._outcome(
                t0,
                route_layer="safety",
                sensitive_hit=",".join(hit.types),
                reply_text=COMFORT_TEXT,
                create_workorder=True,
            )

        # ② 规则层
        rule = match_rule(text)
        if rule is not None:
            handled = await self._run_handler(rule, last_reply)
            if handled is not None:  # handler 未实装（天气）→ 落下一层
                return self._outcome(
                    t0,
                    route_layer="rule",
                    intent=rule.action,
                    latency_ms=rule.latency_ms,
                    reply_text=handled.reply_text,
                    ws_events=handled.ws_events or ACTION_WS_EVENTS.get(rule.action, []),
                    tool_calls=handled.tool_calls,
                )

        # ③ System One（未配置/不可用 → degraded 跳 ④，词表仍在 ① 兜底）
        s1 = self._system_one
        if s1 is None:
            return self._to_agent(t0)
        try:
            result = await s1.classify(text)
        except SystemOneUnavailable as exc:
            logger.warning(
                "system_one unavailable, skip to agent",
                extra={"event": "system_one_unavailable", "reason": exc.reason},
            )
            return self._to_agent(t0, degraded=True)
        return await self._apply_system_one(t0, result, last_reply)

    async def _apply_system_one(
        self, t0: float, result: SystemOneResult, last_reply: str | None
    ) -> CascadeOutcome:
        """③ 命中后的分支：澄清 / handler 直达 / 升级主控。"""
        common = {
            "intent": result.intent.value,
            "confidence": result.confidence,
            "model_version": result.model_version,
        }
        # 低置信或域外 → 受控澄清反问，不硬猜不升级
        if result.needs_clarification or result.is_out_of_domain:
            return self._outcome(t0, route_layer="system_one", reply_text=CLARIFY_TEXT, **common)
        # 高置信且映射到确定性 handler → 路由同 handler（S1→RULE 执行侧）
        action = S1_INTENT_HANDLERS.get(result.intent)
        if action is not None:
            rule = RuleMatch(
                rule_id=f"s1_{result.intent.value}", action=action, reply_text=None, latency_ms=0.0
            )
            handled = await self._run_handler(rule, last_reply)
            if handled is not None:
                return self._outcome(
                    t0,
                    route_layer="system_one",
                    reply_text=handled.reply_text,
                    ws_events=handled.ws_events,
                    tool_calls=handled.tool_calls,
                    **common,
                )
        # chat/health/movie 等会话型意图 → 升级主控（意图信号随行留痕）
        return self._to_agent(t0, **common)  # type: ignore[arg-type]

    def _to_agent(
        self, t0: float, *, degraded: bool = False, **common: object
    ) -> CascadeOutcome:
        """④ 主控：本层不生成回复，交 voice.py 既有管线（P1-7 接 CompanionAgent）。"""
        return self._outcome(
            t0,
            route_layer="agent",
            escalated_to_agent=True,
            degraded=degraded,
            **common,  # type: ignore[arg-type]
        )

    # ---- handler 执行（同步话术或异步管线均支持；P1-6 天气为 async MCP 管线）----

    async def _run_handler(self, rule: RuleMatch, last_reply: str | None) -> CascadeOutcome | None:
        handler = self._handlers.get(rule.action)
        if handler is None:
            return None  # handler 未实装 → 调用方落下一层
        if isinstance(handler, CascadeOutcome):
            return handler  # 静态话术/事件占位（farewell/预告/停/音量）
        if callable(handler):
            result = handler(rule, last_reply)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, CascadeOutcome) else None
        return None

    # ---- decision_logs 组装 ----

    def _outcome(self, t0: float, **kwargs: object) -> CascadeOutcome:
        outcome = CascadeOutcome(**kwargs)  # type: ignore[arg-type]
        if outcome.latency_ms == 0.0:
            outcome.latency_ms = (time.perf_counter() - t0) * 1000
        return outcome


# ---- 内置确定性 handler 表（register 顺序即 cascade 初始化口径）----


def _tell_time_handler(rule: RuleMatch, last_reply: str | None) -> CascadeOutcome:
    del rule, last_reply
    now = datetime.now()
    return CascadeOutcome(reply_text=f"现在是{now:%H点%M分}。")


def _repeat_last_handler(rule: RuleMatch, last_reply: str | None) -> CascadeOutcome:
    del rule
    return CascadeOutcome(reply_text=last_reply if last_reply else REPEAT_EMPTY_TEXT)


def _register_default_handlers(router: CascadeRouter) -> None:
    router.register_handler("tell_time", _tell_time_handler)
    router.register_handler("repeat_last", _repeat_last_handler)
    for action in ("farewell", "media_next_preview", "opera_preview", "medication_preview"):
        rule = next(r for r in RULES if r.action == action)
        router.register_handler(action, CascadeOutcome(reply_text=rule.reply_text))
    # stop_playback / volume_* 纯事件：handler 表有占位（非 None 即视为已实装）
    for action in ("stop_playback", "volume_up", "volume_down"):
        router.register_handler(
            action, CascadeOutcome(reply_text=None, ws_events=ACTION_WS_EVENTS[action])
        )


# ---- 进程级单例 ----

_router: CascadeRouter | None = None


def get_cascade_router() -> CascadeRouter:
    """进程级路由器：按 Settings 决定是否挂 laya（未启用时 ②→④，应用照常启动）。"""
    global _router
    if _router is None:
        _router = CascadeRouter()
        _register_default_handlers(_router)
        _attach_weather_tool(_router)
        settings = get_settings()
        if settings.system_one_provider == "laya":
            provider = LayaProvider(
                timeout_ms=settings.system_one_timeout_ms,
                confidence_threshold=settings.system_one_confidence_threshold,
            )
            provider.warmup()  # 后台线程加载，未就绪期间 classify 抛 Unavailable → degraded
            _router.attach_system_one(provider)
            logger.info(
                "system one attached",
                extra={"event": "cascade_system_one_attached", "provider": "laya"},
            )
    return _router


def _attach_weather_tool(router: CascadeRouter) -> None:
    """注册天气 Tool 并把双路径 action 指到同一 handler（D9 汇合；失败不阻断启动）。"""
    try:
        from app.tools.registry import register_default_tools
        from app.tools.weather_mcp import build_weather_action_handler

        register_default_tools()
        router.register_handler("weather_fast_path", build_weather_action_handler)
        logger.info("weather tool attached", extra={"event": "cascade_weather_attached"})
    except Exception as exc:  # noqa: BLE001 —— 工具缺失走可见降级（落主控），不静默
        logger.warning(
            "weather tool attach failed",
            extra={"event": "cascade_weather_attach_failed", "error": str(exc)[:200]},
        )


# ---- 落库辅助（调用方执行；失败不阻断通话，与打点同口径）----


async def persist_decision_log(
    db_factory,
    outcome: CascadeOutcome,
    *,
    text: str,
    device_id: UUID | None,
    elder_id: UUID | None,
    thread_id: UUID | None = None,
) -> None:
    """每轮 1 行 decision_logs；input_text 脱敏后落库；失败只记日志。"""
    if db_factory is None:
        return
    try:
        from app.models.decision_log import DecisionLog

        async with db_factory() as db:
            db.add(
                DecisionLog(
                    thread_id=thread_id,
                    device_id=device_id,
                    elder_id=elder_id,
                    route_layer=outcome.route_layer,
                    intent=outcome.intent,
                    confidence=outcome.confidence,
                    latency_ms=round(outcome.latency_ms, 2),
                    model_version=outcome.model_version,
                    tool_calls=outcome.tool_calls,
                    escalated_to_agent=outcome.escalated_to_agent,
                    degraded=outcome.degraded,
                    sensitive_hit=outcome.sensitive_hit,
                    input_text=redact_input(text)[:500],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 —— 留痕失败不影响通话
        logger.exception("decision log persist failed")


async def persist_sensitive_workorder(
    db_factory,
    outcome: CascadeOutcome,
    *,
    elder_id: UUID | None,
    text: str,
) -> None:
    """敏感命中 → workorders 工单（type=sensitive_alert, status=open, priority=urgent）。

    elder_id 缺失（未声纹验证）时不可落工单（workorders.elder_id NOT NULL），
    只留 decision_logs 行（safety 层始终落）；HITL 队列以工单为准。
    """
    if db_factory is None or not outcome.create_workorder:
        return
    if elder_id is None:
        logger.warning(
            "sensitive hit without verified elder, workorder skipped",
            extra={"event": "sensitive_workorder_skipped"},
        )
        return
    try:
        from app.models.enums import WorkorderPriority, WorkorderStatus
        from app.models.workorder import Workorder

        async with db_factory() as db:
            db.add(
                Workorder(
                    elder_id=elder_id,
                    type="sensitive_alert",
                    status=WorkorderStatus.OPEN,
                    priority=WorkorderPriority.URGENT,
                    title=f"敏感事件：{'、'.join((outcome.sensitive_hit or 'unknown').split(','))}",
                    description=redact_input(text)[:500],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 —— 工单失败不影响安抚话术先行
        logger.exception("sensitive workorder persist failed")
