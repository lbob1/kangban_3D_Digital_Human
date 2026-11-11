"""report_emergency Tool（Day6+ 第四道防线「宁多勿漏」）单元测试。

handler 直测（不经 LLM）：落库走 monkeypatch 的 session factory（单测不连开发库）；
elder 未验证时拒绝落库（工单必须可归户）；类目白名单外的非法传参归 other。
"""

import pytest

from app.tools import emergency_report, registry


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    monkeypatch.setitem(
        registry._REGISTRY, "report_emergency", emergency_report.build_tool_spec()
    )


class _FakeSessionCtx:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


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
        emergency_report, "get_session_factory", lambda: _FakeFactory(db_session)
    )


async def test_report_persists_workorder_with_category(db_session, seed, monkeypatch):
    _patch_factory(monkeypatch, db_session)
    elder_id = seed["elders"]["single"].elder_id

    result = await emergency_report.report_emergency_handler(
        {"category": "emotional_crisis", "description": "说不想活了，让家人快来"},
        {"elder_id": str(elder_id)},
    )
    assert result == {"reported": True, "category": "emotional_crisis"}

    from sqlalchemy import select

    from app.models.workorder import Workorder

    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].category == "emotional_crisis"
    assert rows[0].title.startswith("敏感事件[LLM观察]")  # 来源标注可区分词表命中
    assert "不想活" in rows[0].description


async def test_report_without_elder_dropped(monkeypatch):
    """未声纹验证（无 elder_id）：不落库（工单必须可归户），返回 reported=False。"""
    result = await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "摔倒了"}, {}
    )
    assert result == {"reported": False, "reason": "elder_not_verified"}


async def test_report_sanitizes_and_clamps_description(db_session, seed, monkeypatch):
    """LLM 输出不信任：控制字符清洗 + 300 字截断 + 非法类目归 other。"""
    _patch_factory(monkeypatch, db_session)

    result = await emergency_report.report_emergency_handler(
        {"category": "not_a_category", "description": "x\x00y" + "长" * 500},
        {"elder_id": str(seed["elders"]["single"].elder_id)},
    )
    assert result["category"] == "other"

    from sqlalchemy import select

    from app.models.workorder import Workorder

    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert len(rows[0].description) <= 300
    assert "\x00" not in rows[0].description
    assert rows[0].category == "other"


async def test_abuse_report_via_llm_writes_abuse_category(db_session, seed, monkeypatch):
    """LLM 观察到虐待 → category=abuse → 可见性矩阵自动适用（仅 family+admin 可见）。"""
    _patch_factory(monkeypatch, db_session)

    result = await emergency_report.report_emergency_handler(
        {"category": "abuse", "description": "长者提到被照顾者打骂"},
        {"elder_id": str(seed["elders"]["single"].elder_id)},
    )
    assert result["category"] == "abuse"
