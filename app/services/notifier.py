"""双通道通知器（Day9 步 7，主文档 §4.4 / D2）：结构化日志 + WS 实时下行。

- ① 结构化日志：既有 default_notifier 语义保留（同 event 字段口径）——
  notify_ws_enabled=false 或 WS 链路任何失败时，此通道单独兜底（离线不丢通知：
  看板轮询既有路径 + 本日志供值班排查）；
- ② WS 实时下行：notify_ws.manager.broadcast 按服务端范围过滤推送（在线秒级）；
- 兼容签名 `async (state: dict) -> bool`（健康图/确认端点原 notifier 位直接换用）；
  返回值=WS 实际送达数>0（与 default_notifier 返回 False 的语义区分，仅审计用）；
- elder 端不做（老人端推送走既有 medication_reminder 等 WS 事件体系）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.notifier")

# state → WS 事件字段白名单（结构化事件不含病历明细，上下文边界）
_EVENT_FIELDS = (
    "notify_kind",
    "elder_id",
    "elder_name",
    "level",
    "reason",
    "workorder_id",
    "record_id",
    "decision_operator",
    "decision_action",
)


async def notify(state: dict) -> bool:
    """双通道通知入口：① 结构化日志（必达）+ ② WS 下行（尽力而为）。"""
    notify_kind = state.get("notify_kind")
    # ① 结构化日志（既有 default_notifier 语义，保留为降级路径）
    logger.warning(
        "health graph notifier fired",
        extra={
            "event": "health_notifier_fired",
            "record_id": state.get("record_id"),
            "notify_kind": notify_kind,
        },
    )

    # ② WS 实时下行（开关关闭=false 回 Day8 行为；任何失败不抛出，通道①已兜底）
    if not get_settings().notify_ws_enabled:
        return False
    try:
        from app.realtime.notify_ws import manager

        event: dict[str, Any] = {
            "type": "notify",
            **{k: state.get(k) for k in _EVENT_FIELDS if state.get(k) is not None},
        }
        sent = await manager.broadcast(event)
        return sent > 0
    except Exception as exc:  # noqa: BLE001 —— WS 链路失败不阻断图/端点主流程
        logger.warning(
            "notify ws broadcast failed, log channel only",
            extra={"event": "notify_ws_error", "error": f"{type(exc).__name__}: {exc}"[:200]},
        )
        return False
