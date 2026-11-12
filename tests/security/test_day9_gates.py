"""Day9 步 2：三道隔离闸门攻击套件（大纲 9.1，主文档 §4.1）。

通过标准：攻击用例全部被拦截（「全红=拦截成功」）+ 正常路径全绿。
人读纪律：每条用例首行注释「攻击意图 → 期望拦截方式」，逐条人读过目，禁止凑数断言。

闸门① 会话隔离：thread_id 全部服务端派生（voice 历史按 device token → 声纹验证
→ verified_elder_id），客户端无可伪造入口（D5 口径：攻击面确认 + service 防御）；
闸门② 记忆隔离：recall pgvector 查询强制 elder_id + status 过滤（源码静态断言 +
跨人空集实测）；闸门③ 工具授权：schema 禁 elder_id、模型传参被忽略、缺 ctx 拒绝。
"""

from uuid import uuid4

import pytest

from app.models import Conversation
from app.models.enums import IdentitySource
from app.services.voice import VoiceSession, voice_session_manager
from app.tools import registry
from tests.conftest import login

RECENT_URL = "/api/v1/voice/conversations/recent"


# ---- 公共辅助（复用 test_voice_conversations 的会话/设备模式）----


async def _device_token(api, seed, device_key="single") -> str:
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    device = seed["devices"][device_key]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens", headers=admin
    )
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["access_token"]


def _register_verified_session(device_id, elder_id) -> VoiceSession:
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


async def _seed_conversation(db_session, elder_id, thread_id) -> None:
    """落一个属于 elder_id 的对话块（内容不落消息，隔离断言只看块归属）。"""
    from datetime import UTC, datetime

    from app.models.enums import ConversationStatus

    db_session.add(
        Conversation(
            thread_id=thread_id,
            elder_id=elder_id,
            session_seq=1,
            identity_source=IdentitySource.VOICEPRINT,
            status=ConversationStatus.ACTIVE,
            started_at=datetime.now(UTC),
        )
    )
    await db_session.flush()


# =========================================================================
# 闸门① 会话隔离（大纲 9.1：伪造他人 thread_id → 403；D5：无入口可伪造）
# =========================================================================


async def test_gate1a_openapi_has_no_client_thread_id_param(api):
    """攻击意图：客户端伪造他人 thread_id 直查对话 → 期望拦截方式：全站 API
    攻击面确认——OpenAPI schema 中不存在任何 thread_id 入参（服务端派生，无入口）。"""
    resp = await api.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    offenders: list[str] = []
    for path, methods in paths.items():
        for method, op in methods.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            for param in op.get("parameters") or []:
                if param.get("name") == "thread_id":
                    offenders.append(f"{method.upper()} {path}")
            body = op.get("requestBody")
            if body is None:
                continue
            for schema in body.get("content", {}).values():
                props = (schema.get("schema") or {}).get("properties") or {}
                if "thread_id" in props:
                    offenders.append(f"{method.upper()} {path} (body)")
    assert offenders == [], f"发现客户端可控 thread_id 入参（攻击面泄漏）: {offenders}"


@pytest.mark.parametrize("username", ["nurse_a", "doctor", "family", "admin"])
async def test_gate1b_user_jwt_on_device_history_403(api, seed, username):
    """攻击意图：持人 JWT（护工/医生/家属/管理员）冒充设备查老人语音历史 →
    期望拦截方式：设备端点 get_current_device 只收设备 Token，人 JWT 一律 403。"""
    headers = {"Authorization": f"Bearer {await login(api, username, seed['password'])}"}
    resp = await api.get(RECENT_URL, headers=headers)
    assert resp.status_code == 403


async def test_gate1c_history_before_voiceprint_403(api, seed):
    """攻击意图：拿到设备 Token 但未过声纹验证直接拉对话记录 →
    期望拦截方式：活跃会话 verified_elder_id 为空 → 403（可见性铁律）。"""
    token = await _device_token(api, seed)
    resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "声纹未验证" in resp.text


async def test_gate1d_verified_device_scoped_to_own_elder(api, seed, db_session):
    """攻击意图：设备已验证为 A，想看同库 B 长者的对话块 →
    期望拦截方式：thread 集合由服务端按 verified_elder_id 派生——响应只含 A
    的块，B 的 thread_id 即使真实存在也不出现在结果里。"""
    elder_a = seed["elders"]["single"]
    elder_b = seed["elders"]["other"]
    thread_a, thread_b = uuid4(), uuid4()
    await _seed_conversation(db_session, elder_a.elder_id, thread_a)
    await _seed_conversation(db_session, elder_b.elder_id, thread_b)

    token = await _device_token(api, seed)
    _register_verified_session(seed["devices"]["single"].device_id, elder_a.elder_id)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["elder_id"] == str(elder_a.elder_id)
        block_ids = [b["block_id"] for b in body["blocks"]]
        assert str(thread_a) in block_ids  # 自己的块可见
        assert str(thread_b) not in block_ids  # 他人块绝不混入
    finally:
        voice_session_manager.unregister(seed["devices"]["single"].device_id)


async def test_gate1e_service_defense_b_credential_sees_a_empty(api, seed, db_session):
    """攻击意图：B 长者的凭证上下文（设备会话）想读 A 的对话 →
    期望拦截方式：service 层防御验证——B 的会话历史查询按 elder_id=B 过滤，
    A 的对话记录不在结果集（空集），且响应不回显 A 的任何内容。"""
    elder_a = seed["elders"]["single"]
    elder_b = seed["elders"]["d2"]
    thread_a = uuid4()
    await _seed_conversation(db_session, elder_a.elder_id, thread_a)

    # B 的设备（双人间 2 床）+ B 已声纹验证
    token = await _device_token(api, seed, device_key="double")
    _register_verified_session(seed["devices"]["double"].device_id, elder_b.elder_id)
    try:
        resp = await api.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["elder_id"] == str(elder_b.elder_id)
        assert body["blocks"] == []  # B 名下无对话 → 空集（A 的记录拿不到）
    finally:
        voice_session_manager.unregister(seed["devices"]["double"].device_id)


# =========================================================================
# 闸门② 记忆隔离（大纲 9.1：pgvector 查询必须带 elder_id 条件）
# =========================================================================


@pytest.fixture
def _fake_embed(monkeypatch):
    """确定性向量替换（不连 embedding API）：1024 维单热点向量（同文本同向量）。"""

    def _vec(text: str) -> list[float]:
        vec = [0.0] * 1024  # memory_chunks 列固定 1024 维（迁移 0012）
        vec[hash(text) % 1024] = 1.0  # 同文本同向量，异文本大概率异向量
        return vec

    async def fake_embed_text(text: str, transport=None) -> list[float]:
        return _vec(text)

    async def fake_embed_texts(texts: list[str], transport=None) -> list[list[float]]:
        return [_vec(t) for t in texts]

    from app.integrations import embedding

    monkeypatch.setattr(embedding, "embed_text", fake_embed_text)
    monkeypatch.setattr(embedding, "embed_texts", fake_embed_texts)


def _gate2_sql_source_assertions() -> None:
    """源码静态断言（D5/§4.1 允许口径）：recall 的 WHERE 必须含 elder_id 与 status。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app" / "services" / "memory.py").read_text(
        encoding="utf-8"
    )
    recall_block = src[src.index("async def recall_memories") : src.index("④ 召回后二次过滤")]
    # 攻击意图：召回查询漏掉主体过滤 → 期望拦截：WHERE 强制 elder_id 精确匹配 + ACTIVE 状态
    assert "MemoryChunk.elder_id == elder_id" in recall_block
    assert "MemoryStatus.ACTIVE" in recall_block
    # 匿名/通用模式（elder_id=None）短路返回空集，不做任何查询
    assert "if elder_id is None" in recall_block


def _one_hot(idx: int) -> list[float]:
    """1024 维单热点向量（索引唯一即互不相似）。"""
    vec = [0.0] * 1024
    vec[idx] = 1.0
    return vec


async def test_gate2a_recall_anonymous_empty(db_session, _fake_embed):
    """攻击意图：无 elder 主体（匿名/未声纹验证）也想召回记忆 →
    期望拦截方式：recall_memories elder_id=None 直接空集，不执行查询。"""
    from app.services.memory import recall_memories

    rows = await recall_memories(db_session, elder_id=None, query_text="我爱吃什么")
    assert rows == []


async def test_gate2b_recall_cross_elder_empty(db_session, seed, _fake_embed):
    """攻击意图：以 B 的主体身份召回，想捞到 A 在库里的相似记忆 →
    期望拦截方式：WHERE elder_id=B 强制过滤——A 的记忆哪怕语义完全相同也不出现。"""
    from app.services.memory import recall_memories, store_memories

    elder_a = seed["elders"]["single"]
    elder_b = seed["elders"]["d2"]
    await store_memories(
        db_session,
        elder_id=elder_a.elder_id,
        items=[{"content": "长者爱吃红烧肉", "category": "preference", "importance": 3}],
        vectors=[_one_hot(1)],
    )

    # B 主体召回同一语义 query → 空集（B 名下无记忆）
    rows_b = await recall_memories(
        db_session, elder_id=elder_b.elder_id, query_text="长者爱吃红烧肉"
    )
    assert rows_b == []
    # 对照组（正常路径）：A 主体自己能召回
    rows_a = await recall_memories(
        db_session, elder_id=elder_a.elder_id, query_text="长者爱吃红烧肉"
    )
    assert len(rows_a) == 1


async def test_gate2c_recall_result_all_belong_to_injected_elder(db_session, seed, _fake_embed):
    """攻击意图：库里混存 A/B 两人的记忆，以 B 主体召回 top-k →
    期望拦截方式：返回集 100% 属于注入的 elder_id=B（跨人内容零泄漏）。"""
    from app.services.memory import recall_memories, store_memories

    elder_a = seed["elders"]["single"]
    elder_b = seed["elders"]["d2"]
    await store_memories(
        db_session,
        elder_id=elder_a.elder_id,
        items=[{"content": "A 的私密偏好", "category": "preference", "importance": 5}],
        vectors=[_one_hot(1)],
    )
    await store_memories(
        db_session,
        elder_id=elder_b.elder_id,
        items=[{"content": "B 的偏好", "category": "preference", "importance": 3}],
        vectors=[_one_hot(2)],
    )

    rows = await recall_memories(db_session, elder_id=elder_b.elder_id, query_text="偏好")
    assert rows, "正常路径：B 至少召回自己的记忆"
    assert all(r.elder_id == elder_b.elder_id for r in rows)
    assert all("A 的私密偏好" not in r.content for r in rows)


async def test_gate2d_recall_sql_source_assertions():
    """攻击意图：未来重构把召回查询的 elder_id 过滤删掉（回归防线）→
    期望拦截方式：源码静态断言——recall WHERE 必须含 elder_id 精确匹配 +
    status=ACTIVE + None 主体短路（主文档 §4.1 b 允许的静态口径）。"""
    _gate2_sql_source_assertions()


# =========================================================================
# 闸门③ 工具授权（大纲 9.1：Tool 从 runtime_context 读 elder_id，忽略模型传参）
# =========================================================================


class _FakeSessionCtx:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


class _FakeFactory:
    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


async def test_gate3a_model_passing_victim_elder_id_is_ignored(db_session, seed, monkeypatch):
    """攻击意图：被提示注入的模型在 tool_calls args 里显式传 elder_id=受害者 →
    期望拦截方式：handler 完全忽略 args 的 elder_id，归属取 runtime_ctx 注入值。"""
    from app.tools import emergency_report

    monkeypatch.setattr(
        emergency_report, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    self_elder = seed["elders"]["single"].elder_id
    victim_elder = seed["elders"]["other"].elder_id

    result = await emergency_report.report_emergency_handler(
        {
            "category": "fall",
            "description": "模型伪造：这是受害者的紧急报告",
            "elder_id": str(victim_elder),  # 模型传参（必须被忽略）
        },
        {"elder_id": str(self_elder)},  # 服务端注入的真实主体
    )
    assert result["reported"] is True

    from sqlalchemy import select

    from app.models.workorder import Workorder

    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].elder_id == self_elder  # 归户=服务端注入值
    assert rows[0].elder_id != victim_elder


async def test_gate3b_all_registered_tool_schemas_have_no_elder_id():
    """攻击意图：工具 schema 开 elder_id 字段让模型/客户端传身份 →
    期望拦截方式：静态断言——注册表全部工具 schema 均无 elder_id（含嵌套定义）。"""
    from app.tools.registry import register_default_tools

    register_default_tools()
    specs = registry.list_tools()
    assert len(specs) >= 6  # weather/local_time/report_emergency/medication/play/health_triage
    for spec in specs:
        dumped = str(spec.schema)
        assert "elder_id" not in dumped, f"工具 {spec.name} schema 含 elder_id（禁区 8）"


async def test_gate3c_missing_runtime_ctx_rejected_report_emergency(monkeypatch):
    """攻击意图：绕过声纹验证（runtime_ctx 无 elder_id）直接调紧急上报工具 →
    期望拦截方式：返回 elder_not_verified，不落任何工单（工单必须可归户）。"""
    from app.tools import emergency_report

    result = await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "摔倒了"}, {}
    )
    assert result == {"reported": False, "reason": "elder_not_verified"}


async def test_gate3d_missing_runtime_ctx_rejected_medication_and_play():
    """攻击意图：未验证长者也要确认用药/点播 →
    期望拦截方式：medication_confirm 与 play_media 均拒绝执行（不查库不落库）。"""
    from app.tools.medication_confirm import medication_confirm_handler
    from app.tools.play_media import play_media_handler

    med = await medication_confirm_handler({}, {})
    assert med["confirmed"] is False
    play = await play_media_handler({"query": "戏曲"}, {})
    assert play["status"] == "need_voiceprint"


async def test_gate3a_normal_path_workorder_belongs_to_caller(db_session, seed, monkeypatch):
    """正常路径对照（非攻击）：runtime_ctx 与 args 均指向本人 → 工单正常落库。"""
    from app.tools import emergency_report

    monkeypatch.setattr(
        emergency_report, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    elder = seed["elders"]["d1"].elder_id
    result = await emergency_report.report_emergency_handler(
        {"category": "acute_symptom", "description": "胸闷"},
        {"elder_id": str(elder)},
    )
    assert result["reported"] is True
    from sqlalchemy import select

    from app.models.workorder import Workorder

    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].elder_id == elder
