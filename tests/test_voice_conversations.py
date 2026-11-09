"""Day6+ 批4：对话记录分层结构（对话块→子对话）可见性与查询测试。

老人端（GET /voice/conversations/recent，设备 Token 鉴权）：
- 声纹未验证/无活跃语音会话一律 403（可见性铁律，elder 由服务端会话状态确定）；
- 验证通过后返回以 elder_id 为键的对话块集合：块倒序（最新在前）、
  块内轮次倒序、北京时间（+08:00）、两级 ID（block_id/round_id）、工单标记；
- 「开始对话」新开块：end_active_conversations 收口旧块，下轮 seq+1 新块。
管理端（GET /admin/conversations*，仅 admin）：长者列表 + 全历史 + 路由叠加。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, ConversationMessage, DecisionLog
from app.models.enums import ConversationStatus, IdentitySource
from app.services.conversation_store import (
    end_active_conversations,
    get_or_create_active_conversation,
)
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


async def _seed_block(
    db_session: AsyncSession,
    elder_id,
    thread_id,
    rounds: int,
    block_seq: int = 1,
    started_at: datetime | None = None,
    sensitive_round: int | None = None,
) -> None:
    """写 1 个对话块（conversation）+ rounds 轮消息，可选某轮打敏感命中决策行。"""
    db_session.add(
        Conversation(
            thread_id=thread_id,
            elder_id=elder_id,
            session_seq=block_seq,
            identity_source=IdentitySource.VOICEPRINT,
            status=ConversationStatus.ENDED if block_seq < 99 else ConversationStatus.ACTIVE,
            started_at=started_at or datetime(2026, 11, 9, 6, block_seq, tzinfo=UTC),
        )
    )
    await db_session.flush()
    for i in range(rounds):
        q = f"问题{block_seq}-{i + 1}"
        a = f"回答{block_seq}-{i + 1}"
        db_session.add(
            ConversationMessage(
                thread_id=thread_id,
                role="user",
                content=q,
                created_at=datetime(2026, 11, 9, 6, block_seq, i + 1, tzinfo=UTC),
            )
        )
        db_session.add(
            ConversationMessage(
                thread_id=thread_id,
                role="assistant",
                content=a,
                created_at=datetime(2026, 11, 9, 6, block_seq, i + 1, 30, tzinfo=UTC),
            )
        )
        if sensitive_round is not None and i == sensitive_round:
            db_session.add(
                DecisionLog(
                    thread_id=thread_id,
                    elder_id=elder_id,
                    route_layer="safety",
                    intent=None,
                    sensitive_hit="acute_symptom",
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


async def test_recent_rejects_user_jwt_403(api, seed):
    """人 JWT 打设备端点 → 403（get_current_device 边界）。"""
    headers = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await api.get(RECENT_URL, headers=headers)
    assert resp.status_code == 403


# ---- 老人端：分层结构 / 北京时间 / 倒序 / 两级 ID ----


async def test_recent_blocks_desc_beijing_time_and_round_desc(api, seed, db_session):
    """块倒序（最新块在前）、块内轮倒序、北京时间 +08:00、两级 ID 齐备。"""
    token = await _device_token(api, seed)
    elder = seed["elders"]["single"]
    t_old, t_new = uuid4(), uuid4()
    await _seed_block(db_session, elder.elder_id, t_old, rounds=2, block_seq=1)
    await _seed_block(db_session, elder.elder_id, t_new, rounds=1, block_seq=2)
    _register_verified_session(seed["devices"]["single"].device_id, elder.elder_id)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["elder_id"] == str(elder.elder_id)
        assert body["elder_name"] == elder.name

        blocks = body["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["block_id"] == str(t_new)  # 最新块在最前
        assert blocks[0]["block_no"] == 2
        assert blocks[1]["block_id"] == str(t_old)

        # 北京时间：UTC 06:02 → 北京 14:02（+08:00）
        assert blocks[0]["started_at"].startswith("2026-11-09T14:02")
        assert blocks[0]["started_at"].endswith("+08:00")

        # 块内轮倒序：最新一轮在前；两级 ID：round_id = block_id:seq
        newest_rounds = blocks[0]["rounds"]
        assert len(newest_rounds) == 1
        assert newest_rounds[0]["question"] == "问题2-1"
        old_rounds = blocks[1]["rounds"]
        assert [r["question"] for r in old_rounds] == ["问题1-2", "问题1-1"]
        assert old_rounds[0]["round_id"].startswith(str(t_old) + ":")
        assert old_rounds[0]["answer"] == "回答1-2"
        assert old_rounds[0]["answered_at"].endswith("+08:00")
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


async def test_recent_workorder_flags_block_and_round(api, seed, db_session):
    """工单标记：命中轮 has_workorder=True，块级任一轮命中即 True；未命中全 False。"""
    token = await _device_token(api, seed)
    elder = seed["elders"]["single"]
    t_hit, t_clean = uuid4(), uuid4()
    await _seed_block(
        db_session, elder.elder_id, t_hit, rounds=3, block_seq=3, sensitive_round=1
    )
    await _seed_block(db_session, elder.elder_id, t_clean, rounds=2, block_seq=4)
    _register_verified_session(seed["devices"]["single"].device_id, elder.elder_id)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        blocks = resp.json()["blocks"]
        hit = next(b for b in blocks if b["block_id"] == str(t_hit))
        clean = next(b for b in blocks if b["block_id"] == str(t_clean))
        assert hit["has_workorder"] is True
        by_q = {r["question"]: r["has_workorder"] for r in hit["rounds"]}
        assert by_q["问题3-2"] is True  # 敏感命中轮
        assert by_q["问题3-1"] is False
        assert clean["has_workorder"] is False
        assert all(r["has_workorder"] is False for r in clean["rounds"])
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


async def test_recent_limit_limits_blocks(api, seed, db_session):
    """limit 按对话块计：3 块取 2 块，最新 2 块。"""
    token = await _device_token(api, seed)
    elder = seed["elders"]["single"]
    for seq in (1, 2, 3):
        await _seed_block(db_session, elder.elder_id, uuid4(), rounds=1, block_seq=seq)
    _register_verified_session(seed["devices"]["single"].device_id, elder.elder_id)
    try:
        resp = await api.get(
            RECENT_URL + "?limit=2", headers={"Authorization": f"Bearer {token}"}
        )
        blocks = resp.json()["blocks"]
        assert len(blocks) == 2
        assert [b["block_no"] for b in blocks] == [3, 2]
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


# ---- 「开始对话」新开块 ----


async def test_end_active_conversations_rotates_block(api, seed, db_session):
    """批4 块切换：end_active 收口旧块 → get_or_create 新建 seq+1 新块。"""
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    old = await get_or_create_active_conversation(
        db_session,
        device_id=device.device_id,
        elder_id=elder.elder_id,
        identity_source=IdentitySource.VOICEPRINT,
    )
    assert old.session_seq == 1

    ended = await end_active_conversations(
        db_session, device_id=device.device_id, elder_id=elder.elder_id
    )
    await db_session.commit()
    assert ended == 1
    await db_session.refresh(old)
    assert old.status == ConversationStatus.ENDED
    assert old.ended_at is not None

    new = await get_or_create_active_conversation(
        db_session,
        device_id=device.device_id,
        elder_id=elder.elder_id,
        identity_source=IdentitySource.VOICEPRINT,
    )
    assert new.thread_id != old.thread_id  # 新块新 ID
    assert new.session_seq == old.session_seq + 1
    assert new.status == ConversationStatus.ACTIVE

    # 再点击一次：收口的是刚新建的 ACTIVE 块（1 行）；第三次无 ACTIVE 块 → 0 行（幂等不报错）
    assert await end_active_conversations(
        db_session, device_id=device.device_id, elder_id=elder.elder_id
    ) == 1
    assert await end_active_conversations(
        db_session, device_id=device.device_id, elder_id=elder.elder_id
    ) == 0


async def test_start_new_block_noop_without_db_factory(api, seed):
    """db_factory 为空（无库环境）时 start_new_block 直接返回，不抛错。"""

    async def _noop_send(event: dict) -> None:
        return None

    session = VoiceSession(
        device_id=uuid4(),
        candidates=[],
        embeddings={},
        send_event=_noop_send,
    )
    await session.start_new_block()  # 不抛错即通过


# ---- 管理端：全历史 + 路由叠加 + 块元数据 ----


async def test_admin_conversations_rounds_with_route_overlay(
    api, seed, db_session, auth_headers_factory
):
    """admin：长者列表 + 轮次成对 + decision_logs 顺序对齐叠加 route 字段。"""
    elder = seed["elders"]["single"]
    thread_id = uuid4()
    await _seed_block(db_session, elder.elder_id, thread_id, rounds=3, block_seq=1)
    # 补 3 行普通路由决策（顺序对齐路径）
    for i in range(3):
        db_session.add(
            DecisionLog(
                thread_id=thread_id,
                elder_id=elder.elder_id,
                route_layer="rule" if i == 0 else "agent",
                intent="weather_today" if i == 0 else None,
                tool_calls=1 if i == 0 else 0,
                input_text=f"问题1-{i + 1}",
            )
        )
    await db_session.commit()
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
    thread = body["threads"][0]
    assert thread["block_no"] == 1
    assert thread["has_workorder"] is False
    rounds = thread["rounds"]
    assert len(rounds) == 3
    assert rounds[0]["question"] == "问题1-1" and rounds[0]["answer"] == "回答1-1"
    assert rounds[0]["route"]["route_layer"] == "rule"
    assert rounds[0]["route"]["intent"] == "weather_today"
    assert rounds[0]["route"]["tool_calls"] == 1
    assert rounds[2]["route"]["route_layer"] == "agent"


async def test_admin_conversations_workorder_flag_and_fallback_match(
    api, seed, db_session, auth_headers_factory
):
    """行数不一致时按 input_text 兜底匹配；日志全删重建后块级 has_workorder=False。"""
    elder = seed["elders"]["single"]
    thread_id = uuid4()
    await _seed_block(db_session, elder.elder_id, thread_id, rounds=2, block_seq=1)
    # 删掉初始决策行 → 行数 0 ≠ 轮数 2；重建 1 行非命中的第二轮，验证兜底按原文匹配
    logs = (
        await db_session.execute(select(DecisionLog).where(DecisionLog.thread_id == thread_id))
    ).scalars().all()
    for lg in logs:
        await db_session.delete(lg)
    await db_session.commit()
    db_session.add(
        DecisionLog(
            thread_id=thread_id,
            elder_id=elder.elder_id,
            route_layer="agent",
            intent=None,
            sensitive_hit=None,
            input_text="问题1-2",
        )
    )
    await db_session.commit()

    resp = await api.get(
        f"{ADMIN_URL}?elder_id={elder.elder_id}",
        headers=await auth_headers_factory("admin"),
    )
    assert resp.status_code == 200
    thread = resp.json()["threads"][0]
    assert thread["has_workorder"] is False  # 仅剩的非命中行 → 无工单
    rounds = thread["rounds"]
    assert rounds[0]["route"] is None  # 问题1-1 对不上，不虚构
    assert rounds[1]["route"]["route_layer"] == "agent"  # input_text 兜底命中


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
