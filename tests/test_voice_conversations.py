"""Day6+ 批3：对话记录可见性与查询测试。

老人端（GET /voice/conversations/recent，设备 Token 鉴权）：
- 声纹未验证/无活跃语音会话一律 403（可见性铁律，elder 由服务端会话状态确定）；
- 验证通过后返回最近 20 轮（user+assistant 成对，仅问题+AI 回答）。
管理端（GET /admin/conversations*，仅 admin）：
- 长者列表 + 全历史轮次 + decision_logs 路由叠加。
"""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, ConversationMessage, DecisionLog
from app.models.enums import ConversationStatus, IdentitySource
from app.services.voice import VoiceSession, voice_session_manager
from tests.conftest import login

RECENT_URL = "/api/v1/voice/conversations/recent"
ADMIN_URL = "/api/v1/admin/conversations"


async def _device_token(api, seed, device_key="single") -> str:
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    device = seed["devices"][device_key]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens", headers=admin
    )
    assert issued.status_code == 200, issued.text
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["access_token"]


def _register_verified_session(device_id, elder_id):
    """注册一个已通过声纹验证的活跃语音会话（内存注册表，测后清理）。"""

    async def _noop_send(event: dict) -> None:
        return None

    session = VoiceSession(
        device_id=device_id,
        candidates=[(elder_id, "测试长者")],
        embeddings={elder_id: []},
        send_event=_noop_send,
    )
    session.verified_elder_id = elder_id
    voice_session_manager.register(session)
    return session


async def _seed_rounds(
    db_session: AsyncSession, elder_id, thread_id, rounds: int, with_logs: bool = False
) -> None:
    """写 1 个会话 + rounds 轮消息（user→assistant 成对），可选逐轮 decision_log。"""
    db_session.add(
        Conversation(
            thread_id=thread_id,
            elder_id=elder_id,
            session_seq=1,
            identity_source=IdentitySource.VOICEPRINT,
            status=ConversationStatus.ACTIVE,
        )
    )
    await db_session.flush()
    for i in range(rounds):
        q = f"问题第{i + 1}句"
        a = f"回答第{i + 1}句"
        db_session.add(ConversationMessage(thread_id=thread_id, role="user", content=q))
        db_session.add(ConversationMessage(thread_id=thread_id, role="assistant", content=a))
        if with_logs:
            db_session.add(
                DecisionLog(
                    thread_id=thread_id,
                    elder_id=elder_id,
                    route_layer="rule" if i == 0 else "agent",
                    intent="weather_today" if i == 0 else None,
                    tool_calls=1 if i == 0 else 0,
                    input_text=q,
                )
            )
    await db_session.commit()


# ---- 老人端：可见性铁律 ----


async def test_recent_403_without_active_session(api, seed):
    """无活跃语音会话（WS 未连/已断）→ 403，对话记录不可见。"""
    token = await _device_token(api, seed)
    resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_recent_403_before_voiceprint_verified(api, seed, db_session):
    """有会话但声纹未验证（verified_elder_id 为空）→ 403。"""
    token = await _device_token(api, seed)
    device = seed["devices"]["single"]

    async def _noop_send(event: dict) -> None:
        return None

    session = VoiceSession(
        device_id=device.device_id,
        candidates=[(seed["elders"]["single"].elder_id, "王单间")],
        embeddings={seed["elders"]["single"].elder_id: []},
        send_event=_noop_send,
    )
    voice_session_manager.register(session)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
    finally:
        voice_session_manager.unregister(device.device_id)


async def test_recent_returns_last_20_rounds_paired(api, seed, db_session):
    """声纹验证通过 → 最近 20 轮（user/assistant 成对，旧轮不回）。"""
    token = await _device_token(api, seed)
    elder = seed["elders"]["single"]
    await _seed_rounds(db_session, elder.elder_id, uuid4(), rounds=23)  # 23 轮 > 20
    _register_verified_session(seed["devices"]["single"].device_id, elder.elder_id)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["elder_id"] == str(elder.elder_id)
        assert body["elder_name"] == elder.name
        rounds = body["rounds"]
        assert len(rounds) == 20
        # 只留最近 20 轮：第 4~23 轮；每轮 question/answer 成对
        assert rounds[0]["question"] == "问题第4句"
        assert rounds[-1]["question"] == "问题第23句"
        for r in rounds:
            assert r["question"].startswith("问题第")
            assert r["answer"].startswith("回答第")
            assert r["answered_at"] is not None
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


async def test_recent_limit_respected(api, seed, db_session):
    """limit=3 只回 3 轮。"""
    token = await _device_token(api, seed)
    elder = seed["elders"]["single"]
    await _seed_rounds(db_session, elder.elder_id, uuid4(), rounds=5)
    _register_verified_session(seed["devices"]["single"].device_id, elder.elder_id)
    try:
        resp = await api.get(
            RECENT_URL + "?limit=3", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert len(resp.json()["rounds"]) == 3
        assert resp.json()["rounds"][-1]["question"] == "问题第5句"
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


async def test_recent_rejects_user_jwt_403(api, seed):
    """人 JWT 打设备端点 → 403（get_current_device 边界）。"""
    headers = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await api.get(RECENT_URL, headers=headers)
    assert resp.status_code == 403


# ---- 管理端：全历史 + 路由叠加 ----


async def test_admin_conversations_rounds_with_route_overlay(
    api, seed, db_session, auth_headers_factory
):
    """admin：长者列表 + 轮次成对 + decision_logs 顺序对齐叠加 route 字段。"""
    elder = seed["elders"]["single"]
    thread_id = uuid4()
    await _seed_rounds(db_session, elder.elder_id, thread_id, rounds=3, with_logs=True)
    headers = await auth_headers_factory("admin")

    elders = await api.get(ADMIN_URL + "/elders", headers=headers)
    assert elders.status_code == 200
    items = elders.json()["items"]
    assert any(e["elder_id"] == str(elder.elder_id) for e in items)

    resp = await api.get(f"{ADMIN_URL}?elder_id={elder.elder_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["elder_name"] == elder.name
    assert len(body["threads"]) == 1
    rounds = body["threads"][0]["rounds"]
    assert len(rounds) == 3
    assert rounds[0]["question"] == "问题第1句" and rounds[0]["answer"] == "回答第1句"
    assert rounds[0]["route"]["route_layer"] == "rule"
    assert rounds[0]["route"]["intent"] == "weather_today"
    assert rounds[0]["route"]["tool_calls"] == 1
    assert rounds[2]["route"]["route_layer"] == "agent"
    assert rounds[2]["route"]["intent"] is None


async def test_admin_conversations_fallback_match_by_input_text(
    api, seed, db_session, auth_headers_factory
):
    """行数不一致（缺 1 行日志）时按 input_text 兜底匹配，对不上则 route=None。"""
    elder = seed["elders"]["single"]
    thread_id = uuid4()
    await _seed_rounds(db_session, elder.elder_id, thread_id, rounds=2, with_logs=True)
    # 删掉第 2 轮日志 → 行数 1 ≠ 轮数 2，走兜底匹配
    logs = (
        await db_session.execute(select(DecisionLog).where(DecisionLog.thread_id == thread_id))
    ).scalars().all()
    await db_session.delete(logs[-1])
    await db_session.commit()

    resp = await api.get(
        f"{ADMIN_URL}?elder_id={elder.elder_id}",
        headers=await auth_headers_factory("admin"),
    )
    assert resp.status_code == 200
    rounds = resp.json()["threads"][0]["rounds"]
    assert rounds[0]["route"]["route_layer"] == "rule"  # input_text 命中
    assert rounds[1]["route"] is None  # 对不上不虚构


async def test_admin_conversations_unknown_elder_404(api, seed, auth_headers_factory):
    resp = await api.get(
        f"{ADMIN_URL}?elder_id={uuid4()}", headers=await auth_headers_factory("admin")
    )
    assert resp.status_code == 404


async def test_admin_conversations_requires_admin(api, seed, auth_headers_factory):
    """非 admin（nurse）403；未登录 401。"""
    resp = await api.get(ADMIN_URL + "/elders", headers=await auth_headers_factory("nurse_a"))
    assert resp.status_code == 403
    resp = await api.get(ADMIN_URL + "/elders")
    assert resp.status_code == 401
