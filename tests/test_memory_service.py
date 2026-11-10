"""MemoryService 步 2 单测：①萃取 ②存储 ③召回 + 隔离三件套（一票否决验收项）。

覆盖大纲 7.1 高危点原文三项必测：
- 伪造他人 elder_id 返回空（越权者只拿到自己的空集，拿不到任何他人记忆）；
- 跨 elder 空集（李爷爷查不到王奶奶记忆）；
- 匿名/通用模式零个人记忆。
embedding/LLM 全部注入 fake，不访问真实网络（测试不外呼红线）。
"""

import json
from uuid import UUID

import pytest

from app.integrations import embedding as embedding_client
from app.integrations import llm as llm_client
from app.integrations.embedding import EmbeddingUnavailable
from app.integrations.llm import LlmUnavailable
from app.models.memory import MemoryChunk
from app.services import memory as memory_service

# 固定种子（隔离于 conftest.seed：本地建 elder，避免依赖 seed 内部结构）


async def _make_elder(db_session) -> UUID:
    import uuid as _uuid

    from app.models import Elder, Persona

    persona = Persona(
        code=f"memory-test-{_uuid.uuid4().hex[:8]}", name="记忆测试人设", is_active=True
    )
    db_session.add(persona)
    await db_session.flush()  # 先取 persona_id（ elder.persona_id 非空 FK）
    elder = Elder(name="记忆测试长者", persona_id=persona.persona_id, status="active")
    db_session.add(elder)
    await db_session.flush()
    return elder.elder_id


def _vec(index: int) -> list[float]:
    v = [0.0] * 1024
    v[index] = 1.0
    return v


HEALTH_CONTENT = "长者患有糖尿病，日常控糖"


def _tool_call_result(memories: list[dict]) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {"id": "call-1", "name": "submit_memories", "arguments": {"memories": memories}}
        ],
        "raw_tool_calls": [],
        "usage": {"total_tokens": 1},
    }


# ---------------------------------------------------------------------------
# ①萃取 + ②存储
# ---------------------------------------------------------------------------


async def test_extract_and_store_happy_path(db_session, monkeypatch):
    """块结束萃取 → 3 条结构化记忆落库：importance 持久化、1024 维、默认 private。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        return _tool_call_result(
            [
                {"content": HEALTH_CONTENT, "category": "health", "importance": 5},
                {"content": "长者爱吃粽子", "category": "preference", "importance": 3},
                {
                    "content": "长者女儿每周探望",
                    "category": "relationship",
                    "visibility": "care_team",
                    "importance": 2,
                },
            ]
        )

    async def fake_embed(texts, *, transport=None):
        return [_vec(i) for i in range(len(texts))]

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)

    messages = [("user", "我有糖尿病"), ("assistant", "好的，我会注意提醒您控糖。")]
    count = await memory_service.extract_and_store(db_session, elder_id=elder_id, messages=messages)
    await db_session.flush()
    assert count == 3

    rows = list(
        (
            await db_session.execute(
                MemoryChunk.__table__.select().where(MemoryChunk.elder_id == elder_id)
            )
        ).mappings()
    )
    assert len(rows) == 3
    by_content = {r["content"]: r for r in rows}
    health = by_content[HEALTH_CONTENT]
    assert health["category"] == "health"
    assert health["importance"] == 5
    assert health["visibility"] == "private"  # 默认 private（隔离红线）
    assert health["embedding_model"]  # 模型标注非空
    assert len(health["embedding"]) == 1024  # 0012 固定维度
    assert by_content["长者女儿每周探望"]["visibility"] == "care_team"  # 显式 care_team 生效


async def test_extract_llm_unavailable_skips_block(db_session, monkeypatch):
    """LLM 不可用 → 本块跳过萃取（返回 0），不抛异常不阻塞（可见降级红线）。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        raise LlmUnavailable("LLM 未配置")

    async def fail_embed(texts, *, transport=None):  # 不应被调用
        raise AssertionError("萃取失败后不应向量化")

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    monkeypatch.setattr(embedding_client, "embed_texts", fail_embed)

    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "今天天气不错")]
    )
    assert count == 0


async def test_extract_unparseable_output_skips(db_session, monkeypatch):
    """LLM 输出既无 tool_calls 也非合法 JSON → 跳过本块，不落脏数据。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        return {"content": "这不是JSON", "tool_calls": [], "raw_tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "随便聊聊")]
    )
    assert count == 0


async def test_extract_content_json_fallback(db_session, monkeypatch):
    """模型未走工具、正文输出 JSON 围栏 → 兜底解析仍可萃取。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        payload = json.dumps(
            {"memories": [{"content": "长者早睡早起", "category": "routine", "importance": 2}]}
        )
        return {
            "content": f"```json\n{payload}\n```",
            "tool_calls": [],
            "raw_tool_calls": [],
            "usage": None,
        }

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我平时九点就睡")]
    )
    assert count == 1


async def test_extract_invalid_items_dropped(db_session, monkeypatch):
    """非法 category / 空 content 条目丢弃；importance 越界收敛到 1-5。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        return _tool_call_result(
            [
                {"content": "", "category": "health", "importance": 3},  # 空 content → 丢
                # 非法枚举 → 丢
                {"content": "长者喜欢越剧", "category": "invalid_cat", "importance": 3},
                {"content": "长者爱喝茶", "category": "preference", "importance": 99},  # 收敛 5
            ]
        )

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我爱喝茶和越剧")]
    )
    assert count == 1


async def test_extract_embedding_unavailable_stores_nothing(db_session, monkeypatch):
    """embedding 不可用 → 整批不入库（不留无向量脏数据），返回 0。"""
    elder_id = await _make_elder(db_session)

    async def fake_chat(*args, **kwargs):
        return _tool_call_result(
            [{"content": "长者喜欢越剧", "category": "preference", "importance": 3}]
        )

    async def fake_embed(texts, *, transport=None):
        raise EmbeddingUnavailable("embedding 未配置")

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我想听越剧")]
    )
    assert count == 0


async def test_extract_anonymous_mode_zero(db_session, monkeypatch):
    """匿名/通用模式（elder_id=None）零个人记忆：不调 LLM 不落库（隔离红线）。"""

    async def fail_chat(*args, **kwargs):  # 匿名模式不应触发任何 LLM 调用
        raise AssertionError("匿名模式不应调用 LLM")

    monkeypatch.setattr(llm_client, "chat_with_tools", fail_chat)
    count = await memory_service.extract_and_store(
        db_session, elder_id=None, messages=[("user", "我有糖尿病")]
    )
    assert count == 0
    # 且库里不会因此多出任何记录
    total = len((await db_session.execute(MemoryChunk.__table__.select())).all())
    assert total == 0


# ---------------------------------------------------------------------------
# ③召回 + 隔离三件套
# ---------------------------------------------------------------------------


async def _seed_memory(db_session, monkeypatch, elder_id: UUID, *, status: str = "active"):
    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    rows = await memory_service.store_memories(
        db_session,
        elder_id=elder_id,
        items=[{"content": HEALTH_CONTENT, "category": "health", "importance": 5}],
    )
    if status != "active":
        rows[0].status = status
    await db_session.flush()
    return rows[0]


async def _patch_query_vec(monkeypatch, index: int = 0):
    async def fake_embed_text(text, *, transport=None):
        return _vec(index)

    monkeypatch.setattr(embedding_client, "embed_text", fake_embed_text)


async def test_recall_hits_for_owner(db_session, monkeypatch):
    """本人在场：手工插记忆 → 相同向量召回命中（步 2 观察现象）。"""
    elder_id = await _make_elder(db_session)
    await _seed_memory(db_session, monkeypatch, elder_id)
    await _patch_query_vec(monkeypatch)

    hits = await memory_service.recall_memories(
        db_session, elder_id=elder_id, query_text="能吃粽子吗"
    )
    assert len(hits) == 1
    assert hits[0].content == HEALTH_CONTENT


async def test_recall_forged_and_cross_elder_empty(db_session, monkeypatch):
    """隔离三件套之二：伪造他人 elder_id / 跨 elder 均为空集。

    服务签名层面不存在「请求参数指定查询主体」的旁路——elder_id 形参即服务端
    Runtime Context 注入值；越权传入他人 id 时，过滤条件严格落在该 id 上，
    拿到的是越权者自己的（空）记忆集，绝不泄漏他人记忆。
    """
    victim_id = await _make_elder(db_session)  # 王奶奶：有记忆
    attacker_id = await _make_elder(db_session)  # 李爷爷：无记忆
    await _seed_memory(db_session, monkeypatch, victim_id)
    await _patch_query_vec(monkeypatch)

    # 伪造/跨 elder：查询词与王奶奶记忆完全同义，仍为空
    assert (
        await memory_service.recall_memories(
            db_session, elder_id=attacker_id, query_text="我有糖尿病"
        )
        == []
    )


async def test_recall_anonymous_empty(db_session, monkeypatch):
    """匿名/通用模式（elder_id=None）零个人记忆，且不做任何 embedding/DB 查询。"""
    victim_id = await _make_elder(db_session)
    await _seed_memory(db_session, monkeypatch, victim_id)

    async def fail_embed(text, *, transport=None):
        raise AssertionError("匿名模式不应触发 embedding 调用")

    monkeypatch.setattr(embedding_client, "embed_text", fail_embed)
    hits = await memory_service.recall_memories(
        db_session, elder_id=None, query_text="糖尿病"
    )
    assert hits == []


async def test_recall_filters_status_and_orders_by_distance(db_session, monkeypatch):
    """status='active' 强制过滤（superseded 不可召回）；命中按余弦距离升序。"""
    elder_id = await _make_elder(db_session)
    hit_row = await _seed_memory(db_session, monkeypatch, elder_id)  # vec(0)
    await _seed_memory(db_session, monkeypatch, elder_id, status="superseded")

    # 再插一条不相关记忆（vec(5)，与查询 vec(0) 余弦距离更远）
    async def fake_embed(texts, *, transport=None):
        return [_vec(5) for _ in texts]

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    unrelated = (
        await memory_service.store_memories(
            db_session,
            elder_id=elder_id,
            items=[{"content": "长者喜欢晨练", "category": "routine", "importance": 2}],
        )
    )[0]
    await _patch_query_vec(monkeypatch, index=0)

    hits = await memory_service.recall_memories(
        db_session, elder_id=elder_id, query_text="糖尿病饮食注意"
    )
    # superseded 被过滤；active 命中按余弦距离升序（相同向量在前，不相关在后）
    assert [h.chunk_id for h in hits] == [hit_row.chunk_id, unrelated.chunk_id]


async def test_recall_embedding_unavailable_empty(db_session, monkeypatch):
    """embedding 不可用 → 召回空集（无记忆直答），不阻塞对话（可见降级红线）。"""
    elder_id = await _make_elder(db_session)
    await _seed_memory(db_session, monkeypatch, elder_id)

    async def fake_embed(text, *, transport=None):
        raise EmbeddingUnavailable("embedding 超时")

    monkeypatch.setattr(embedding_client, "embed_text", fake_embed)
    assert (
        await memory_service.recall_memories(
            db_session, elder_id=elder_id, query_text="能吃粽子吗"
        )
        == []
    )


@pytest.mark.parametrize("bad_text", ["", "   "])
async def test_recall_blank_query_empty(db_session, monkeypatch, bad_text):
    """空查询直接空集，不浪费 embedding 调用。"""
    elder_id = await _make_elder(db_session)
    hits = await memory_service.recall_memories(
        db_session, elder_id=elder_id, query_text=bad_text
    )
    assert hits == []
