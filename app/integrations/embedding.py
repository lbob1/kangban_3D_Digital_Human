"""embedding 客户端（Day7，主文档 §1.2-2 / §6.2）：百炼 compatible-mode 批量向量。

与 app/integrations/llm.py 同一体系（base_url/key 复用 LLM_* 同值占位，
Day2 决策：embedding 四字段独立于 LLM_* 但填同值，零新增密钥管理）。

失败口径与 llm.py 一致：统一 EmbeddingUnavailable，由上层可见降级——
记忆链路任何失败不阻塞对话（萃取跳过本块 / 召回走无记忆直答），严禁静默 mock。
只记元数据（条数/耗时/维度/usage），文本与向量不入日志（AGENTS 原始内容红线）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 百炼 compatible-mode 单请求批量上限（官方 embeddings 接口批量 ≤ 10 条）
BATCH_SIZE = 10


class EmbeddingUnavailable(Exception):
    """embedding 未配置或调用失败；调用方必须降级，不得阻塞对话。"""


def is_configured() -> bool:
    s = get_settings()
    return bool(s.embedding_api_key and s.embedding_base_url and s.embedding_model)


async def embed_texts(
    texts: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[list[float]]:
    """批量向量化：返回与入参等长等序的向量列表（维度 == settings.embedding_dimensions）。

    - 超时/5xx/429 临时错：退避重试 1 次（失败四分类口径：临时错→退避重试）；
    - 其余（未配置/参数错/4xx）：不重试，直接 EmbeddingUnavailable；
    - 返回维度与配置不符：视为模型-维度绑定错（主文档 §6.2 ⚠️），立即失败不上库。
    """
    s = get_settings()
    if not is_configured():
        raise EmbeddingUnavailable("embedding 未配置（embedding_api_key/base_url/model 缺失）")
    if not texts:
        return []

    url = s.embedding_base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {s.embedding_api_key}"}
    expected_dim = s.embedding_dimensions

    out: list[list[float]] = []
    started = time.monotonic()
    # 分批串行：记忆场景单块 ≤3 条、查询 1 条，正常只有一批；大批量不追求并行
    for offset in range(0, len(texts), BATCH_SIZE):
        batch = texts[offset : offset + BATCH_SIZE]
        payload: dict[str, Any] = {
            "model": s.embedding_model,
            "input": batch,
            "dimensions": expected_dim,
            "encoding_format": "float",
        }
        vectors = await _post_once(url, payload, headers, expected_dim, transport)
        out.extend(vectors)
    logger.info(
        "embedding batch ok",
        extra={
            "event": "embedding_batch_ok",
            "count": len(out),
            "dims": expected_dim,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return out


async def embed_text(
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[float]:
    """单条向量化便捷入口（召回查询用）。"""
    return (await embed_texts([text], transport=transport))[0]


async def _post_once(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    expected_dim: int,
    transport: httpx.AsyncBaseTransport | None,
) -> list[list[float]]:
    """单批请求，含 1 次退避重试；transport 仅供测试注入 MockTransport。"""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = sorted(data.get("data", []), key=lambda it: it.get("index", 0))
                if len(items) != len(payload["input"]):
                    raise EmbeddingUnavailable("embedding 返回条数与请求不符")
                vectors = [item["embedding"] for item in items]
                for v in vectors:
                    if len(v) != expected_dim:
                        raise EmbeddingUnavailable(
                            f"embedding 维度不符：期望 {expected_dim}，实测 {len(v)}"
                        )
                return vectors
            # 临时错（429/5xx）重试一次；其余 4xx 是参数/鉴权错，重试无意义
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                last_error = EmbeddingUnavailable(f"embedding HTTP {resp.status_code}")
                await asyncio.sleep(0.5)
                continue
            raise EmbeddingUnavailable(f"embedding HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            if attempt == 0:
                last_error = EmbeddingUnavailable(f"embedding 调用失败：{type(exc).__name__}")
                await asyncio.sleep(0.5)
                continue
            raise EmbeddingUnavailable(f"embedding 调用失败：{type(exc).__name__}") from exc
    raise last_error or EmbeddingUnavailable("embedding 调用失败")
