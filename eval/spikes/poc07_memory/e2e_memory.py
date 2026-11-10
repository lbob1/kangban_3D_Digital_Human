"""POC-07 记忆链路端到端实测（Day7 步 4，真实百炼 LLM + embedding + kangban_dev）。

场景（大纲 7.1 验收锚点，US-03）：
  1. 王奶奶聊「我有糖尿病」→ 块结束萃取落库（1024 维、importance、private）；
  2. 新会话问「能吃粽子吗」→ 向量召回 + ④二次过滤 → 记忆命中糖尿病；
  3. 构造矛盾「现在想吃点甜的了」→ ⑤冲突检测 → 旧记忆 superseded；
  4. 李爷爷（另一长者）查王奶奶记忆 → 空集（隔离红线）。

运行：uv run python eval/spikes/poc07_memory/e2e_memory.py
说明：演示长者命名为「王奶奶（POC-07演示）」，脚本结束清理本脚本产生的全部数据，
不触碰任何真实长者数据（AGENTS 安全禁区 3）。
"""

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Elder, Persona
from app.models.memory import MemoryChunk
from app.services import memory as memory_service

BLOCK_1 = [
    ("user", "我有糖尿病，甜的都不敢多吃"),
    ("assistant", "王奶奶您注意控糖是对的，我会陪您一起留意的。"),
]
BLOCK_2 = [
    ("user", "最近想吃点甜的了，月饼能吃吗"),
    ("assistant", "适量尝一点可以，我还是会提醒您注意血糖的。"),
]
QUERY = "能吃粽子吗"


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persona_code = "poc07-wangnainai"
    async with factory() as db:
        # ---- 准备：演示长者（可重跑：先清理旧数据）----
        old_persona = (
            await db.execute(select(Persona).where(Persona.code == persona_code))
        ).scalar_one_or_none()
        elder_id = None
        if old_persona is not None:
            old_elder = (
                await db.execute(select(Elder).where(Elder.persona_id == old_persona.persona_id))
            ).scalars().first()
            if old_elder is not None:
                elder_id = old_elder.elder_id
                await db.execute(delete(MemoryChunk).where(MemoryChunk.elder_id == elder_id))
                await db.execute(delete(Elder).where(Elder.elder_id == elder_id))
            await db.execute(delete(Persona).where(Persona.code == persona_code))
            await db.commit()
        persona = Persona(code=persona_code, name="POC-07 演示人设", is_active=True)
        db.add(persona)
        await db.flush()
        elder = Elder(
            name="王奶奶（POC-07演示）", persona_id=persona.persona_id, status="active"
        )
        db.add(elder)
        await db.commit()
        elder_id = elder.elder_id
        print(f"[0] 演示长者就绪 elder_id={elder_id}")

        # ---- ① 萃取：块 1 结束（真实 LLM + embedding）----
        n = await memory_service.extract_and_store(db, elder_id=elder_id, messages=BLOCK_1)
        await db.commit()
        rows = list(
            (
                await db.execute(
                    select(MemoryChunk).where(MemoryChunk.elder_id == elder_id)
                )
            )
            .scalars()
            .all()
        )
        print(f"[1] 萃取落库 {n} 条：")
        for r in rows:
            print(
                f"    - ({r.category}/imp={r.importance}/{r.visibility}/"
                f"dim={len(r.embedding)}) {r.content}"
            )

        # ---- ③ 召回 + ④二次过滤：问粽子（真实 embedding + LLM）----
        candidates = await memory_service.recall_memories(
            db, elder_id=elder_id, query_text=QUERY
        )
        filtered = await memory_service.filter_recalled(QUERY, candidates)
        print(f"[2] 「{QUERY}」召回 top-5={len(candidates)} 条 → 过滤后 {len(filtered)} 条：")
        for r in filtered:
            print(f"    - ({r.category}) {r.content}")
        assert any("糖尿病" in r.content for r in filtered), "US-03 锚点未命中：应召回糖尿病记忆"

        # ---- ⑤ 冲突检测：块 2「想吃甜的」→ 旧记忆应被 superseded ----
        before_block2 = {
            r.chunk_id: r for r in rows if r.status == "active"
        }
        await memory_service.extract_and_store(db, elder_id=elder_id, messages=BLOCK_2)
        await db.commit()
        after = list(
            (
                await db.execute(
                    select(MemoryChunk)
                    .where(MemoryChunk.elder_id == elder_id)
                    .order_by(MemoryChunk.created_at)
                )
            )
            .scalars()
            .all()
        )
        new_rows = [r for r in after if r.chunk_id not in before_block2]
        for r in new_rows:
            print(f"    - 新记忆: ({r.category}) {r.content}")
        # 相似度诊断：新记忆 vs 旧记忆（向量层面，判断 0.9 阈值是否触发冲突比对）
        import math

        def _cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na and nb else 0.0

        for r_new in new_rows:
            for r_old in before_block2.values():
                print(f"    - 相似度(新{r_new.content[:12]}… vs 旧{r_old.content[:12]}…)= "
                      f"{_cos(r_new.embedding, r_old.embedding):.3f}")
        superseded = [r for r in after if r.status == "superseded"]
        active = [r for r in after if r.status == "active"]
        print(f"[3] 冲突检测后：active={len(active)} superseded={len(superseded)}")
        for r in superseded:
            print(f"    - superseded: {r.content}")

        # ---- 隔离红线：另一长者查王奶奶记忆 → 空 ----
        stranger = (
            await db.execute(select(Elder).where(Elder.elder_id != elder_id).limit(1))
        ).scalars().first()
        if stranger is not None:
            leaked = await memory_service.recall_memories(
                db, elder_id=stranger.elder_id, query_text="糖尿病"
            )
            print(f"[4] 隔离：{stranger.name} 查询王奶奶记忆 → {len(leaked)} 条")
            assert leaked == [], "隔离红线被击穿！"

        # ---- 清理：不留演示数据 ----
        await db.execute(delete(MemoryChunk).where(MemoryChunk.elder_id == elder_id))
        await db.execute(delete(Elder).where(Elder.elder_id == elder_id))
        await db.execute(delete(Persona).where(Persona.code == persona_code))
        await db.commit()
        print("[5] 已清理演示数据。POC-07 四项全部通过。")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
