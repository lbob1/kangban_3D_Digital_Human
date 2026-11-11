"""health_triage Tool（Day8 步3）：主控子图——老人主动主诉 → 健康决策图分级。

口径（Day8 主文档 §4.1/§4.2，大纲 8.1 红线）：
- **三通道共用一图**：主诉触发与定时外呼采集（步4）/ 端点登记（步5）同走
  app.graphs.health.run_triage，规则引擎先行，阈值永不进 prompt；
- schema 不含 elder_id（TDD §5.3）：elder_id 只来自 runtime_ctx 服务端注入
  （AGENTS 禁区 8）；模型/客户端在 args 里塞 elder_id 一律拒绝（验收 5「未验证拒绝」）；
- **话术红线**：分级结论只在后台流转，不出现在对老人语音中——工具观察只回
  固定确认话术，不回 level/reason，主控 LLM 上下文里不存在分级信息，无从说出诊断词；
- 与 safety.py 层级关系：胸口疼/晕厥等急症词在级联 ① 层直达 HITL 短路
  （Day6 架构复用，词表已对齐扩表），到不了本工具；本工具承接
  「头晕/乏力/不舒服」等非急症主诉与主动报数值。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.schemas.health_records import HealthRecordIn

logger = get_logger("tools.health_triage")

# 控制字符清洗（LLM 输出不信任，与 emergency_report 同口径）
_DESC_SANITIZE = re.compile(r"[\x00-\x1f]+")

# 固定确认话术（话术红线：零诊断词、零分级词；对 yellow/red/green 一视同仁）
ACK_TEXT = (
    "好的，我已经把您的情况记下来了，也会告诉工作人员多留意。"
    "您先歇一会儿，有事随时叫我。"
)


async def _default_notifier(state: dict) -> bool:
    """工具通道占位通知器：本层初始调用在 doctor interrupt 前暂停，理论不可达；
    确认后的真实通知由 resume 端点传入 notifier（步 6 接线）。兜底记日志不静默。"""
    logger.warning(
        "health graph notifier fired via default (unexpected)",
        extra={"event": "health_tool_default_notifier", "record_id": state.get("record_id")},
    )
    return False


async def health_triage_handler(args: dict, runtime_ctx: dict) -> dict[str, Any]:
    """主控 LLM 观察到长者主诉/报数值 → 落 health_records 并触发健康图分级。"""
    # 1) elder_id 归属：只信任服务端 Runtime Context，模型/客户端传参即拒
    if "elder_id" in args:
        logger.warning(
            "health_triage rejected client elder_id",
            extra={"event": "health_triage_rejected", "reason": "elder_id_not_allowed"},
        )
        return {"triaged": False, "reason": "elder_id_not_allowed"}
    elder_id = runtime_ctx.get("elder_id")
    if not elder_id:
        # 长者未声纹验证：记录不可归户，不落库（与 emergency_report 同口径）
        logger.info(
            "health_triage without verified elder, dropped",
            extra={"event": "health_triage_dropped", "reason": "elder_not_verified"},
        )
        return {"triaged": False, "reason": "elder_not_verified"}

    # 2) 契约校验：与登记端点同边界（收缩 50–250/舒张 30–150/睡眠 0–1440/情绪 1–5）
    complaint = _DESC_SANITIZE.sub(" ", str(args.get("chief_complaint") or "")).strip()[:200]
    try:
        payload = HealthRecordIn(
            bp_systolic=args.get("bp_systolic"),
            bp_diastolic=args.get("bp_diastolic"),
            sleep_minutes=args.get("sleep_minutes"),
            sleep_awakenings=args.get("sleep_awakenings"),
            mood=args.get("mood"),
            chief_complaint=complaint or None,
        )
    except ValidationError:
        return {"triaged": False, "reason": "invalid_args"}

    try:
        elder_uuid = uuid.UUID(str(elder_id))
    except ValueError:
        return {"triaged": False, "reason": "elder_not_verified"}

    # 3) 落记录（三通道同表同源标记）→ 走同一张健康图（大纲 8.1 红线）
    from app.graphs.health import run_triage
    from app.models import Elder, HealthRecord

    async with get_session_factory() as db:
        elder: Elder | None = await db.get(Elder, elder_uuid)
        if elder is None:
            return {"triaged": False, "reason": "elder_not_found"}
        record = HealthRecord(
            elder_id=elder.elder_id,
            recorded_at=datetime.now(UTC),
            bp_systolic=payload.bp_systolic,
            bp_diastolic=payload.bp_diastolic,
            sleep_minutes=payload.sleep_minutes,
            sleep_awakenings=payload.sleep_awakenings,
            mood=str(payload.mood) if payload.mood is not None else None,
            chief_complaint=payload.chief_complaint,
            source="voice_complaint",
        )
        db.add(record)
        await db.flush()
        await run_triage(
            db,
            record=record,
            elder_name=elder.name,
            notifier=_default_notifier,
            checkpoint_path=runtime_ctx.get("checkpoint_path"),  # 测试注入用；生产走 Settings 默认
        )
        await db.commit()
        record_id = str(record.record_id)

    logger.info(
        "health triaged via voice complaint",
        extra={"event": "health_triage_tool", "record_id": record_id},
    )
    # 观察不带 level/reason（话术红线）：分级只留 DB 与 graph state（可 get_state 审计）
    return {"triaged": True, "record_id": record_id, "ack": ACK_TEXT}


TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "chief_complaint": {
            "type": "string",
            "description": "长者主诉原话（≤200 字），如「我今天有点头晕」「夜里没睡好」",
        },
        "bp_systolic": {
            "type": "integer", "minimum": 50, "maximum": 250,
            "description": "收缩压 mmHg（长者主动报数时填）",
        },
        "bp_diastolic": {
            "type": "integer", "minimum": 30, "maximum": 150,
            "description": "舒张压 mmHg（长者主动报数时填）",
        },
        "sleep_minutes": {
            "type": "integer", "minimum": 0, "maximum": 1440,
            "description": "睡眠时长分钟（长者主动提起时填）",
        },
        "sleep_awakenings": {
            "type": "integer", "minimum": 0, "maximum": 20,
            "description": "夜间醒来次数（长者主动提起时填）",
        },
        "mood": {
            "type": "integer", "minimum": 1, "maximum": 5,
            "description": "情绪自评 1–5 分（长者主动提起时填）",
        },
    },
    "required": ["chief_complaint"],
    # TDD §5.3：schema 不含 elder_id（服务端 Runtime Context 注入）
}


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="health_triage",
        description=(
            "长者诉说身体不适或主动报告健康数值（头晕/乏力/没睡好/血压血糖等，"
            "非急症）时调用：系统自动记录并分级，无需向长者解释结果。"
            "调用后按返回的 ack 话术安抚即可，不得自行描述任何分级或病情结论。"
        ),
        schema=TOOL_SCHEMA,
        handler=health_triage_handler,
    )
