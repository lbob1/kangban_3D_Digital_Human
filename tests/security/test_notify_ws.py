"""Day9 步 7：/ws/notify 通知实时化测试（主文档 §4.4，D2）。

覆盖：WS 鉴权（伪造/设备 Token/无 Token 拒绝——TestClient 真握手）；范围过滤
（护工本楼栋、医生/家属有效关联、admin 全量——复用 workqueue 同款
can_access_elder 语义，以伪 WS 连接注册表 + 异步 broadcast 实测，规避
TestClient 与 pytest-asyncio 事件循环冲突）；双通道（日志必达 + WS 尽力）/
离线兜底（无连接仅日志）/ notify 开关关闭回 Day8 行为 / 事件字段白名单。
"""

import jwt as pyjwt
import pytest
from starlette.testclient import TestClient

from app.core.config import get_settings

NOTIFY_PATH = "/ws/notify"


@pytest.fixture
def _clean_manager():
    """每例清空连接注册表（进程级单例，防串扰）。"""
    from app.realtime import notify_ws

    notify_ws.manager._connections.clear()
    yield notify_ws.manager
    notify_ws.manager._connections.clear()


@pytest.fixture
def _patch_notify_session_factory(monkeypatch, db_session):
    """notify_ws 的会话工厂指向测试会话（范围判定/账号校验均在测试库同循环内）。"""
    from app.realtime import notify_ws

    class _FakeFactory:
        def __init__(self, session):
            self._s = session

        def __call__(self):
            return self

        async def __aenter__(self):
            return self._s

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(notify_ws, "get_session_factory", lambda: _FakeFactory(db_session))
    yield


class _FakeWS:
    """伪 WS 连接（scope 过滤实测用：broadcast 只调 send_json）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)


def _mint_token(seed, username: str) -> str:
    """手工签发用户 JWT（与 conftest.login 签发口径一致；账号在测试库真实存在）。"""
    from datetime import UTC, datetime, timedelta

    account = seed["accounts"][username]
    return pyjwt.encode(
        {
            "sub": str(account.account_id),
            "role": account.role,
            "token_type": "user",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )


def _mint_device_token(seed) -> str:
    from datetime import UTC, datetime, timedelta

    return pyjwt.encode(
        {
            "sub": str(seed["devices"]["single"].device_id),
            "token_type": "device",
            "token_kind": "access",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )


# ---- WS 鉴权闸门（真握手；无效 token 在触达会话前被拒，无事件循环冲突）----


def test_ws_invalid_token_closed_4401(app):
    """攻击意图：伪造 JWT 冒充管理端接收通知 → 期望：握手后 4401 关闭。"""
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(NOTIFY_PATH + "?token=forged.token.here"):
                pass


def test_ws_device_token_rejected(app, seed):
    """攻击意图：设备 Token（人/设备互斥红线）接到通知通道 → 期望：4401 关闭。"""
    device_token = _mint_device_token(seed)
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(NOTIFY_PATH + f"?token={device_token}"):
                pass


def test_ws_missing_token_closed(app):
    """攻击意图：不带 token 直接连 → 期望：4401 关闭（默认空串即拒绝）。"""
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(NOTIFY_PATH):
                pass


# ---- 范围过滤（服务端判定，客户端不做权限判断；伪 WS + 异步 broadcast）----


async def _nurse_building_assignment(db_session, seed, building_code: str):
    from app.models import BuildingAssignment
    from app.models.enums import AssignmentStatus

    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id,
            building_code=building_code,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def _doctor_assignment(db_session, seed, elder_id):
    from app.models import DoctorElderAssignment
    from app.models.enums import AssignmentStatus

    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=elder_id,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def _family_link(db_session, seed, elder_id, *, expired: bool = False):
    from datetime import UTC, datetime, timedelta

    from app.models import FamilyElderLink
    from app.models.enums import AssignmentStatus

    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=elder_id,
            relation="son",
            status=AssignmentStatus.ACTIVE,
            valid_until=(
                datetime.now(UTC) - timedelta(minutes=1)
                if expired
                else datetime.now(UTC) + timedelta(days=1)
            ),
        )
    )
    await db_session.flush()


def _event(elder_id) -> dict:
    return {
        "type": "notify",
        "notify_kind": "health_urgent",
        "elder_id": str(elder_id),
        "elder_name": "王单间",
        "level": "red",
        "reason": "收缩压185≥180",
    }


async def test_admin_receives_any_elder(
    db_session, seed, _clean_manager, _patch_notify_session_factory
):
    """admin 全量接收：任意 elder 的事件必达（对照角色基线）。"""
    _clean_manager.connect(
        _FakeWS(), role="admin", account_id=seed["accounts"]["admin"].account_id
    )
    sent = await _clean_manager.broadcast(_event(seed["elders"]["other"].elder_id))
    assert sent == 1


async def test_nurse_only_own_building(
    db_session, seed, _clean_manager, _patch_notify_session_factory
):
    """硬验收（大纲 8.1 口径复用）：护工只收本楼栋——A 楼护工收不到 B 楼长者事件。"""
    from app.realtime.notify_ws import manager

    await _nurse_building_assignment(db_session, seed, "A")
    ws = _FakeWS()
    manager.connect(ws, role="nurse", account_id=seed["accounts"]["nurse_a"].account_id)
    elder_a = seed["elders"]["single"].elder_id  # A 楼 101
    elder_b = seed["elders"]["other"].elder_id  # B 楼 201
    sent = await manager.broadcast(_event(elder_b))
    assert sent == 0 and ws.sent == []  # 范围外：不推送（不泄露存在性）
    sent = await manager.broadcast(_event(elder_a))
    assert sent == 1 and ws.sent[0]["elder_id"] == str(elder_a)


async def test_family_only_valid_linked_elder(
    db_session, seed, _clean_manager, _patch_notify_session_factory
):
    """家属只收有效关联长者：过期关联不推送（范围判定复用 can_access_elder）。"""
    from app.realtime.notify_ws import manager

    elder = seed["elders"]["single"].elder_id
    await _family_link(db_session, seed, elder, expired=True)
    manager.connect(_FakeWS(), role="family", account_id=seed["accounts"]["family"].account_id)
    sent = await manager.broadcast(_event(elder))
    assert sent == 0  # 关联已过期：不推送


async def test_family_valid_link_receives(
    db_session, seed, _clean_manager, _patch_notify_session_factory
):
    """家属有效关联 → 接收（正常路径对照）。"""
    from app.realtime.notify_ws import manager

    elder = seed["elders"]["single"].elder_id
    await _family_link(db_session, seed, elder, expired=False)
    ws = _FakeWS()
    manager.connect(ws, role="family", account_id=seed["accounts"]["family"].account_id)
    sent = await manager.broadcast(_event(elder))
    assert sent == 1 and ws.sent[0]["notify_kind"] == "health_urgent"


async def test_doctor_linked_receives_out_of_scope_not(
    db_session, seed, _clean_manager, _patch_notify_session_factory
):
    """医生只收关联长者：有关联接收；未关联长者不推送。"""
    from app.realtime.notify_ws import manager

    linked = seed["elders"]["single"].elder_id
    unlinked = seed["elders"]["other"].elder_id
    await _doctor_assignment(db_session, seed, linked)
    ws = _FakeWS()
    manager.connect(ws, role="doctor", account_id=seed["accounts"]["doctor"].account_id)
    assert await manager.broadcast(_event(unlinked)) == 0 and ws.sent == []
    assert await manager.broadcast(_event(linked)) == 1


# ---- 双通道 / 离线兜底 / 开关 / 事件边界 ----


async def test_notify_log_channel_always_fires(caplog):
    """双通道①：日志必达——无 WS 连接（离线）时结构化日志仍记录（不丢通知）。"""
    from app.services import notifier

    result = await notifier.notify(
        {"notify_kind": "health_urgent", "record_id": "r1", "elder_name": "王单间"}
    )
    assert result is False  # 无在线连接 → WS 送达 0
    assert any(getattr(r, "event", None) == "health_notifier_fired" for r in caplog.records)


async def test_notify_ws_disabled_returns_day8_behavior(monkeypatch, caplog):
    """开关关闭（notify_ws_enabled=false）→ 仅日志通道（回 Day8 行为），不触达广播。"""
    from app.realtime import notify_ws
    from app.services import notifier

    class _Stub:
        notify_ws_enabled = False

    monkeypatch.setattr(notifier, "get_settings", lambda: _Stub())
    calls = {"n": 0}

    async def _spy(event):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(notify_ws.manager, "broadcast", _spy)
    result = await notifier.notify({"notify_kind": "health_urgent"})
    assert result is False
    assert calls["n"] == 0  # 未触达 WS
    assert any(getattr(r, "event", None) == "health_notifier_fired" for r in caplog.records)


async def test_notify_event_payload_whitelist(seed, monkeypatch):
    """上下文边界：WS 事件只含白名单字段——对话原文/病史明细等敏感字段不外发。"""
    from app.realtime import notify_ws
    from app.services import notifier

    captured: dict = {}

    async def _spy(event):
        captured["event"] = event
        return 1

    monkeypatch.setattr(notify_ws.manager, "broadcast", _spy)

    class _Stub:
        notify_ws_enabled = True

    monkeypatch.setattr(notifier, "get_settings", lambda: _Stub())
    await notifier.notify(
        {
            "notify_kind": "health_urgent",
            "elder_id": "e1",
            "elder_name": "王单间",
            "input_text": "不该外发的对话原文",  # 敏感字段：必须被白名单滤掉
            "chief_complaint": "病史明细",  # 同上
        }
    )
    ev = captured["event"]
    assert ev["type"] == "notify" and ev["elder_name"] == "王单间"
    assert "input_text" not in ev and "chief_complaint" not in ev


async def test_notify_broadcast_failure_does_not_block_graph(monkeypatch, caplog):
    """WS 链路失败（广播抛异常）→ 不阻断图/端点主流程，日志通道已兜底。"""
    from app.realtime import notify_ws
    from app.services import notifier

    class _Stub:
        notify_ws_enabled = True

    monkeypatch.setattr(notifier, "get_settings", lambda: _Stub())

    async def _boom(event):
        raise RuntimeError("socket dead")

    monkeypatch.setattr(notify_ws.manager, "broadcast", _boom)
    result = await notifier.notify({"notify_kind": "health_urgent"})
    assert result is False  # 失败被吞（日志通道已记录），主流程不受影响
    assert any(getattr(r, "event", None) == "notify_ws_error" for r in caplog.records)
