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
from app.models.enums import MemoryStatus
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


# ---------------------------------------------------------------------------
# 步 3：④二次过滤 ⑤冲突检测 遗忘归并
# ---------------------------------------------------------------------------


def _chunk(elder_id, content, vec_index, *, importance=3):
    from datetime import UTC, datetime

    return MemoryChunk(
        elder_id=elder_id,
        content=content,
        embedding=_vec(vec_index),
        embedding_model="test-model",
        category="preference",
        visibility="private",
        importance=importance,
        status="active",
        created_at=datetime.now(UTC),
    )


def _tool_result(name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": "call-x", "name": name, "arguments": arguments}],
        "raw_tool_calls": [],
        "usage": {"total_tokens": 1},
    }


async def _add_chunk(db_session, elder_id, content, vec_index, *, importance=3):
    row = _chunk(elder_id, content, vec_index, importance=importance)
    db_session.add(row)
    await db_session.flush()
    return row


async def _backdate(db_session, row, days: float):
    from datetime import UTC, datetime, timedelta

    row.created_at = datetime.now(UTC) - timedelta(days=days)
    await db_session.flush()
    return row


# ----- ④ 召回后二次过滤 -----


async def test_filter_keeps_relevant_only(db_session, monkeypatch):
    """5 候选含噪声 → LLM 只保留真相关条目，保持余弦距离序，噪声不进 prompt。"""
    elder_id = await _make_elder(db_session)
    rows = [
        await _add_chunk(db_session, elder_id, f"记忆{i}", i, importance=3) for i in range(5)
    ]

    async def fake_chat(*args, **kwargs):
        keep_ids = [str(rows[0].chunk_id), str(rows[2].chunk_id), str(rows[4].chunk_id)]
        return _tool_result("select_relevant_memories", {"relevant_ids": keep_ids})

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    filtered = await memory_service.filter_recalled("糖尿病饮食", rows)
    assert [c.chunk_id for c in filtered] == [rows[0].chunk_id, rows[2].chunk_id, rows[4].chunk_id]


async def test_filter_llm_unavailable_degrades_top2_importance(db_session, monkeypatch):
    """LLM 不可用 → 降级取前 2 条最高 importance（保持原序，大纲 7.1 原文口径）。"""
    elder_id = await _make_elder(db_session)
    rows = [
        await _add_chunk(db_session, elder_id, "低价值", 0, importance=1),
        await _add_chunk(db_session, elder_id, "核心", 1, importance=5),
        await _add_chunk(db_session, elder_id, "一般", 2, importance=3),
        await _add_chunk(db_session, elder_id, "重要", 3, importance=4),
    ]

    async def fail_chat(*args, **kwargs):
        raise LlmUnavailable("LLM 未配置")

    monkeypatch.setattr(llm_client, "chat_with_tools", fail_chat)
    filtered = await memory_service.filter_recalled("随便问", rows)
    assert [c.importance for c in filtered] == [5, 4]  # top2 by importance
    assert [c.content for c in filtered] == ["核心", "重要"]  # 保持原（余弦距离）序


async def test_filter_unparseable_output_degrades(db_session, monkeypatch):
    """LLM 输出不可解析 → 同样走 top2 importance 降级路径。"""
    elder_id = await _make_elder(db_session)
    rows = [
        await _add_chunk(db_session, elder_id, "甲", 0, importance=2),
        await _add_chunk(db_session, elder_id, "乙", 1, importance=5),
        await _add_chunk(db_session, elder_id, "丙", 2, importance=1),
    ]

    async def fake_chat(*args, **kwargs):
        return {"content": "我不用工具", "tool_calls": [], "raw_tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    filtered = await memory_service.filter_recalled("问题", rows)
    # top2 by importance（5、2），保持原（余弦距离）序
    assert [c.content for c in filtered] == ["甲", "乙"]


# ----- ⑤ 冲突检测（写入前）-----


async def test_conflict_supersedes_old_memory(db_session, monkeypatch):
    """「上周不吃甜→这周想吃月饼」：相似度>0.9 且 LLM 判矛盾 → 旧记忆 superseded。"""
    elder_id = await _make_elder(db_session)
    old_row = await _add_chunk(db_session, elder_id, "长者不吃甜食", 0, importance=3)

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]  # 新旧同向量（相似度 1.0 > 0.9）

    async def fake_chat(messages, tools, **kwargs):
        tool_name = tools[0]["name"]
        if tool_name == "submit_memories":  # 第 1 次：萃取
            return _tool_result(
                "submit_memories",
                {
                    "memories": [
                        {"content": "长者想吃月饼", "category": "preference", "importance": 3}
                    ]
                },
            )
        # 第 2 次：冲突检测
        return _tool_result(
            "report_memory_conflicts",
            {"conflicts": [{"old_id": str(old_row.chunk_id), "new_index": 0}]},
        )

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "这周我想吃月饼")]
    )
    await db_session.flush()
    assert count == 1
    assert old_row.status == MemoryStatus.SUPERSEDED  # 旧记忆标记、保留可追溯（不物理删除）
    active = list(
        (
            await db_session.execute(
                MemoryChunk.__table__.select().where(
                    MemoryChunk.elder_id == elder_id,
                    MemoryChunk.__table__.c.status == "active",
                )
            )
        ).mappings()
    )
    assert len(active) == 1 and active[0]["content"] == "长者想吃月饼"  # 不追加矛盾记录


async def test_conflict_not_contradictory_keeps_both(db_session, monkeypatch):
    """表述不同但不矛盾 → 双双保持 active。"""
    elder_id = await _make_elder(db_session)
    old_row = await _add_chunk(db_session, elder_id, "长者喜欢越剧", 0, importance=3)

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]

    async def fake_chat(messages, tools, **kwargs):
        if tools[0]["name"] == "submit_memories":
            return _tool_result(
                "submit_memories",
                {
                    "memories": [
                        {"content": "长者也爱看京剧", "category": "preference", "importance": 3}
                    ]
                },
            )
        return _tool_result("report_memory_conflicts", {"conflicts": []})

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我也爱看京剧")]
    )
    await db_session.flush()
    assert old_row.status == "active"
    total = len(
        (await db_session.execute(MemoryChunk.__table__.select())).all()
    )
    assert total == 2


async def test_conflict_llm_unavailable_stores_anyway(db_session, monkeypatch):
    """冲突检测 LLM 失败 → 跳过检测照常写入（宁可多记，不阻塞）。"""
    elder_id = await _make_elder(db_session)
    old_row = await _add_chunk(db_session, elder_id, "长者不吃甜食", 0, importance=3)

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]

    async def fake_chat(messages, tools, **kwargs):
        if tools[0]["name"] == "submit_memories":
            return _tool_result(
                "submit_memories",
                {
                    "memories": [
                        {"content": "长者想吃月饼", "category": "preference", "importance": 3}
                    ]
                },
            )
        raise LlmUnavailable("超时")

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我想吃月饼了")]
    )
    assert count == 1
    assert old_row.status == "active"  # 未被误标，检测降级可见


async def test_conflict_no_similar_candidates_skips_llm(db_session, monkeypatch):
    """无相似度>0.9 的旧记忆 → 不发起 LLM 冲突比对。"""
    elder_id = await _make_elder(db_session)
    await _add_chunk(db_session, elder_id, "长者喜欢晨练", 7, importance=3)  # vec(7) 与 vec(0) 正交

    async def fake_embed(texts, *, transport=None):
        return [_vec(0) for _ in texts]  # 新记忆 vec(0)，与旧 vec(7) 相似度 0

    # 按 tool 名分流：萃取正常应答；若冲突比对被触发则测试失败
    async def fake_chat(messages, tools, **kwargs):
        if tools[0]["name"] == "submit_memories":
            return _tool_result(
                "submit_memories",
                {
                    "memories": [
                        {"content": "长者爱吃粽子", "category": "preference", "importance": 3}
                    ]
                },
            )
        raise AssertionError("无相似候选时不应调用冲突比对")

    monkeypatch.setattr(embedding_client, "embed_texts", fake_embed)
    monkeypatch.setattr(llm_client, "chat_with_tools", fake_chat)
    count = await memory_service.extract_and_store(
        db_session, elder_id=elder_id, messages=[("user", "我爱吃粽子")]
    )
    assert count == 1


# ----- ⑤ 遗忘归并（确定性，零模型）-----


def test_relevance_score_decay():
    """importance × 90 天线性衰减：新记忆=importance，过窗=0。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    fresh = MemoryChunk(importance=3, created_at=now)
    mid = MemoryChunk(importance=3, created_at=now - timedelta(days=45))
    old = MemoryChunk(importance=5, created_at=now - timedelta(days=91))
    assert memory_service.relevance_score(fresh, now=now) == 3.0
    assert memory_service.relevance_score(mid, now=now) == 1.5
    assert memory_service.relevance_score(old, now=now) == 0.0


async def test_weekly_merge_dedup_keeps_best(db_session):
    """重复归并：相似度>0.95 簇内保留 importance 最高，其余 superseded；跨 elder 互不影响。"""
    e1 = await _make_elder(db_session)
    e2 = await _make_elder(db_session)
    low = await _add_chunk(db_session, e1, "长者爱吃粽子（记录A）", 0, importance=3)
    high = await _add_chunk(db_session, e1, "长者爱吃粽子（记录B）", 0, importance=5)
    other = await _add_chunk(db_session, e2, "长者爱吃粽子（他人）", 0, importance=3)

    stats = await memory_service.weekly_merge(db_session)
    await db_session.flush()
    assert stats["superseded_duplicates"] == 1
    assert low.status == MemoryStatus.SUPERSEDED
    assert high.status == MemoryStatus.ACTIVE  # 保留 importance 最高
    assert other.status == MemoryStatus.ACTIVE  # 跨 elder 不参与归并（隔离红线）


async def test_weekly_merge_cold_tiering(db_session):
    """冷热分层：importance≤2 且超 90 天 → suppressed；高重要性永不被衰减隐藏。"""
    elder_id = await _make_elder(db_session)
    cold = await _add_chunk(db_session, elder_id, "随口聊的天气", 0, importance=2)
    await _backdate(db_session, cold, 100)
    hot = await _add_chunk(db_session, elder_id, "核心健康事实", 1, importance=5)
    await _backdate(db_session, hot, 100)
    fresh_low = await _add_chunk(db_session, elder_id, "昨天随口一提", 2, importance=2)

    stats = await memory_service.weekly_merge(db_session)
    await db_session.flush()
    assert stats["suppressed_cold"] == 1
    assert cold.status == MemoryStatus.SUPPRESSED
    assert hot.status == MemoryStatus.ACTIVE  # importance≥3 不衰减隐藏（糖尿病场景锚点）
    assert fresh_low.status == MemoryStatus.ACTIVE  # 未过 90 天窗口
