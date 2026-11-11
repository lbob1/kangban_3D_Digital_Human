"""用药提醒调度（Day7 步7，大纲 7.2/§4.2）：APScheduler 定时扫描 + 外呼接线。

外呼口径（D1，与大纲字面差异如实标注）：服务端无法向浏览器主动发起 WebRTC
（浏览器为发起方）——「在线外呼」复用既有 WS 下行 + TTS/口型链路数字人开口；
设备不在线（无活动 WS 会话）由 notifier 返回 False，图内走 retry 循环、
调度器按退避间隔 recall 再呼，3 次耗尽由图 escalated 节点落漏服工单。

单 tick 三件事（幂等均可重入，start_reminder 按 thread_id 查重）：
1) 到点 slot → start_reminder（发起外呼）；
2) waiting_confirm 超时 → resume_with_reply("__timeout__")（图内 retry→END 退避）；
3) calling 离线退避到点 → recall（再呼）。

定时与对话共用一图（大纲红线）：本模块只做触发与退避，状态机全部在
app/graphs/medication.py；确认恒由对话侧（规则 handler / 主控 Tool）复入。
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.graphs.medication import recall, resume_with_reply, start_reminder
from app.models import Device, Elder, MedicationEvent, MedicationPlan
from app.models.enums import (
    DeviceStatus,
    ElderStatus,
    MedicationEventSource,
    MedicationEventStatus,
    MedicationPlanStatus,
)
from app.services.voice import voice_session_manager

logger = get_logger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")


# ---- 外呼 notifier（图 calling 节点回调；在线=True 已播报，离线=False 走退避）----


async def device_id_for_elder(db: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """长者 → 房间 → 首台 active 设备（未绑定/无设备 → None → 离线退避）。"""
    elder = await db.get(Elder, elder_id)
    if elder is None or elder.status != ElderStatus.ACTIVE or elder.room_id is None:
        return None
    device = (
        await db.execute(
            select(Device.device_id)
            .where(Device.room_id == elder.room_id, Device.status == DeviceStatus.ACTIVE)
            .order_by(Device.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return device


async def ws_notifier(state: dict) -> bool:
    """D1 外呼：WS 下行 medication_reminder 元数据 + 复用 TTS/口型链路开口播报。"""
    try:
        elder_id = uuid.UUID(str(state["elder_id"]))
        factory = get_session_factory()
        async with factory() as db:
            device_id = await device_id_for_elder(db, elder_id)
        if device_id is None:
            return False
        session = voice_session_manager.get(device_id)
        if session is None:  # 设备 WS 不在线
            return False
        text = (
            f"{state['elder_name']}，{state['slot']}用药时间到了："
            f"{state['drug_name']} {state['dose']}。吃好了请告诉我一声。"
        )
        # 元数据事件（前端 voice.js 只追加的播报分支）+ TTS 音频（tts 事件自然跟进）
        await session.send_event(
            {
                "type": "medication_reminder",
                "event_id": state["event_id"],
                "thread_id": state.get("thread_id"),
                "drug_name": state["drug_name"],
                "dose": state["dose"],
                "slot": state["slot"],
                "text": text,
            }
        )
        # 复用既有 TTS/口型链路数字人开口（capture_metrics=False 不污染打点口径）
        await session._synthesize_and_send(text, capture_metrics=False)  # noqa: SLF001
        return True
    except Exception:  # noqa: BLE001 —— 外呼失败按离线处理，不炸调度循环
        logger.exception("medication ws notify failed")
        return False


# ---- 事件上下文（recall/timeout 复入图所需字段）----


async def _event_context(db: AsyncSession, event: MedicationEvent) -> dict | None:
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


# ---- tick 任务 ----


async def medication_tick() -> None:
    """调度心跳：到点触发 / 超时推进 / 离线退避，全部经图入口（共用一图）。"""
    settings = get_settings()
    now = datetime.now(_TZ)
    now_utc = datetime.now(UTC)
    today = now.date()
    factory = get_session_factory()
    async with factory() as db:
        # 1) 到点 slot（start_reminder 幂等：同 thread 已有事件直接复用）
        plans = (
            (
                await db.execute(
                    select(MedicationPlan).where(
                        MedicationPlan.status == MedicationPlanStatus.ACTIVE,
                        MedicationPlan.start_date <= today,
                    )
                )
            )
            .scalars()
            .all()
        )
        for plan in plans:
            if plan.end_date is not None and plan.end_date < today:
                continue
            for slot in (plan.schedule or {}).get("slots", []):
                slot_dt = _slot_datetime(today, str(slot.get("time", "")))
                if slot_dt is None:
                    continue
                delta = (now - slot_dt).total_seconds()
                if 0 <= delta <= settings.medication_slot_window_seconds:
                    elder = await db.get(Elder, plan.elder_id)
                    event = await start_reminder(
                        db,
                        plan=plan,
                        elder_name=elder.name if elder else "长者",
                        slot=str(slot.get("label") or slot["time"]),
                        scheduled_at=slot_dt,
                        source=MedicationEventSource.SCHEDULER,
                        notifier=ws_notifier,
                    )
                    logger.info(
                        "medication reminder triggered",
                        extra={
                            "event": "medication_reminder_triggered",
                            "event_id": str(event.event_id),
                            "status": event.status,
                        },
                    )

        # 2) waiting_confirm 超时 → resume("__timeout__")：图内 retry→END（退避），
        #    耗尽由图 escalated 节点落工单（resume 内 flush 刷新 updated_at 重新计时）
        stale_wait = (
            (
                await db.execute(
                    select(MedicationEvent).where(
                        MedicationEvent.status == MedicationEventStatus.WAITING_CONFIRM,
                        MedicationEvent.updated_at
                        < now_utc - timedelta(seconds=settings.medication_wait_timeout_seconds),
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in stale_wait:
            ctx = await _event_context(db, event)
            if ctx is None:
                continue
            await resume_with_reply(
                db, event=event, reply="__timeout__", notifier=ws_notifier, **ctx
            )
            logger.info(
                "medication wait timeout advanced",
                extra={
                    "event": "medication_timeout",
                    "event_id": str(event.event_id),
                    "retry_count": event.retry_count,
                    "status": event.status,
                },
            )

        # 3) calling 且离线退避到点 → recall 再呼（耗尽由图 escalated）
        offline_events = (
            (
                await db.execute(
                    select(MedicationEvent).where(
                        MedicationEvent.status == MedicationEventStatus.CALLING,
                        MedicationEvent.updated_at
                        < now_utc - timedelta(seconds=settings.medication_offline_backoff_seconds),
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in offline_events:
            ctx = await _event_context(db, event)
            if ctx is None:
                continue
            await recall(db, event=event, notifier=ws_notifier, **ctx)
            logger.info(
                "medication offline recall",
                extra={
                    "event": "medication_recall",
                    "event_id": str(event.event_id),
                    "retry_count": event.retry_count,
                    "status": event.status,
                },
            )

        await db.commit()


def _slot_datetime(today, time_text: str):
    try:
        hour, minute = (int(p) for p in time_text.split(":")[:2])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=_TZ)


# ---- Day8 步4：晚间健康外呼（20:00，§4.3；三通道共用健康图）----


async def health_checkin_tick() -> None:
    """20:00 晚间健康外呼：在线设备逐一下行 health_checkin + 4 问采集；
    离线长者按 D1 口径标记（次日外呼头插一问补昨晚睡眠，不重建调度）；
    先 finalize 过期采集会话（部分答案照常入库）。"""
    from app.services import health_checkin

    await health_checkin.finalize_expired()
    today = datetime.now(_TZ).date()
    factory = get_session_factory()
    async with factory() as db:
        elders = (
            (
                await db.execute(select(Elder).where(Elder.status == ElderStatus.ACTIVE))
            )
            .scalars()
            .all()
        )
        device_by_elder = {
            elder.elder_id: await device_id_for_elder(db, elder.elder_id) for elder in elders
        }
    for elder in elders:
        if elder.elder_id in health_checkin._collectors:  # noqa: SLF001 —— 采集中不重复外呼
            continue
        device_id = device_by_elder.get(elder.elder_id)
        session = voice_session_manager.get(device_id) if device_id else None
        if session is None:
            health_checkin.mark_offline(elder.elder_id, day=today)
            logger.warning(
                "health checkin offline marked",
                extra={
                    "event": "health_checkin_offline",
                    "elder_id": str(elder.elder_id),
                },
            )
            continue
        makeup = health_checkin.pending_makeup_date(elder.elder_id)
        # 补问只在缺失次日生效（隔两天以上不再回溯，避免翻旧账）
        if makeup is not None and makeup < today - timedelta(days=1):
            makeup = None
        await health_checkin.start_checkin(
            elder.elder_id, elder.name, session, makeup_date=makeup
        )


# ---- 生命周期 ----

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def start_scheduler() -> AsyncIOScheduler | None:
    """启动调度器（用药与健康外呼分别受各自开关控制）。"""
    global _scheduler
    settings = get_settings()
    if not (settings.medication_scheduler_enabled or settings.health_call_enabled):
        logger.info("scheduler disabled", extra={"event": "scheduler_disabled"})
        return None
    if _scheduler is not None:
        return _scheduler
    _scheduler = AsyncIOScheduler(timezone=str(_TZ))
    if settings.medication_scheduler_enabled:
        _scheduler.add_job(
            medication_tick,
            "interval",
            seconds=settings.medication_scan_interval_seconds,
            id="medication_tick",
            max_instances=1,
            coalesce=True,
        )
    if settings.health_call_enabled:
        hh, mm = (int(p) for p in settings.health_call_time.split(":")[:2])
        _scheduler.add_job(
            health_checkin_tick,
            "cron",
            hour=hh,
            minute=mm,
            id="health_checkin",
            max_instances=1,
            coalesce=True,
        )
    _scheduler.start()
    logger.info(
        "scheduler started",
        extra={
            "event": "scheduler_started",
            "interval_s": settings.medication_scan_interval_seconds,
            "health_call_time": settings.health_call_time,
        },
    )
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("medication scheduler stopped", extra={"event": "scheduler_stopped"})
