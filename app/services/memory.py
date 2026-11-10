"""MemoryService：长期记忆五步链路（Day7，大纲 7.1 / US-03 / POC-07）。

五步口径：①萃取（本模块 extract_memories）→ ②存储（store_memories）
→ ③召回（recall_memories）→ ④召回后二次过滤 / ⑤冲突检测与遗忘（步 3 扩展）。

铁律（一票否决，大纲 7.1 高危点原文）：
- elder_id 只从服务端 Runtime Context 注入（调用方传 verified_elder_id），
  任何 API 参数 / prompt 内容 / 前端传入都不可信——本模块所有函数的 elder_id
  形参即「运行时主体」，无任何按请求参数切换查询主体的旁路；
- 所有查询强制 elder_id + visibility + status 过滤，匿名/通用模式（elder_id
  为 None）返回空集；
- 萃取/召回任何失败不阻塞对话：萃取失败跳过本块、召回失败走无记忆直答，
  统一如实记日志（严禁静默 mock，AGENTS 降级红线）。

为什么用 function calling 萃取：content/category/importance 是结构化契约，
tools 参数即 JSON schema，模型输出天然可校验；正文 JSON 解析仅作降级兜底。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations import embedding as embedding_client
from app.integrations import llm as llm_client
from app.integrations.embedding import EmbeddingUnavailable
from app.integrations.llm import LlmUnavailable
from app.models.enums import MemoryStatus, MemoryVisibility
from app.models.memory import MemoryChunk

logger = logging.getLogger(__name__)

# 每块最多萃取 3 条（大纲 7.1：短 prompt 无对话历史，开销可控）
EXTRACT_MAX_ITEMS = 3
# 召回 top-5（大纲 7.1：向量余弦 top-5）
RECALL_TOP_K = 5
# content 落库长度上限（超长截断，防 LLM 异常输出撑爆 TEXT 语义）
CONTENT_MAX_LEN = 500
# ⑤ 冲突检测：余弦相似度超过该值的旧记忆进入 LLM 语义矛盾比对。
# 大纲 7.1 原文 0.9，经 POC-07 实测校准：qwen3.7-text-embedding-flash 对改述句
# 实测仅 ~0.84（「控糖」vs「想吃月饼适量」0.663），0.9 会导致冲突比对永不触发；
# 阈值只决定「谁进入比对」，是否真矛盾由 LLM 最终裁判，放宽安全。
CONFLICT_SIMILARITY = 0.75
# ⑤ 遗忘归并：确定性去重（无 LLM 裁判）故保持高精度
MERGE_SIMILARITY = 0.90
# ⑤ 遗忘衰减窗口：90 天线性衰减（大纲 7.1 原文）
DECAY_DAYS = 90.0
# ⑤ 冷热分层：低重要性（≤2）且超过衰减窗口的记忆转入 suppressed（冷）
COLD_IMPORTANCE_MAX = 2

_VALID_CATEGORIES = {
    "preference",
    "health",
    "event",
    "relationship",
    "routine",
    "other",
}
_VALID_VISIBILITY = {MemoryVisibility.PRIVATE, MemoryVisibility.CARE_TEAM}

# 萃取工具：parameters 即 JSON schema（function calling 强制结构化输出）
_EXTRACT_TOOL: dict[str, Any] = {
    "name": "submit_memories",
    "description": "提交从对话记录中萃取的结构化长期记忆（最多 3 条，无则空数组）",
    "parameters": {
        "type": "object",
        "properties": {
            "memories": {
                "type": "array",
                "maxItems": EXTRACT_MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "一句第三人称陈述，以「长者」开头，不含姓名等身份信息",
                        },
                        "category": {
                            "type": "string",
                            "enum": sorted(_VALID_CATEGORIES),
                        },
                        "visibility": {
                            "type": "string",
                            "enum": ["private", "care_team"],
                            "description": "默认 private（仅本人）；care_team 为授权医护共享",
                        },
                        "importance": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "1=随口一提 3=一般事实 5=核心健康/偏好事实",
                        },
                    },
                    "required": ["content", "category", "importance"],
                },
            }
        },
        "required": ["memories"],
    },
}

_EXTRACT_SYSTEM_PROMPT = (
    "你是养老陪伴数字人的记忆萃取模块。从对话记录中提取值得长期记住的事实，最多 3 条。"
    "规则：\n"
    "1. 只提取明确的事实性信息（偏好/健康/事件/人际/日常规律），不推测、不编造；\n"
    "2. content 用一句第三人称陈述，以「长者」开头，不出现姓名与证件号等身份信息；\n"
    "3. health 类仅记录长者主动告知的日常健康事实，不做任何医学判断；\n"
    "4. 无值得记住的内容时返回空数组。\n"
    "5. visibility 默认 private；仅当是需医护协同跟进的健康/照护事项才用 care_team。\n"
    "必须调用 submit_memories 工具提交结果。"
)


# ---------------------------------------------------------------------------
# ① 萃取
# ---------------------------------------------------------------------------


def _format_transcript(messages: Sequence[Any]) -> str:
    """对话块消息 → 短 transcript；支持 (role, content) 元组 / dict / ORM 对象。"""
    lines: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            role, content = m.get("role", "user"), m.get("content", "")
        elif isinstance(m, tuple):
            role, content = m[0], m[1]
        else:
            role, content = getattr(m, "role", "user"), getattr(m, "content", "")
        content = str(content).strip()
        if content:
            lines.append(f"{'用户' if role == 'user' else '助手'}：{content}")
    return "\n".join(lines)


def _parse_extraction(result: dict[str, Any]) -> list[dict[str, Any]]:
    """chat_with_tools 结果 → 原始记忆列表：优先 tool_calls，正文 JSON 兜底。"""
    for tc in result.get("tool_calls") or []:
        if tc.get("name") == "submit_memories":
            memories = tc.get("arguments", {}).get("memories")
            if isinstance(memories, list):
                return memories
    content = result.get("content")
    if isinstance(content, str) and content.strip():
        text = content.strip()
        if text.startswith("```"):  # 剥 markdown 代码围栏兜底
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("memories"), list):
            return parsed["memories"]
        if isinstance(parsed, list):
            return parsed
    return []


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    """单条记忆校验/归一：非法 category 或空 content 的条目直接丢弃（宁可少存不存脏）。"""
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()
    if not content:
        return None
    category = str(raw.get("category") or "").strip()
    if category not in _VALID_CATEGORIES:
        return None
    visibility = str(raw.get("visibility") or MemoryVisibility.PRIVATE).strip()
    if visibility not in _VALID_VISIBILITY:
        visibility = MemoryVisibility.PRIVATE
    try:
        importance = int(raw.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3
    importance = max(1, min(5, importance))
    return {
        "content": content[:CONTENT_MAX_LEN],
        "category": category,
        "visibility": visibility,
        "importance": importance,
    }


async def extract_memories(
    messages: Sequence[Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """① 萃取：对话块消息 → 0–3 条验证过的记忆 dict。

    LLM 未配置/调用失败/输出不可解析时抛异常，由调用方决定跳过本块
    （extract_and_store 已内置捕获）；transport 仅供测试注入。
    """
    transcript = _format_transcript(messages)
    if not transcript:
        return []
    result = await llm_client.chat_with_tools(
        [
            {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"对话记录：\n{transcript}"},
        ],
        [_EXTRACT_TOOL],
        transport=transport,
    )
    return [
        item
        for item in (_normalize_item(raw) for raw in _parse_extraction(result))
        if item is not None
    ][:EXTRACT_MAX_ITEMS]


# ---------------------------------------------------------------------------
# ② 存储
# ---------------------------------------------------------------------------


async def store_memories(
    db: AsyncSession,
    *,
    elder_id: UUID,
    items: Sequence[dict[str, Any]],
    transport: httpx.AsyncBaseTransport | None = None,
    vectors: list[list[float]] | None = None,
) -> list[MemoryChunk]:
    """② 存储：content 向量化（1024 维）+ 写 memory_chunks。

    elder_id 为服务端 Runtime Context 注入值（本函数不接受任何「请求方主张」）；
    vectors 可传预计算向量（extract_and_store 已 embed 一次，避免重复调用）；
    embedding 失败抛 EmbeddingUnavailable，由调用方降级（整批不入库，不留脏数据）。
    """
    if not items:
        return []
    contents = [item["content"] for item in items]
    if vectors is None:
        vectors = await embedding_client.embed_texts(contents, transport=transport)
    settings = get_settings()
    rows = [
        MemoryChunk(
            elder_id=elder_id,
            content=item["content"],
            embedding=vector,
            embedding_model=settings.embedding_model,
            category=item["category"],
            visibility=item.get("visibility", MemoryVisibility.PRIVATE),
            importance=item["importance"],
        )
        for item, vector in zip(items, vectors, strict=True)
    ]
    db.add_all(rows)
    await db.flush()
    return rows


async def extract_and_store(
    db: AsyncSession,
    *,
    elder_id: UUID | None,
    messages: Sequence[Any],
    transport: httpx.AsyncBaseTransport | None = None,
    embed_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """块结束钩子入口（步 4 接线用）：①萃取 + ②存储，失败返回 0 不抛。

    匿名/通用模式（elder_id 为 None）不萃取任何个人记忆（隔离红线）；
    LLM 或 embedding 失败 → 本块跳过萃取，如实记日志，不阻塞对话。
    """
    if elder_id is None:
        return 0
    try:
        items = await extract_memories(messages, transport=transport)
    except LlmUnavailable as exc:
        logger.warning(
            "memory extract skipped (llm unavailable)",
            extra={"event": "memory_extract_unavailable", "error": str(exc)},
        )
        return 0
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "memory extract skipped (unparseable output)",
            extra={"event": "memory_extract_parse_failed", "error": str(exc)},
        )
        return 0
    if not items:
        return 0
    try:
        # embed 一次：冲突检测与存储共用同一批向量（避免重复调用 embedding API）
        vectors = await embedding_client.embed_texts(
            [item["content"] for item in items], transport=embed_transport
        )
    except EmbeddingUnavailable as exc:
        logger.warning(
            "memory store skipped (embedding unavailable)",
            extra={"event": "memory_store_unavailable", "error": str(exc)},
        )
        return 0
    # ⑤ 冲突检测：写入前标记被新事实取代的旧记忆（失败不阻塞写入）
    await detect_and_supersede(
        db, elder_id=elder_id, new_items=items, new_vectors=vectors, transport=transport
    )
    rows = await store_memories(db, elder_id=elder_id, items=items, vectors=vectors)
    logger.info(
        "memory extracted and stored",
        extra={
            "event": "memory_extracted",
            "elder_id": str(elder_id),
            "count": len(rows),
        },
    )
    return len(rows)


# ---------------------------------------------------------------------------
# ③ 召回
# ---------------------------------------------------------------------------


async def recall_memories(
    db: AsyncSession,
    *,
    elder_id: UUID | None,
    query_text: str,
    top_k: int = RECALL_TOP_K,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[MemoryChunk]:
    """③ 召回：查询文本 → pgvector 余弦 top-k，强制隔离过滤。

    隔离三件套（一票否决验收项）：
    - elder_id 精确匹配（服务端 Runtime Context 注入；伪造/他人 elder_id 得到的
      只是他自己的记忆集——越权者拿不到任何他人记忆）；
    - status='active'（superseded/suppressed/deleted 一律不可召回）；
    - visibility 过滤（本人口径 private+care_team 均可见）；
    - 匿名/通用模式（elder_id 为 None）→ 空集，不做任何查询。
    embedding 失败 → 返回空集（无记忆直答），不阻塞对话。
    """
    if elder_id is None or not query_text.strip():
        return []
    try:
        query_vec = await embedding_client.embed_text(query_text, transport=transport)
    except EmbeddingUnavailable as exc:
        logger.warning(
            "memory recall skipped (embedding unavailable)",
            extra={"event": "memory_recall_unavailable", "error": str(exc)},
        )
        return []
    stmt = (
        select(MemoryChunk)
        .where(
            MemoryChunk.elder_id == elder_id,
            MemoryChunk.status == MemoryStatus.ACTIVE,
            MemoryChunk.visibility.in_(_VALID_VISIBILITY),
        )
        .order_by(MemoryChunk.embedding.cosine_distance(query_vec))
        .limit(top_k)
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# ④ 召回后二次过滤（Post-retrieval Reasoning）
# ---------------------------------------------------------------------------

_FILTER_TOOL: dict[str, Any] = {
    "name": "select_relevant_memories",
    "description": "从候选长期记忆中选出与当前问题真相关的条目 id（无相关则空数组）",
    "parameters": {
        "type": "object",
        "properties": {
            "relevant_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "与当前问题真相关的候选记忆 chunk_id 列表",
            }
        },
        "required": ["relevant_ids"],
    },
}

_FILTER_SYSTEM_PROMPT = (
    "你是养老陪伴数字人的记忆过滤模块。给定当前问题和候选长期记忆，"
    "选出对回答当前问题真有帮助的记忆 id：\n"
    "1. 只保留与问题主题直接相关的条目，剔除仅词语重叠的噪声；\n"
    "2. 不确定是否相关的一律剔除（宁缺勿滥，避免污染回复）；\n"
    "3. 必须调用 select_relevant_memories 工具提交结果。"
)


def _parse_ids(result: dict[str, Any], tool_name: str, list_key: str) -> list[str] | None:
    """通用解析：优先 tool_calls，正文 JSON 兜底。

    返回 id 列表；返回 None 表示输出完全不可解析（既无工具调用也无合法 JSON），
    由调用方决定降级路径（区别于「LLM 明确返回空列表」）。
    """
    for tc in result.get("tool_calls") or []:
        if tc.get("name") == tool_name:
            items = tc.get("arguments", {}).get(list_key)
            if isinstance(items, list):
                return [str(i) for i in items if isinstance(i, (str, int))]
    content = result.get("content")
    if isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content.strip().strip("`").removeprefix("json"))
        except ValueError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get(list_key), list):
            return [str(i) for i in parsed[list_key]]
        if isinstance(parsed, list):
            return [str(i) for i in parsed]
    return None


async def filter_recalled(
    query_text: str,
    candidates: Sequence[MemoryChunk],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[MemoryChunk]:
    """④ 二次过滤：top-5 候选不直接进 prompt——LLM 判断真相关性后组装。

    LLM 不可用/输出不可解析 → 降级取前 2 条最高 importance（降级路径可测，
    大纲 7.1 原文口径）。返回保持原有余弦距离序（最相关在前）。
    """
    if not candidates:
        return []
    listing = "\n".join(f"- {c.chunk_id}: {c.content}" for c in candidates)
    try:
        result = await llm_client.chat_with_tools(
            [
                {"role": "system", "content": _FILTER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"当前问题：{query_text}\n候选记忆：\n{listing}",
                },
            ],
            [_FILTER_TOOL],
            transport=transport,
        )
        parsed = _parse_ids(result, "select_relevant_memories", "relevant_ids")
        if parsed is None:  # 输出完全不可解析 → 与 LLM 不可用同走降级
            raise LlmUnavailable("过滤输出不可解析")
        keep = set(parsed)
        filtered = [c for c in candidates if str(c.chunk_id) in keep]
        if not filtered:
            logger.info(
                "memory filter removed all candidates",
                extra={"event": "memory_filter_empty", "candidates": len(candidates)},
            )
        return filtered
    except LlmUnavailable as exc:
        logger.warning(
            "memory filter degraded (llm unavailable)",
            extra={"event": "memory_filter_degraded", "error": str(exc)},
        )
        top2 = sorted(candidates, key=lambda c: c.importance, reverse=True)[:2]
        return [c for c in candidates if c in top2]


# ---------------------------------------------------------------------------
# ⑤ 冲突检测（写入前）与遗忘归并
# ---------------------------------------------------------------------------

_CONFLICT_TOOL: dict[str, Any] = {
    "name": "report_memory_conflicts",
    "description": "报告被新记忆取代的旧记忆（语义矛盾，如偏好/健康事实随时间更新）",
    "parameters": {
        "type": "object",
        "properties": {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_id": {"type": "string", "description": "被取代的旧记忆 chunk_id"},
                        "new_index": {"type": "integer", "description": "取代它的新记忆序号"},
                    },
                    "required": ["old_id", "new_index"],
                },
            }
        },
        "required": ["conflicts"],
    },
}

_CONFLICT_SYSTEM_PROMPT = (
    "你是养老陪伴数字人的记忆冲突检测模块。给定若干条新记忆和与它们高度相似的旧记忆，"
    "判断哪些旧记忆已被新事实取代（语义矛盾）：\n"
    "1. 同一事实随时间更新算矛盾，旧记忆被取代——尤其偏好与饮食习惯"
    "（如「不敢吃甜」→「想吃甜的/想吃月饼」是最新意愿，取代旧约束）；\n"
    "2. 表述不同但不矛盾的补充细节、并列偏好不算矛盾；\n"
    "3. 只报告确实矛盾的旧记忆 id，不推测；\n"
    "4. 必须调用 report_memory_conflicts 工具提交结果。"
)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


async def detect_and_supersede(
    db: AsyncSession,
    *,
    elder_id: UUID,
    new_items: Sequence[dict[str, Any]],
    new_vectors: Sequence[Sequence[float]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """⑤ 冲突检测：写入前相似度>0.9 的旧记忆经 LLM 语义比对矛盾 → superseded。

    旧记忆保留可追溯（不物理删除，status 置 superseded 即退出召回）；
    检测只在同一 elder 内进行（隔离红线）；LLM 失败 → 跳过检测照常写入
    （宁可多记不阻塞），返回被取代条数。
    """
    # 1) 找相似旧记忆：每条新记忆取余弦距离最近 3 条且相似度 > 0.9
    candidates: dict[UUID, tuple[MemoryChunk, int]] = {}
    for idx, vec in enumerate(new_vectors):
        distance_cut = 1.0 - CONFLICT_SIMILARITY
        stmt = (
            select(MemoryChunk)
            .where(
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.status == MemoryStatus.ACTIVE,
                MemoryChunk.embedding.cosine_distance(list(vec)) < distance_cut,
            )
            .order_by(MemoryChunk.embedding.cosine_distance(list(vec)))
            .limit(3)
        )
        for row in (await db.execute(stmt)).scalars().all():
            candidates.setdefault(row.chunk_id, (row, idx))
    if not candidates:
        return 0
    # 2) LLM 语义比对：是否真的矛盾
    new_listing = "\n".join(
        f"{i}. {item['content']}" for i, item in enumerate(new_items)
    )
    old_listing = "\n".join(f"- {cid}: {row.content}" for cid, (row, _) in candidates.items())
    try:
        result = await llm_client.chat_with_tools(
            [
                {"role": "system", "content": _CONFLICT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"新记忆：\n{new_listing}\n\n相似旧记忆：\n{old_listing}",
                },
            ],
            [_CONFLICT_TOOL],
            transport=transport,
        )
    except LlmUnavailable as exc:
        logger.warning(
            "memory conflict check skipped (llm unavailable)",
            extra={"event": "memory_conflict_unavailable", "error": str(exc)},
        )
        return 0
    superseded = 0
    valid_new_indices = set(range(len(new_items)))
    for old_id, new_index in _parse_conflicts(result):
        hit = candidates.get(UUID(old_id)) if old_id else None
        if hit is None or new_index not in valid_new_indices:
            continue
        row, _ = hit
        if row.status == MemoryStatus.ACTIVE:
            row.status = MemoryStatus.SUPERSEDED
            superseded += 1
            logger.info(
                "memory superseded by newer fact",
                extra={
                    "event": "memory_superseded",
                    "elder_id": str(elder_id),
                    "old_chunk_id": str(row.chunk_id),
                    "new_index": new_index,
                },
            )
    if superseded:
        await db.flush()
    return superseded


def _parse_conflicts(result: dict[str, Any]) -> list[tuple[str, int]]:
    """冲突结果解析：[(old_id, new_index)]；tool_calls 优先，正文 JSON 兜底。"""
    raw: list[Any] = []
    for tc in result.get("tool_calls") or []:
        if tc.get("name") == "report_memory_conflicts":
            items = tc.get("arguments", {}).get("conflicts")
            if isinstance(items, list):
                raw = items
                break
    if not raw:
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content.strip().strip("`").removeprefix("json"))
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("conflicts"), list):
                raw = parsed["conflicts"]
    out: list[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        old_id, new_index = item.get("old_id"), item.get("new_index")
        if isinstance(old_id, str) and isinstance(new_index, int):
            out.append((old_id, new_index))
    return out


def relevance_score(chunk: MemoryChunk, *, now: datetime | None = None) -> float:
    """遗忘打分（确定性，零模型）：importance × 90 天线性衰减权重。

    大纲 7.1「importance 打分 + 90 天衰减」：新记忆权重=importance，
    随时间线性衰减至 0；冷热分层与归并排序均以该分数为准。
    """
    now = now or datetime.now(UTC)
    created = chunk.created_at
    if created is None:
        return float(chunk.importance)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created).total_seconds() / 86400)
    decay = max(0.0, 1.0 - age_days / DECAY_DAYS)
    return chunk.importance * decay


async def weekly_merge(
    db: AsyncSession,
    *,
    elder_id: UUID | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """⑤ 周归并任务（APScheduler 周任务承载，可手动触发验证）：重复归并 + 冷热分层。

    确定性实现，零模型零 token（大纲红线：确定性逻辑不调模型）：
    - 重复归并：同 elder 内余弦相似度 > 0.95 的记忆簇 → 保留 importance 最高
      （并列取最新）一条，其余 superseded（保留可追溯）；
    - 冷热分层：低重要性（importance ≤ 2）且超过 90 天衰减窗口 → suppressed；
      importance ≥ 3 的记忆（如核心健康事实）不衰减隐藏，永远可召回。
    elder_id=None 处理全部长者（周任务口径）；返回统计 dict。
    """
    now = now or datetime.now(UTC)
    stmt = select(MemoryChunk.elder_id).where(MemoryChunk.status == MemoryStatus.ACTIVE)
    if elder_id is not None:
        stmt = stmt.where(MemoryChunk.elder_id == elder_id)
    elder_ids = list((await db.execute(stmt.distinct())).scalars().all())
    stats = {"elders": len(elder_ids), "superseded_duplicates": 0, "suppressed_cold": 0}
    for eid in elder_ids:
        rows = list(
            (
                await db.execute(
                    select(MemoryChunk)
                    .where(
                        MemoryChunk.elder_id == eid,
                        MemoryChunk.status == MemoryStatus.ACTIVE,
                    )
                    .order_by(MemoryChunk.created_at)
                )
            )
            .scalars()
            .all()
        )
        # ① 冷热分层（确定性）
        for row in rows:
            if row.importance <= COLD_IMPORTANCE_MAX and relevance_score(row, now=now) <= 0.0:
                row.status = MemoryStatus.SUPPRESSED
                stats["suppressed_cold"] += 1
        # ② 重复归并（确定性：相似度 > 0.95 的簇内保留 importance 最高（并列取最新））
        for row in rows:
            if row.status != MemoryStatus.ACTIVE:
                continue
            dupes = [
                other
                for other in rows
                if other is not row
                and other.status == MemoryStatus.ACTIVE
                and _cosine_similarity(row.embedding, other.embedding) > MERGE_SIMILARITY
            ]
            if not dupes:
                continue
            best = max([row, *dupes], key=lambda c: (c.importance, c.created_at or now))
            for c in [row, *dupes]:
                if c is not best and c.status == MemoryStatus.ACTIVE:
                    c.status = MemoryStatus.SUPERSEDED
                    stats["superseded_duplicates"] += 1
    if stats["superseded_duplicates"] or stats["suppressed_cold"]:
        await db.flush()
    return stats
