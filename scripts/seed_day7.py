"""Day7 步8：演示数据种子（幂等，可重复执行）。

内容：
1) 媒体样例（复用 scripts/import_media.import_samples，含本地合成音频）；
2) 演示长者 active 用药计划（3 slots：早/午/晚，供 APScheduler 扫描）；
3) 演示长者偏好记忆「喜欢听越剧」（点播偏好优先用，embedding 置零向量
   ——仅作关键词匹配源，不参与召回排序）。

长者选择：--elder-id 指定；缺省取库内第一台绑定房间的 active 设备对应长者
（演示库单人间场景）。全部按内容查重，重复执行零副作用。

用法：uv run python scripts/seed_day7.py [--elder-id <uuid>]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Device, Elder, MedicationPlan, MemoryChunk
from app.models.enums import (
    MedicationPlanStatus,
    MemoryCategory,
    MemoryStatus,
)

try:  # 模块方式（测试/uv run -m）与直接脚本运行双兼容
    from scripts.import_media import STATIC_MEDIA_DIR, import_samples
except ImportError:  # python scripts/seed_day7.py 时 sys.path[0] 即 scripts/
    from import_media import STATIC_MEDIA_DIR, import_samples  # type: ignore[no-redef]

PREF_CONTENT = "喜欢听越剧"
PLAN_SCHEDULE = {
    "slots": [
        {"time": "08:00", "label": "早"},
        {"time": "12:30", "label": "午"},
        {"time": "18:00", "label": "晚"},
    ]
}


async def resolve_elder(session, elder_id: UUID | None) -> Elder:
    if elder_id is not None:
        elder = await session.get(Elder, elder_id)
        if elder is None:
            raise SystemExit(f"[seed_day7] 长者不存在：{elder_id}")
        return elder
    device = (
        await session.execute(
            select(Device)
            .where(Device.room_id.is_not(None))
            .order_by(Device.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if device is None:
        raise SystemExit("[seed_day7] 库内无绑定房间的设备，请先完成 Day2 基础数据")
    elder = (
        await session.execute(
            select(Elder).where(Elder.room_id == device.room_id).order_by(Elder.created_at).limit(1)
        )
    ).scalar_one_or_none()
    if elder is None:
        raise SystemExit("[seed_day7] 设备绑定房间无长者，请先完成 Day2 基础数据")
    return elder


async def ensure_demo_data(session, elder: Elder) -> dict:
    result: dict = {"media": await import_samples(session)}

    # 偏好记忆（幂等：同 elder 同 content 查重；零向量仅作关键词源）
    existing_pref = (
        await session.execute(
            select(MemoryChunk).where(
                MemoryChunk.elder_id == elder.elder_id,
                MemoryChunk.content == PREF_CONTENT,
                MemoryChunk.category == MemoryCategory.PREFERENCE,
            )
        )
    ).scalar_one_or_none()
    if existing_pref is None:
        session.add(
            MemoryChunk(
                elder_id=elder.elder_id,
                content=PREF_CONTENT,
                embedding=[0.0] * 1024,
                category=MemoryCategory.PREFERENCE,
                importance=3,
                status=MemoryStatus.ACTIVE,
            )
        )
        result["preference"] = "inserted"
    else:
        result["preference"] = "exists"

    # active 用药计划（幂等：已有 active 计划则跳过）
    existing_plan = (
        await session.execute(
            select(MedicationPlan).where(
                MedicationPlan.elder_id == elder.elder_id,
                MedicationPlan.status == MedicationPlanStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if existing_plan is None:
        session.add(
            MedicationPlan(
                elder_id=elder.elder_id,
                drug_name="硝苯地平缓释片",
                dose="20mg",
                schedule=PLAN_SCHEDULE,
                instructions="饭后服用，注意监测血压",
                start_date=datetime.now(UTC).date(),
                status=MedicationPlanStatus.ACTIVE,
            )
        )
        result["plan"] = "inserted"
    else:
        result["plan"] = "exists"

    await session.commit()
    result["elder"] = f"{elder.name}({elder.elder_id})"
    return result


async def main(elder_id: str | None = None, db_url: str | None = None) -> None:
    STATIC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    url = db_url or get_settings().database_url
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        elder = await resolve_elder(session, UUID(elder_id) if elder_id else None)
        result = await ensure_demo_data(session, elder)
    await engine.dispose()
    print(f"[seed_day7] {result}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--elder-id", default=None, help="指定演示长者（缺省取首个绑定房间的在住长者）"
    )
    parser.add_argument("--db", default=None, help="覆盖数据库 URL（默认读 Settings）")
    args = parser.parse_args()
    asyncio.run(main(args.elder_id, args.db))
