"""Day7 步 4 接线测试：块结束萃取钩子 + 回复管线召回注入。

口径（D8）：萃取挂在 start_new_block（会话块收口后、后台任务不阻塞通话）；
召回注入 _reply_via_agent 组装 prompt 处（persona 之后、对话历史之前）。
隔离红线在接线层同样成立：匿名会话（verified_elder_id=None）零个人记忆。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations import embedding as embedding_client
from app.integrations import llm as llm_client
from app.models import Conversation, ConversationMessage
from app.models.enums import ConversationStatus, IdentitySource
from app.models.memory import MemoryChunk
from app.services import memory as memory_service
from app.services.voice import VoiceSession, _build_system_prompt


def _vec(index: int) -> list[float]:
    v = [0.0] * 1024
    v[index] = 1.0
    return v


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """与 conftest.db_session 同构的会话工厂（同一连接、savepoint 模式、随外层回滚）。"""
    return async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _session(device_id, elder_id, factory) -> VoiceSession:
    session = VoiceSession(
        device_id=device_id,
        candidates=[(elder_id, "测试长者")],
        embeddings={elder_id: []},
        send_event=lambda event: None,
        db_factory=factory,
    )
    session.verified_elder_id = elder_id
    return session


async def _seed_active_block(db_session, elder_id, device_id) -> tuple[Conversation, list[str]]:
    """写 1 个 ACTIVE 会话块 + 两轮消息（收口后应被萃取）。"""
    conversation = Conversation(
        thread_id=uuid.uuid4(),
        elder_id=elder_id,
        device_id=device_id,
        session_seq=1,
        identity_source=IdentitySource.VOICEPRINT,
        status=ConversationStatus.ACTIVE,
    )
    db_session.add(conversation)
    await db_session.flush()
    contents = []
    for role, text in [("user", "我有糖尿病，平时不敢多吃"), ("assistant", "好的，我会记住的。")]:
        db_session.add(
            ConversationMessage(
                thread_id=conversation.thread_id, role=role, content=text
            )
        )
        contents.append(text)
    await db_session.commit()
    return conversation, contents


# ---- 萃取钩子 ----


async def test_start_new_block_triggers_extraction(db_session, seed, monkeypatch):
    """块收口 → 后台任务萃取结束块消息，elder_id 取服务端 verified_elder_id。"""
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    conversation, _ = await _seed_active_block(db_session, elder.elder_id, device.device_id)

    captured: dict = {}

    async def fake_extract(db, *, elder_id, messages, **kwargs):
        captured["elder_id"] = elder_id
        captured["messages"] = list(messages)
        return 2

    monkeypatch.setattr(memory_service, "extract_and_store", fake_extract)

    session = _session(device.device_id, elder.elder_id, _session_factory(db_session))
    await session.start_new_block()
    assert session._block_extraction_task is not None
    await session._block_extraction_task

    assert captured["elder_id"] == elder.elder_id  # 服务端 Runtime Context 注入
    assert [m.content for m in captured["messages"]] == [
        "我有糖尿病，平时不敢多吃",
        "好的，我会记住的。",
    ]
    # 块已被收口
    await db_session.refresh(conversation)
    assert conversation.status == ConversationStatus.ENDED


async def test_start_new_block_anonymous_skips_extraction(db_session, seed, monkeypatch):
    """匿名会话（verified_elder_id=None）→ 不调度萃取（隔离红线在接线层成立）。"""

    async def fail_extract(db, *, elder_id, messages, **kwargs):
        raise AssertionError("匿名会话不应触发萃取")

    monkeypatch.setattr(memory_service, "extract_and_store", fail_extract)

    device = seed["devices"]["single"]
    await _seed_active_block(db_session, None, device.device_id)

    session = _session(device.device_id, None, _session_factory(db_session))
    await session.start_new_block()
    assert session._block_extraction_task is None


# ---- 召回注入 ----


async def test_recall_memory_block_injected(db_session, seed, monkeypatch):
    """召回 + ④二次过滤 → 记忆块文本；隔离过滤与注入点均按 D8 口径。"""
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    row = MemoryChunk(
        elder_id=elder.elder_id,
        content="长者患有糖尿病，日常控糖",
        embedding=_vec(0),
        embedding_model="test-model",
        category="health",
        visibility="private",
        importance=5,
    )
    db_session.add(row)
    await db_session.commit()

    async def fake_embed_text(text, *, transport=None):
        return _vec(0)

    async def fake_chat(*args, **kwargs):
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "select_relevant_memories",
                    "arguments": {"relevant_ids": [str(row.chunk_id)]},
                }
            ],
            "raw_tool_calls": [],
            "usage": {"total_tokens": 1},
        }

    monkeypatch.setattr(embedding_client, "embed_text", fake_embed_text)
    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)

    session = _session(device.device_id, elder.elder_id, _session_factory(db_session))
    block = await session._recall_memory_block("能吃粽子吗")
    assert block is not None
    assert "[health] 长者患有糖尿病，日常控糖" in block
    assert "【长期记忆】" in block


async def test_recall_memory_block_anonymous_none(db_session, seed):
    """匿名会话 → None（零个人记忆），且不做任何库查询。"""
    device = seed["devices"]["single"]
    session = _session(device.device_id, None, _session_factory(db_session))
    assert await session._recall_memory_block("能吃粽子吗") is None


async def test_recall_memory_block_db_failure_none(db_session, seed):
    """库故障 → 返回 None（无记忆直答，不阻塞对话）。"""
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]

    def broken_factory():
        raise RuntimeError("db down")

    session = _session(device.device_id, elder.elder_id, broken_factory)
    assert await session._recall_memory_block("能吃粽子吗") is None


def test_build_system_prompt_memory_after_persona():
    """记忆块紧跟 persona 之后（D8 注入点），无记忆时不出现占位。"""
    plain = _build_system_prompt()
    with_memory = _build_system_prompt("【长期记忆】\n- [health] 测试记忆")
    assert "【长期记忆】" not in plain
    assert "【长期记忆】" in with_memory
    persona = with_memory.split("【长期记忆】")[0]
    assert persona.strip()  # persona 在记忆块之前
    assert with_memory.index("【长期记忆】") < with_memory.index("紧急观察规则")
