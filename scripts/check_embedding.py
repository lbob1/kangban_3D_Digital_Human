"""embedding 自检脚本（Day7 步骤 1，主文档 §6.3 前置条件 2）。

一条真实文本调用百炼 compatible-mode embeddings 接口：
- 返回 1024 维 → 自检通过，0012 迁移按此维度固定；
- 装不上 / 超时 / 维度不符 → 退出码 1 并如实打印实测数据，禁止静默 mock。

用法：uv run python scripts/check_embedding.py [文本]
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.core.config import get_settings

EXPECTED_DIM = 1024


async def main() -> int:
    settings = get_settings()
    configured = bool(
        settings.embedding_api_key and settings.embedding_base_url and settings.embedding_model
    )
    if not configured:
        print("[自检失败] EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL 未配置")
        return 1

    url = settings.embedding_base_url.rstrip("/") + "/embeddings"
    text = sys.argv[1] if len(sys.argv) > 1 else "王奶奶有糖尿病，想吃粽子。"
    headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}

    # 先带 dimensions 参数（百炼 compatible-mode 支持）；若模型不认该参数则原样重试
    for payload in (
        {
            "model": settings.embedding_model,
            "input": [text],
            "dimensions": EXPECTED_DIM,
            "encoding_format": "float",
        },
        {"model": settings.embedding_model, "input": [text], "encoding_format": "float"},
    ):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            print(f"[自检失败] 请求异常：{exc.__class__.__name__}: {exc}")
            return 1
        if resp.status_code != 200:
            print(f"[自检失败] HTTP {resp.status_code}: {resp.text[:300]}")
            continue
        data = resp.json()
        vector = data["data"][0]["embedding"]
        dims = len(vector)
        model = data.get("model", payload["model"])
        usage = data.get("usage", {})
        if dims != EXPECTED_DIM:
            print(f"[自检失败] 维度不符：期望 {EXPECTED_DIM}，实测 {dims}（model={model}）")
            return 1
        preview = "[" + ", ".join(f"{v:.6f}" for v in vector[:3]) + ", ...]"
        print(f"[自检通过] model={model} dims={dims} usage={usage}")
        print(f"向量前 3 维：{preview}")
        return 0

    print("[自检失败] 两次尝试均未成功，见上方实测输出")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
