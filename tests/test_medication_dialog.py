"""Day7 步7：对话内汇合测试——规则接管 / S1 映射 / 主控 Tool / 调度 tick。

口径：定时与对话共用一图（大纲红线）——对话确认经 confirm_latest_event
复入 app/graphs/medication.py 同一张图；handler 走 monkeypatch 的 session
factory（不连开发库，与 test_tools_emergency_report 同模式）。
"""

from datetime import UTC, datetime

from app.graphs.medication import start_reminder
from app.models import MedicationPlan
from app.models.enums import MedicationEventSource, MedicationEventStatus, MedicationPlanStatus
from app.router import cascade as rc
from app.router.cascade import RuntimeBridge, get_cascade_router
from app.system_one.questions import IntentLabel
from app.tools import medication_confirm, registry

# ---- 桩与夹具 ----


class _FakeFactory:
    """factory() → async context manager（同测试事务 savepoint，可见未提交数据）。"""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


def _patch_confirm_factory(monkeypatch, db_session):
    monkeypatch.setattr(
        medication_confirm, "get_session_factory", lambda: _FakeFactory(db_session)
    )


async def _make_active_plan(db_session, seed, **overrides) -> MedicationPlan:
    plan = MedicationPlan(
        elder_id=seed["elders"]["single"].elder_id,
        drug_name=overrides.get("drug_name", "硝苯地平缓释片"),
        dose=overrides.get("dose", "20mg"),
        schedule=overrides.get(
            "schedule",
            {
                "slots": [
                    {"time": "08:00", "label": "早"},
                    {"time": "12:30", "label": "午"},
                ]
            },
        ),
        start_date=datetime.now(UTC).date(),
        status=overrides.get("status", MedicationPlanStatus.ACTIVE),
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


def _notifier(ok: bool = True):
    async def notifier(state) -> bool:
        return ok

    return notifier


async def _start_event(db_session, seed, plan, tmp_path):
    return await start_reminder(
        db_session,
        plan=plan,
        elder_name=seed["elders"]["single"].name,
        slot="早",
        scheduled_at=datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0),
        source=MedicationEventSource.SCHEDULER,
        notifier=_notifier(True),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )


# ---- 规则接管 ----


async def test_dialog_confirm_reenters_graph(db_session, seed, tmp_path, monkeypatch):
    """「我吃过药了」：确认词 → 复入同一图 → event confirmed（验收 #11）。

    注：确认句不匹配规则正则（吃(点)?药|服药 零改动红线），生产走 S1 MEDICATION
    高置信 / 主控 Tool → 同一 handler；此处直调 handler 验证确认分支。
    """
    _patch_confirm_factory(monkeypatch, db_session)
    plan = await _make_active_plan(db_session, seed)
    event = await _start_event(db_session, seed, plan, tmp_path)
    token = RuntimeBridge.set({"elder_id": str(plan.elder_id)})
    try:
        match = rc.RuleMatch(
            rule_id="medication_preview",
            action="medication_confirm",
            reply_text=None,
            latency_ms=0.1,
            text="我吃过药了",
        )
        outcome = await rc._medication_confirm_handler(match, None)
    finally:
        RuntimeBridge.reset(token)

    assert outcome.intent == "medication_confirm"
    assert "已记录" in outcome.reply_text
    assert event.status == MedicationEventStatus.CONFIRMED
    assert event.confirmed_at is not None


async def test_dialog_plan_broadcast(db_session, seed, tmp_path, monkeypatch):
    """「我要吃药」：当日计划确定性播报（零模型零 token）。"""
    _patch_confirm_factory(monkeypatch, db_session)
    plan = await _make_active_plan(db_session, seed)
    token = RuntimeBridge.set({"elder_id": str(plan.elder_id)})
    try:
        outcome = await get_cascade_router().decide("我要吃药")
    finally:
        RuntimeBridge.reset(token)

    assert outcome.route_layer == "rule"
    assert plan.drug_name in outcome.reply_text
    assert "08:00" in outcome.reply_text and "12:30" in outcome.reply_text


async def test_dialog_anonymous_generic_reply(db_session, seed, monkeypatch):
    """未验证长者：通用话术，不泄露任何个人用药信息（红线 4/8）。"""
    _patch_confirm_factory(monkeypatch, db_session)
    await _make_active_plan(db_session, seed)

    outcome = await get_cascade_router().decide("我要吃药")
    assert outcome.route_layer == "rule"
    assert "声纹" in outcome.reply_text
    assert "硝苯地平" not in outcome.reply_text


async def test_dialog_confirm_without_event(db_session, seed, monkeypatch):
    _patch_confirm_factory(monkeypatch, db_session)
    plan = await _make_active_plan(db_session, seed)
    token = RuntimeBridge.set({"elder_id": str(plan.elder_id)})
    try:
        match = rc.RuleMatch(
            rule_id="medication_preview",
            action="medication_confirm",
            reply_text=None,
            latency_ms=0.1,
            text="我吃过药了",
        )
        outcome = await rc._medication_confirm_handler(match, None)
    finally:
        RuntimeBridge.reset(token)

    assert "没有待确认" in outcome.reply_text


# ---- S1 映射与 Tool ----


def test_s1_medication_maps_to_same_handler():
    assert rc.S1_INTENT_HANDLERS[IntentLabel.MEDICATION] == "medication_confirm"
    assert rc.S1_INTENT_HANDLERS[IntentLabel.OPERA] == "opera_play"


def test_medication_tool_registered_without_elder_id():
    registry.register_default_tools()
    spec = registry.get_tool("medication_confirm")
    assert spec is not None
    assert "elder_id" not in str(spec.schema)  # TDD §5.3：schema 禁含 elder_id


async def test_medication_tool_handler_reenters_graph(db_session, seed, tmp_path, monkeypatch):
    _patch_confirm_factory(monkeypatch, db_session)
    plan = await _make_active_plan(db_session, seed)
    event = await _start_event(db_session, seed, plan, tmp_path)

    result = await medication_confirm.medication_confirm_handler(
        {}, {"elder_id": str(plan.elder_id)}
    )
    assert result["confirmed"] is True
    assert event.status == MedicationEventStatus.CONFIRMED


async def test_medication_tool_requires_verified_elder(db_session, monkeypatch):
    _patch_confirm_factory(monkeypatch, db_session)
    result = await medication_confirm.medication_confirm_handler({}, {"elder_id": None})
    assert result["confirmed"] is False
    assert "声纹" in result["reply"]


# ---- 规则动作零回归 ----


def test_media_opera_rules_kept_preview_text():
    """opera/media_next 原位接管换 action 但保留预告话术（步8 才换真播放 handler）。"""
    from app.router.rules import RULES

    by_id = {r.rule_id: r for r in RULES}
    assert by_id["opera_preview"].action == "opera_play"
    assert by_id["media_next_preview"].action == "media_next"
    assert by_id["medication_preview"].action == "medication_confirm"
    assert len(RULES) == 10  # 条数封顶不追加
