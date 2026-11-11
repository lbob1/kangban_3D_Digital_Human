"""Day8 步2：健康决策图单测（三分支 / 两处 interrupt / 副作用红线 / 重启恢复）。

走 kangban_test 真实库（事务回滚隔离）；checkpoint 文件用 tmp_path 每例隔离。
红线断言：green 零副作用；interrupt 未恢复前零通知；确认后副作用有留痕。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.graphs.health import get_graph_state, resume_confirm, run_triage, thread_id_for
from app.models import HealthRecord, Workorder
from app.models.enums import WorkorderPriority, WorkorderStatus


def _notifier(calls: list | None = None):
    async def notifier(state) -> bool:
        if calls is not None:
            calls.append(dict(state))
        return True

    return notifier


async def _make_record(db_session, seed, **overrides) -> HealthRecord:
    record = HealthRecord(
        elder_id=seed["elders"]["single"].elder_id,
        recorded_at=overrides.pop("recorded_at", datetime.now(UTC)),
        bp_systolic=overrides.pop("bp_systolic", None),
        bp_diastolic=overrides.pop("bp_diastolic", None),
        sleep_minutes=overrides.pop("sleep_minutes", None),
        mood=overrides.pop("mood", None),
        chief_complaint=overrides.pop("chief_complaint", None),
        source=overrides.pop("source", "checkin"),
    )
    db_session.add(record)
    await db_session.flush()
    return record


def _tid(record: HealthRecord) -> str:
    return thread_id_for(record.elder_id, record.record_id)


async def _workorders(db_session) -> list[Workorder]:
    return (
        (
            await db_session.execute(
                select(Workorder).where(Workorder.category == "health_alert")
            )
        )
        .scalars()
        .all()
    )


# ---- green：零副作用 ----


async def test_green_zero_side_effects(db_session, seed, tmp_path):
    calls: list = []
    record = await _make_record(
        db_session, seed, bp_systolic=130, bp_diastolic=85, sleep_minutes=420, mood="4"
    )
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "green"
    assert record.triage_reason
    assert await _workorders(db_session) == []  # 无工单
    assert calls == []  # 无通知


async def test_missing_all_green_with_note(db_session, seed, tmp_path):
    record = await _make_record(db_session, seed)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "green"
    assert "缺采集" in record.triage_reason


# ---- yellow：建单 → interrupt 暂停（确认前零通知）→ resolve/escalate ----


async def test_yellow_pauses_before_notify(db_session, seed, tmp_path):
    calls: list = []
    record = await _make_record(db_session, seed, bp_systolic=165, bp_diastolic=88)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "yellow"
    workorders = await _workorders(db_session)
    assert len(workorders) == 1
    wo = workorders[0]
    assert wo.status == WorkorderStatus.OPEN  # 先建单，可看板可见
    assert wo.priority == WorkorderPriority.HIGH
    assert calls == []  # 副作用红线：interrupt 未恢复前零通知
    state = await get_graph_state(_tid(record), checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state["level"] == "yellow"
    assert state["__next__"] == ("nurse_confirm",)  # 挂起在护工确认
    assert state["workorder_id"] == str(wo.workorder_id)


async def test_yellow_resume_resolve(db_session, seed, tmp_path):
    calls: list = []
    record = await _make_record(db_session, seed, sleep_minutes=200)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    await resume_confirm(
        db_session, record=record, action="resolve", operator="nurse_a",
        notifier=_notifier(calls), checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    wo = (await _workorders(db_session))[0]
    assert wo.status == WorkorderStatus.ACKNOWLEDGED  # 确认后流转可处理
    assert wo.priority == WorkorderPriority.HIGH  # 未升级
    state = await get_graph_state(_tid(record), checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state["decision_operator"] == "nurse_a"  # 决策人可审计
    assert state.get("referral_no") is None  # yellow 无转诊
    assert calls == []  # yellow resolve 无需下行通知


async def test_yellow_resume_escalate_then_doctor(db_session, seed, tmp_path):
    calls: list = []
    record = await _make_record(db_session, seed, bp_systolic=170)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    # 护工确认升级 → 升 urgent → 挂起在医生确认（仍零通知）
    await resume_confirm(
        db_session, record=record, action="escalate", operator="nurse_a",
        notifier=_notifier(calls), checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    wo = (await _workorders(db_session))[0]
    assert wo.priority == WorkorderPriority.URGENT  # 升级留痕
    assert calls == []  # 升级后仍暂停在医生 interrupt，零通知
    state = await get_graph_state(_tid(record), checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state["__next__"] == ("doctor_confirm",)
    # 医生确认 → 放行副作用链（通知+转诊 mock）
    await resume_confirm(
        db_session, record=record, action="resolve", operator="doctor",
        notifier=_notifier(calls), checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert len(calls) == 1  # 确认后才放行通知
    state = await get_graph_state(_tid(record), checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state["referral_no"] and state["referral_no"].startswith("REF-")
    assert wo.status == WorkorderStatus.ACKNOWLEDGED
    assert state["referral_no"] in wo.description  # 转诊单号留痕进工单


# ---- red：直达 urgent 单 → doctor interrupt → 确认后全副作用链 ----


async def test_red_pauses_then_full_side_effects(db_session, seed, tmp_path):
    calls: list = []
    record = await _make_record(db_session, seed, bp_systolic=185, bp_diastolic=95)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "red"
    wo = (await _workorders(db_session))[0]
    assert wo.priority == WorkorderPriority.URGENT
    assert calls == []  # 红线：等医生确认期间零通知
    await resume_confirm(
        db_session, record=record, action="resolve", operator="doctor",
        notifier=_notifier(calls), checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert len(calls) == 1
    assert calls[0]["notify_kind"] == "health_urgent"
    state = await get_graph_state(_tid(record), checkpoint_path=str(tmp_path / "ck.sqlite"))
    assert state["referral_no"] and state["referral_no"].startswith("REF-")
    assert state["notified"] is True


async def test_complaint_direct_red(db_session, seed, tmp_path):
    record = await _make_record(db_session, seed, chief_complaint="胸口疼")
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "red"
    assert "胸口疼" in record.triage_reason


# ---- 重启恢复（POC-06 收口证据：断点落 SQLite，新连接续跑结果一致）----


async def test_interrupt_survives_reopen(db_session, seed, tmp_path):
    calls: list = []
    ck = str(tmp_path / "ck.sqlite")
    record = await _make_record(db_session, seed, bp_systolic=185)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(calls),
        checkpoint_path=ck,
    )
    # 模拟服务重启：get_state（新开连接）可见挂起态与完整审计字段
    state = await get_graph_state(_tid(record), checkpoint_path=ck)
    assert state["__next__"] == ("doctor_confirm",)
    assert state["level"] == "red" and state["reason"]
    assert state["workorder_id"]
    # 新连接 Command(resume) 续跑，结果与不重启一致
    await resume_confirm(
        db_session, record=record, action="resolve", operator="doctor",
        notifier=_notifier(calls), checkpoint_path=ck,
    )
    state = await get_graph_state(_tid(record), checkpoint_path=ck)
    assert state["__next__"] == ()  # 图已走完
    assert state["referral_no"] and len(calls) == 1


# ---- 误报防护：偏差>40% 标注进 triage_reason（不改级别）----


async def test_deviation_annotation_with_baseline(db_session, seed, tmp_path):
    elder_id = seed["elders"]["single"].elder_id
    base_time = datetime.now(UTC) - timedelta(days=2)
    for _ in range(3):
        db_session.add(
            HealthRecord(
                elder_id=elder_id,
                recorded_at=base_time,
                bp_systolic=120,
                bp_diastolic=80,
                sleep_minutes=420,
                source="checkin",
            )
        )
    await db_session.flush()
    record = await _make_record(db_session, seed, bp_systolic=185)
    await run_triage(
        db_session, record=record, elder_name="王单间", notifier=_notifier(),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert record.triage_level == "red"  # 级别不变，裁决权留给 HITL
    assert "偏差大，建议人工复核" in record.triage_reason
