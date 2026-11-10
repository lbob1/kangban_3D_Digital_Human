"""Day7 步6：用药确认图单测（状态逐档 / 重试 3 次 / 重启恢复，POC-06 验收硬项）。

走 kangban_test 真实库（事务回滚隔离）；checkpoint 文件用 tmp_path 每例隔离。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.graphs.medication import (
    classify_reply,
    get_graph_state,
    recall,
    resume_with_reply,
    start_reminder,
    thread_id_for,
)
from app.models import MedicationEvent, MedicationPlan, Workorder
from app.models.enums import (
    MedicationEventSource,
    MedicationEventStatus,
)


def _notifier(ok: bool = True, calls: list | None = None):
    async def notifier(state) -> bool:
        if calls is not None:
            calls.append(dict(state))
        return ok

    return notifier


async def _make_plan(db_session, seed, **overrides) -> MedicationPlan:
    plan = MedicationPlan(
        elder_id=seed["elders"]["single"].elder_id,
        drug_name=overrides.get("drug_name", "硝苯地平缓释片"),
        dose=overrides.get("dose", "20mg"),
        schedule=overrides.get("schedule", {"slots": [{"time": "08:00", "label": "早"}]}),
        start_date=datetime.now(UTC).date(),
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


def _scheduled_at() -> datetime:
    return datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)


async def _start(db_session, seed, plan, notifier, tmp_path):
    return await start_reminder(
        db_session,
        plan=plan,
        elder_name=seed["elders"]["single"].name,
        slot="早",
        scheduled_at=_scheduled_at(),
        source=MedicationEventSource.SCHEDULER,
        notifier=notifier,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )


# ---- 分类器 ----


def test_classify_reply_three_kinds():
    assert classify_reply("我吃过药了") == "confirm"
    assert classify_reply("吃过了") == "confirm"
    assert classify_reply("还没吃呢") == "decline"
    assert classify_reply("今天天气不错") == "timeout"
    assert classify_reply("") == "timeout"


def test_thread_id_format():
    plan_id = uuid.uuid4()
    tid = thread_id_for(plan_id, datetime(2026, 11, 10, 8, 0, tzinfo=UTC), "早")
    assert tid == f"med-{plan_id}-2026-11-10-早"


# ---- 启动 / 幂等 ----


async def test_start_creates_event_and_waits_confirm(db_session, seed, tmp_path):
    calls: list = []
    plan = await _make_plan(db_session, seed)
    event = await _start(db_session, seed, plan, _notifier(True, calls), tmp_path)

    assert event.status == MedicationEventStatus.WAITING_CONFIRM
    assert event.thread_id == thread_id_for(plan.plan_id, _scheduled_at(), "早")
    assert len(calls) == 1
    assert calls[0]["drug_name"] == plan.drug_name


async def test_start_idempotent_same_thread(db_session, seed, tmp_path):
    calls: list = []
    plan = await _make_plan(db_session, seed)
    first = await _start(db_session, seed, plan, _notifier(True, calls), tmp_path)
    second = await _start(db_session, seed, plan, _notifier(True, calls), tmp_path)

    assert first.event_id == second.event_id
    assert len(calls) == 1  # 复入不重复外呼
    rows = (
        await db_session.execute(
            select(MedicationEvent).where(MedicationEvent.thread_id == first.thread_id)
        )
    ).scalars().all()
    assert len(rows) == 1


# ---- 确认 ----


async def test_confirm_flow(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed)
    notifier = _notifier(True)
    event = await _start(db_session, seed, plan, notifier, tmp_path)

    await resume_with_reply(
        db_session,
        event=event,
        reply="我吃过药了",
        elder_name=seed["elders"]["single"].name,
        drug_name=plan.drug_name,
        dose=plan.dose,
        slot="早",
        notifier=notifier,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert event.status == MedicationEventStatus.CONFIRMED
    assert event.confirmed_at is not None
    assert event.retry_count == 0


# ---- 拒绝重试 → 升级工单 ----


async def test_decline_retries_then_escalated_with_workorder(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed)
    notifier = _notifier(True)
    event = await _start(db_session, seed, plan, notifier, tmp_path)

    kwargs = dict(
        elder_name=seed["elders"]["single"].name,
        drug_name=plan.drug_name,
        dose=plan.dose,
        slot="早",
        notifier=notifier,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    # 第 1、2 次「没吃」：循环边回 calling 重呼（retry_count 1→2）
    for _ in range(2):
        await resume_with_reply(db_session, event=event, reply="没吃", **kwargs)
        assert event.status == MedicationEventStatus.WAITING_CONFIRM
    # 第 3 次「没吃」：重试耗尽（retry_count=3）→ escalated + 漏服工单
    await resume_with_reply(db_session, event=event, reply="没吃", **kwargs)

    assert event.status == MedicationEventStatus.ESCALATED
    assert event.retry_count == 3
    assert event.workorder_id is not None
    workorder = (
        await db_session.execute(
            select(Workorder).where(Workorder.workorder_id == event.workorder_id)
        )
    ).scalar_one()
    assert workorder.category == "medication"
    assert workorder.elder_id == plan.elder_id
    assert plan.drug_name in workorder.title


# ---- 离线退避 → 升级 ----


async def test_offline_backoff_recall_then_escalated(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed)
    notifier = _notifier(False)
    event = await _start(db_session, seed, plan, notifier, tmp_path)

    # 首呼离线：retry_count=1，状态停在 calling（等调度器退避后再 recall）
    assert event.retry_count == 1
    assert event.status == MedicationEventStatus.CALLING

    kwargs = dict(
        elder_name=seed["elders"]["single"].name,
        drug_name=plan.drug_name,
        dose=plan.dose,
        slot="早",
        notifier=notifier,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    await recall(db_session, event=event, **kwargs)
    assert event.retry_count == 2
    await recall(db_session, event=event, **kwargs)

    assert event.status == MedicationEventStatus.ESCALATED
    assert event.retry_count == 3
    assert event.workorder_id is not None


# ---- 超时重试后恢复 ----


async def test_timeout_then_recover_confirmed(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed)
    notifier = _notifier(True)
    event = await _start(db_session, seed, plan, notifier, tmp_path)

    kwargs = dict(
        elder_name=seed["elders"]["single"].name,
        drug_name=plan.drug_name,
        dose=plan.dose,
        slot="早",
        notifier=notifier,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    # 超时一次（调度器 120s 后触发）：retry_count+1，退避后 recall 重呼
    await resume_with_reply(db_session, event=event, reply="__timeout__", **kwargs)
    assert event.retry_count == 1
    await recall(db_session, event=event, **kwargs)
    assert event.status == MedicationEventStatus.WAITING_CONFIRM
    assert event.retry_count == 1

    # 长者终于确认
    await resume_with_reply(db_session, event=event, reply="吃了", **kwargs)
    assert event.status == MedicationEventStatus.CONFIRMED


# ---- get_state 重启恢复 ----


async def test_get_state_recovers_after_restart(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed)
    event = await _start(db_session, seed, plan, _notifier(True), tmp_path)

    # 模拟重启：全新连接读取 checkpoint（每次调用本就新开连接，文件级持久化）
    state = await get_graph_state(event.thread_id, checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state is not None
    assert state["current_node"] == "waiting_confirm"
    assert state["retry_count"] == 0
    assert state["drug_name"] == plan.drug_name
    assert state["__next__"] == ()  # 本轮已 END，等待下一次复入


async def test_get_state_unknown_thread_returns_none(tmp_path):
    state = await get_graph_state("med-nonexistent", checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state is None
