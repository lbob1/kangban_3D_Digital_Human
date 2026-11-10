"""Day7 步7：APScheduler 调度 tick 测试（到点触发 / 离线退避 / 超时推进）。

ws_notifier monkeypatch 为桩（不依赖真实 WS 会话）；session factory 同测试事务。
"""

import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import MedicationEvent, Workorder
from app.models.enums import MedicationEventSource, MedicationEventStatus
from app.scheduler import medication_tick
from tests.test_medication_dialog import _FakeFactory, _make_active_plan

_TZ = ZoneInfo("Asia/Shanghai")


def _tmp_ckpt() -> str:
    return str(Path(tempfile.gettempdir()) / f"ck-{uuid.uuid4()}.sqlite")


def _patch_scheduler_env(monkeypatch, db_session, calls):
    from app import scheduler as sched
    from app.core.config import get_settings

    monkeypatch.setattr(sched, "get_session_factory", lambda: _FakeFactory(db_session))
    notifier_ok = [True]

    async def fake_notifier(state) -> bool:
        calls.append(dict(state))
        return notifier_ok[0]

    monkeypatch.setattr(sched, "ws_notifier", fake_notifier)

    settings = get_settings()
    monkeypatch.setattr(settings, "medication_slot_window_seconds", 120, raising=False)
    monkeypatch.setattr(settings, "medication_wait_timeout_seconds", 9999, raising=False)
    monkeypatch.setattr(settings, "medication_offline_backoff_seconds", 9999, raising=False)
    return notifier_ok


def _slot_time_now() -> str:
    now = datetime.now(_TZ)
    return f"{now.hour:02d}:{now.minute:02d}"


async def test_tick_triggers_due_slot_idempotent(db_session, seed, monkeypatch):
    calls: list = []
    _patch_scheduler_env(monkeypatch, db_session, calls)
    await _make_active_plan(
        db_session, seed, schedule={"slots": [{"time": _slot_time_now(), "label": "早"}]}
    )

    await medication_tick()
    await medication_tick()  # 幂等：同 thread 复用，不重复外呼

    events = ((await db_session.execute(select(MedicationEvent))).scalars().all())
    assert len(events) == 1
    assert events[0].status == MedicationEventStatus.WAITING_CONFIRM
    assert events[0].source == MedicationEventSource.SCHEDULER
    assert len(calls) == 1  # 第二次 tick 复入不重呼


async def test_tick_offline_backoff_then_escalated(db_session, seed, monkeypatch):
    calls: list = []
    notifier_ok = _patch_scheduler_env(monkeypatch, db_session, calls)
    from app.core.config import get_settings

    await _make_active_plan(
        db_session, seed, schedule={"slots": [{"time": _slot_time_now(), "label": "早"}]}
    )

    notifier_ok[0] = False  # 设备离线
    await medication_tick()  # 首呼失败：retry_count=1，停在 calling 等退避
    event = ((await db_session.execute(select(MedicationEvent))).scalars().one())
    assert event.status == MedicationEventStatus.CALLING
    assert event.retry_count == 1

    monkeypatch.setattr(get_settings(), "medication_offline_backoff_seconds", 0, raising=False)
    await medication_tick()  # 退避到点 recall：retry_count=2
    await db_session.refresh(event)
    assert event.retry_count == 2

    await medication_tick()  # 再 recall：retry_count=3 → escalated 落工单
    await db_session.refresh(event)
    assert event.status == MedicationEventStatus.ESCALATED
    assert event.retry_count == 3
    workorders = (
        (await db_session.execute(select(Workorder).where(Workorder.category == "medication")))
        .scalars()
        .all()
    )
    assert len(workorders) == 1


async def test_tick_timeout_advances_retry(db_session, seed, monkeypatch):
    calls: list = []
    _patch_scheduler_env(monkeypatch, db_session, calls)
    from app.core.config import get_settings

    # 不建到点计划（slot 在窗口外）：事件用图直接启动，避免 tick 步1 干扰
    plan = await _make_active_plan(
        db_session, seed, schedule={"slots": [{"time": "03:00", "label": "早"}]}
    )
    from app.graphs.medication import start_reminder

    event = await start_reminder(
        db_session,
        plan=plan,
        elder_name=seed["elders"]["single"].name,
        slot="早",
        scheduled_at=datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0),
        source=MedicationEventSource.SCHEDULER,
        notifier=lambda state: _async_true(),
        checkpoint_path=_tmp_ckpt(),
    )
    assert event.status == MedicationEventStatus.WAITING_CONFIRM

    monkeypatch.setattr(get_settings(), "medication_wait_timeout_seconds", 0, raising=False)
    await medication_tick()  # 超时推进：__timeout__ → retry → END（退避）
    await db_session.refresh(event)
    assert event.retry_count == 1
    assert event.status == MedicationEventStatus.WAITING_CONFIRM


async def _async_true() -> bool:
    return True
