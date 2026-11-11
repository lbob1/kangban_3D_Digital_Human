"""Day8 步3：health_triage 主控子图工具 + safety 急症词扩表 测试。

- 「我头晕」e2e：工具落 voice_complaint 记录 → 健康图分级 → 观察*不含*分级结论
  （话术红线）；green 零副作用、red 建单挂起 interrupt；
- 未验证拒绝：runtime_ctx 无 elder_id / args 塞 elder_id 一律拒绝（AGENTS 禁区 8）；
- safety 扩表：晕厥/意识模糊在级联 ① 层直达 HITL（复用既有架构，大纲 §4.2）。
"""

import pytest
from sqlalchemy import select

from app.graphs.health import get_graph_state, thread_id_for
from app.models import HealthRecord, Workorder
from app.models.enums import WorkorderPriority, WorkorderStatus
from app.tools import health_triage, registry


class _FakeFactory:
    """对应生产 `get_session_factory()()`：工厂调用后返回异步上下文会话。"""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


def _patch_factory(monkeypatch, db_session):
    monkeypatch.setattr(
        health_triage, "get_session_factory", lambda: _FakeFactory(db_session)
    )


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    monkeypatch.setitem(
        registry._REGISTRY, "health_triage", health_triage.build_tool_spec()
    )


async def _records(db_session) -> list[HealthRecord]:
    rows = await db_session.execute(
        select(HealthRecord).where(HealthRecord.source == "voice_complaint")
    )
    return list(rows.scalars().all())


async def _health_workorders(db_session) -> list[Workorder]:
    return (
        (await db_session.execute(select(Workorder).where(Workorder.category == "health_alert")))
        .scalars()
        .all()
    )


# ---- 注册表接线：schema 不含 elder_id ----


async def test_registry_wired_schema_has_no_elder_id():
    registry.register_default_tools()
    spec = registry.get_tool("health_triage")
    assert spec is not None and spec.handler is not None
    assert "elder_id" not in (spec.schema.get("properties") or {})
    assert "chief_complaint" in spec.schema["required"]


# ---- 「我头晕」e2e：green 主诉记录，观察零分级结论 ----


async def test_say_dizzy_creates_green_record(db_session, seed, monkeypatch, tmp_path):
    _patch_factory(monkeypatch, db_session)
    elder = seed["elders"]["single"]

    result = await health_triage.health_triage_handler(
        {"chief_complaint": "我今天有点头晕"},
        {"elder_id": str(elder.elder_id), "checkpoint_path": str(tmp_path / "ck.sqlite")},
    )

    assert result["triaged"] is True
    # 话术红线：观察里没有分级信息，固定确认话术零诊断词
    assert "level" not in result and "reason" not in result
    assert "分级" not in result["ack"] and "green" not in result["ack"]
    assert "工作人员" in result["ack"]

    records = await _records(db_session)
    assert len(records) == 1
    assert records[0].chief_complaint == "我今天有点头晕"
    assert records[0].triage_level == "green"  # 头晕非急症词、无测量值 → green
    assert await _health_workorders(db_session) == []  # green 零副作用


# ---- 急症主诉（词表漏网兜底）：red → 建单 + interrupt 挂起，观察仍零分级 ----


async def test_red_complaint_creates_workorder_and_pauses(db_session, seed, monkeypatch, tmp_path):
    _patch_factory(monkeypatch, db_session)
    elder = seed["elders"]["single"]

    result = await health_triage.health_triage_handler(
        {"chief_complaint": "感觉喘不上气"},
        {"elder_id": str(elder.elder_id), "checkpoint_path": str(tmp_path / "ck.sqlite")},
    )

    assert result["triaged"] is True
    assert "level" not in result
    workorders = await _health_workorders(db_session)
    assert len(workorders) == 1
    assert workorders[0].status == WorkorderStatus.OPEN
    assert workorders[0].priority == WorkorderPriority.URGENT
    # 图挂起在 doctor interrupt（副作用红线：确认前零通知）
    state = await get_graph_state(
        thread_id_for(elder.elder_id, __import__("uuid").UUID(result["record_id"])),
        checkpoint_path=str(tmp_path / "ck.sqlite"),
    )
    assert state is not None and state["__next__"] == ("doctor_confirm",)


# ---- 未验证拒绝（验收 5）----


async def test_client_elder_id_rejected(db_session, seed, monkeypatch):
    _patch_factory(monkeypatch, db_session)
    result = await health_triage.health_triage_handler(
        {"chief_complaint": "头晕", "elder_id": str(seed["elders"]["single"].elder_id)},
        {"elder_id": None},
    )
    assert result == {"triaged": False, "reason": "elder_id_not_allowed"}
    assert await _records(db_session) == []


async def test_unverified_elder_rejected(db_session, seed, monkeypatch):
    _patch_factory(monkeypatch, db_session)
    result = await health_triage.health_triage_handler(
        {"chief_complaint": "头晕"}, {}
    )
    assert result == {"triaged": False, "reason": "elder_not_verified"}
    assert await _records(db_session) == []


async def test_invalid_args_rejected(db_session, seed, monkeypatch):
    _patch_factory(monkeypatch, db_session)
    result = await health_triage.health_triage_handler(
        {"chief_complaint": "血压有点高", "bp_systolic": 999},
        {"elder_id": str(seed["elders"]["single"].elder_id)},
    )
    assert result == {"triaged": False, "reason": "invalid_args"}
    assert await _records(db_session) == []


# ---- safety 扩表：晕厥/意识模糊直达 ① 层（复用既有级联架构）----


async def test_safety_words_extended():
    from app.router.safety import check_sensitive

    hit = check_sensitive("我刚才好像晕厥了一下")
    assert hit is not None and "acute_symptom" in hit.types
    hit = check_sensitive("他早上起来有点意识模糊")
    assert hit is not None and "acute_symptom" in hit.types


# ---- 级联分层 e2e：头晕走主控（非敏感/非外呼），胸口疼走 ① 层短路 ----


async def test_cascade_layers():
    from app.router.cascade import CascadeRouter, _register_default_handlers

    router = CascadeRouter()
    _register_default_handlers(router)  # 单测不挂 S1/天气（bar_cascade 同口径）

    dizzy = await router.decide("我有点头晕，不太舒服")
    assert dizzy.route_layer == "agent" and dizzy.escalated_to_agent is True
    assert dizzy.create_workorder is False  # 非敏感路径 → 交主控子图

    chest = await router.decide("我胸口疼")
    assert chest.route_layer == "safety" and chest.create_workorder is True
