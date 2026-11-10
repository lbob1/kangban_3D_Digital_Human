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
from collections.abc import Sequence
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
) -> list[MemoryChunk]:
    """② 存储：content 向量化（1024 维）+ 写 memory_chunks。

    elder_id 为服务端 Runtime Context 注入值（本函数不接受任何「请求方主张」）；
    embedding 失败抛 EmbeddingUnavailable，由调用方降级（整批不入库，不留脏数据）。
    """
    if not items:
        return []
    contents = [item["content"] for item in items]
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
        rows = await store_memories(db, elder_id=elder_id, items=items, transport=embed_transport)
    except EmbeddingUnavailable as exc:
        logger.warning(
            "memory store skipped (embedding unavailable)",
            extra={"event": "memory_store_unavailable", "error": str(exc)},
        )
        return 0
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
