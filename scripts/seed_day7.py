"""Day7 步8：演示数据种子（幂等，可重复执行）。

内容（默认覆盖全部在住长者；--elder-id 可指定单位）：
1) 媒体样例（复用 scripts/import_media.import_samples，含本地合成音频）；
2) 每位在住长者 3 条用药计划（常见慢病演示药名轮换，供 APScheduler 扫描
   与用药 CRUD 页面演示）；
3) 每位在住长者偏好记忆（越剧/京剧/黄梅戏/经典老歌轮换，embedding 置零向量
   ——仅作关键词匹配源，不参与召回排序）；
4) 每位在住长者近期播放留痕（media_history，库内无该长者记录时补 3 条）。

长者选择：--elder-id 指定单人；缺省遍历全部 status=active 长者。
全部按内容查重，重复执行零副作用。

用法：uv run python scripts/seed_day7.py [--elder-id <uuid>]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Elder, MediaHistory, MediaLibrary, MedicationPlan, MemoryChunk
from app.models.enums import (
    MediaStatus,
    MedicationPlanStatus,
    MemoryCategory,
    MemoryStatus,
)

try:  # 模块方式（测试/uv run -m）与直接脚本运行双兼容
    from scripts.import_media import STATIC_MEDIA_DIR, import_samples
except ImportError:  # python scripts/seed_day7.py 时 sys.path[0] 即 scripts/
    from import_media import STATIC_MEDIA_DIR, import_samples  # type: ignore[no-redef]

# 每位长者 3 条用药计划（常见慢病演示口径，按长者序号轮换；
# schedule 单 slot 与服药时间对应，供 scheduler 逐计划扫描）
MED_SETS: list[list[tuple[str, str, str, str, str]]] = [
    [  # 高血压为主
        ("氨氯地平片", "5mg", "每天早餐后", "早餐后服用，注意监测血压", "08:00"),
        ("缬沙坦胶囊", "80mg", "每天晚餐后", "晚餐后服用，避免与保钾利尿剂联用", "18:00"),
        ("阿托伐他汀钙片", "20mg", "每晚睡前", "睡前服用，定期复查血脂肝功", "21:00"),
    ],
    [  # 糖尿病为主
        ("二甲双胍缓释片", "0.5g", "每天午餐随餐", "随餐服用，减少胃肠道反应", "12:30"),
        ("格列美脲片", "2mg", "每天早餐后", "早餐后服用，注意低血糖先兆", "08:00"),
        ("硝苯地平缓释片", "20mg", "每天早餐后", "饭后服用，注意监测血压", "08:00"),
    ],
    [  # 心脑血管 + 助眠
        ("美托洛尔缓释片", "47.5mg", "每天早餐后", "早餐后服用，不可突然停药", "08:00"),
        ("阿司匹林肠溶片", "100mg", "每天早餐前", "早餐前空腹服用", "07:30"),
        ("右佐匹克隆片", "3mg", "每晚睡前", "睡前服用，服药后卧床休息（HITL 关注）", "21:30"),
    ],
    [  # 慢病 + 补钙
        ("氯沙坦钾片", "50mg", "每天早餐后", "早餐后服用，监测血压", "08:00"),
        ("碳酸钙D3片", "600mg", "每天午餐随餐", "随餐服用，多饮水", "12:30"),
        ("辛伐他汀片", "20mg", "每晚睡前", "睡前服用，定期复查血脂", "21:00"),
    ],
]

GENRES = ["越剧", "京剧", "黄梅戏", "经典老歌"]


async def resolve_elder(session, elder_id: UUID | None) -> Elder | None:
    """--elder-id 指定单人；否则返回 None（主流程遍历全部在住长者）。"""
    if elder_id is not None:
        elder = await session.get(Elder, elder_id)
        if elder is None:
            raise SystemExit(f"[seed_day7] 长者不存在：{elder_id}")
        return elder
    return None


async def ensure_for_elder(session, elder: Elder, index: int, media_rows: list) -> dict:
    """给单个长者补齐：偏好记忆 + 用药计划 + 播放留痕（全部幂等）。"""
    result: dict = {"elder": f"{elder.name}({elder.elder_id})"}

    # ---- 偏好记忆（按长者序号轮换剧种；同 elder 同 content 查重）----
    genre = GENRES[index % len(GENRES)]
    pref_content = f"喜欢听{genre}"
    existing_pref = (
        await session.execute(
            select(MemoryChunk).where(
                MemoryChunk.elder_id == elder.elder_id,
                MemoryChunk.content == pref_content,
                MemoryChunk.category == MemoryCategory.PREFERENCE,
            )
        )
    ).scalar_one_or_none()
    if existing_pref is None:
        session.add(
            MemoryChunk(
                elder_id=elder.elder_id,
                content=pref_content,
                embedding=[0.0] * 1024,
                category=MemoryCategory.PREFERENCE,
                importance=3,
                status=MemoryStatus.ACTIVE,
            )
        )
        result["preference"] = pref_content
    else:
        result["preference"] = "exists"

    # ---- 用药计划（按长者序号轮换药组；按 (elder, drug_name) 查重）----
    existing_drugs = set(
        (
            await session.execute(
                select(MedicationPlan.drug_name).where(
                    MedicationPlan.elder_id == elder.elder_id,
                    MedicationPlan.status.in_(
                        [MedicationPlanStatus.PLANNED, MedicationPlanStatus.ACTIVE]
                    ),
                )
            )
        ).scalars().all()
    )
    inserted = 0
    med_set = MED_SETS[index % len(MED_SETS)]
    for drug_name, dose, freq, instructions, slot_time in med_set:
        if drug_name in existing_drugs:
            continue
        session.add(
            MedicationPlan(
                elder_id=elder.elder_id,
                drug_name=drug_name,
                dose=dose,
                schedule={"slots": [{"time": slot_time, "label": freq}]},
                instructions=instructions,
                start_date=datetime.now(UTC).date(),
                status=MedicationPlanStatus.ACTIVE,
            )
        )
        inserted += 1
    result["plans"] = f"+{inserted}"

    # ---- 播放留痕（该长者无任何记录时补 3 条近期播放）----
    hist_count = (
        await session.execute(
            select(func.count()).select_from(MediaHistory).where(
                MediaHistory.elder_id == elder.elder_id
            )
        )
    ).scalar_one()
    if hist_count == 0 and media_rows:
        now = datetime.now(UTC)
        for k, media in enumerate(media_rows[:3]):
            session.add(
                MediaHistory(
                    elder_id=elder.elder_id,
                    media_id=media.media_id,
                    played_at=now - timedelta(days=k + 1, hours=k),
                    duration_seconds=90 + 30 * k,
                    source="voice_rule",
                )
            )
        result["history"] = f"+{min(3, len(media_rows))}"
    else:
        result["history"] = "exists"
    return result


async def main(elder_id: str | None = None, db_url: str | None = None) -> None:
    STATIC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    url = db_url or get_settings().database_url
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await import_samples(session)
        active_media = (
            await session.execute(
                select(MediaLibrary)
                .where(MediaLibrary.status == MediaStatus.ACTIVE)
                .order_by(MediaLibrary.created_at)
            )
        ).scalars().all()

        target = await resolve_elder(session, UUID(elder_id) if elder_id else None)
        if target is not None:
            targets = [(target, 0)]
        else:
            rows = (
                await session.execute(
                    select(Elder).where(Elder.status == "active").order_by(Elder.created_at)
                )
            ).scalars().all()
            targets = [(elder, i) for i, elder in enumerate(rows)]

        results = []
        for elder, idx in targets:
            results.append(await ensure_for_elder(session, elder, idx, active_media))
            await session.commit()
    await engine.dispose()
    for r in results:
        print(f"[seed_day7] {r}")
    print(f"[seed_day7] 共处理 {len(results)} 位长者；曲库样例 {len(active_media)} 条 active")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--elder-id", default=None, help="指定演示长者（缺省遍历全部在住长者）"
    )
    parser.add_argument("--db", default=None, help="覆盖数据库 URL（默认读 Settings）")
    args = parser.parse_args()
    asyncio.run(main(args.elder_id, args.db))
