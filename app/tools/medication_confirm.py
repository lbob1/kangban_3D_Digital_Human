"""medication_confirm：用药确认对话侧核心（Day7 步7）。

三个入口复用同一张图（大纲红线「定时与对话共用一图」）：
- 规则/S1 handler（「我吃过药了」确认 /「我要吃药」当日计划播报）→ 本模块；
- 主控 ReAct Tool `medication_confirm`（LLM 判断需要记录确认时调用）→ 本模块；
- 调度器（到点/退避）→ scheduler.py，不经本模块。

elder_id 只来自服务端 Runtime Context（cascade runtime_ctx_v / Tool runtime_ctx，
AGENTS 禁区 8）；未验证长者只给通用话术，不泄露任何个人用药信息。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.graphs.medication import resume_with_reply
from app.models import Elder, MedicationEvent, MedicationPlan
from app.models.enums import (
    MedicationEventStatus,
    MedicationPlanStatus,
)
from app.scheduler import ws_notifier

logger = get_logger("tools.medication_confirm")

# 未验证长者/无待确认事件时的通用话术（零个人信息）
_GENERIC_NO_EVENT = "您目前没有待确认的用药提醒，请按医嘱按时服药。"
_GENERIC_NOT_VERIFIED = "好的。请先对数字人说一声做声纹确认，我再帮您记录用药情况。"


async def _load_event_context(db: AsyncSession, event: MedicationEvent) -> dict | None:
    plan = await db.get(MedicationPlan, event.plan_id)
    elder = await db.get(Elder, event.elder_id)
    if plan is None or elder is None:
        return None
    slot = (event.thread_id or "").rsplit("-", 1)[-1] if event.thread_id else "用药"
    return {
        "elder_name": elder.name,
        "drug_name": plan.drug_name,
        "dose": plan.dose,
        "slot": slot,
    }


async def confirm_latest_event(elder_id: str | uuid.UUID) -> dict[str, Any]:
    """「我吃过药了」：找该长者最新一条 waiting_confirm 事件复入图确认。

    返回 {"confirmed": bool, "reply": 播报文本}；图复入失败不影响通用回复。
    """
    factory = get_session_factory()
    async with factory() as db:
        event = (
            await db.execute(
                select(MedicationEvent)
                .where(
                    MedicationEvent.elder_id == uuid.UUID(str(elder_id)),
                    MedicationEvent.status == MedicationEventStatus.WAITING_CONFIRM,
                )
                .order_by(MedicationEvent.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if event is None:
            return {"confirmed": False, "reply": _GENERIC_NO_EVENT}
        ctx = await _load_event_context(db, event)
        if ctx is None:
            return {"confirmed": False, "reply": _GENERIC_NO_EVENT}
        try:
            await resume_with_reply(
                db, event=event, reply="我吃过药了", notifier=ws_notifier, **ctx
            )
            await db.commit()
        except Exception:  # noqa: BLE001 —— 图复入失败可见降级为通用回复
            logger.exception("medication confirm graph resume failed")
            return {"confirmed": False, "reply": _GENERIC_NO_EVENT}
        logger.info(
            "medication confirmed via dialog",
            extra={"event": "medication_dialog_confirmed", "event_id": str(event.event_id)},
        )
        return {
            "confirmed": True,
            "reply": f"好的，已记录您已服用{ctx['drug_name']}。记得多喝水，休息一下。",
        }


async def today_plan_reply(elder_id: str | uuid.UUID) -> str:
    """「我要吃药」：当日有效计划确定性播报（零模型零 token）。"""
    factory = get_session_factory()
    async with factory() as db:
        plans = (
            (
                await db.execute(
                    select(MedicationPlan).where(
                        MedicationPlan.elder_id == uuid.UUID(str(elder_id)),
                        MedicationPlan.status == MedicationPlanStatus.ACTIVE,
                        MedicationPlan.start_date <= date.today(),
                    )
                )
            )
            .scalars()
            .all()
        )
    parts: list[str] = []
    today = date.today()
    for plan in plans:
        if plan.end_date is not None and plan.end_date < today:
            continue
        for slot in (plan.schedule or {}).get("slots", []):
            label = slot.get("label") or ""
            parts.append(f"{label}{slot.get('time', '')} {plan.drug_name} {plan.dose}")
    if not parts:
        return "您今天暂没有登记的用药安排，请按医嘱按时服药。"
    return "您今天的用药安排是：" + "；".join(parts) + "。到点我会提醒您。"


# ---- Tool（主控 ReAct；schema 人审后注册，禁含 elder_id）----


async def medication_confirm_handler(args: dict, runtime_ctx: dict) -> dict[str, Any]:
    """主控 LLM 判断长者在报告已服药时调用：复入用药图确认（与规则侧同一图）。"""
    del args
    elder_id = runtime_ctx.get("elder_id")
    if not elder_id:
        return {"confirmed": False, "reply": _GENERIC_NOT_VERIFIED}
    return await confirm_latest_event(elder_id)


TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "description": "记录长者报告已服药；无参数，工具自动匹配该长者待确认的用药提醒。",
}


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="medication_confirm",
        description=(
            "长者说自己已经服药/吃过药时调用，记录用药确认。"
            "若没有待确认的用药提醒，返回提示话术。"
        ),
        schema=TOOL_SCHEMA,
        handler=medication_confirm_handler,
    )
