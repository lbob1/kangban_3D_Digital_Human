"""Day7 步8：媒体样例导入（幂等）+ 本地生成演示音频。

版权红线（AGENTS 禁区 4）：不下载、不引用任何未授权媒体资源——演示音频为
本脚本用标准库 wave 现场合成的短正弦音，曲目元数据为演示数据，版权统一标注
TEACHING/PUBLIC_DOMAIN。入库后由 /media/library 与点播链路的授权过滤兜底。

用法：uv run python scripts/import_media.py [--db postgresql+psycopg://...]
（未传 --db 时读 Settings.database_url；重复执行按 title 幂等 upsert）
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import wave
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import MediaLibrary

STATIC_MEDIA_DIR = Path("app/static/media")

# 演示曲库（音频由 _generate_tone_wav 合成，非真实唱片——版权零风险）
SAMPLES: list[dict] = [
    {
        "type": "opera",
        "title": "梁山伯与祝英台·选段（教学样例）",
        "genre": "越剧",
        "era": "经典",
        "artist": "教学样例音源",
        "copyright_status": "teaching",
        "media_uri": "/static/media/opera_liangzhu.wav",
        "duration_seconds": 3,
        "tone_hz": 440.0,
    },
    {
        "type": "opera",
        "title": "红楼梦·天上掉下个林妹妹（教学样例）",
        "genre": "越剧",
        "era": "经典",
        "artist": "教学样例音源",
        "copyright_status": "teaching",
        "media_uri": "/static/media/opera_honglou.wav",
        "duration_seconds": 3,
        "tone_hz": 523.25,
    },
    {
        "type": "opera",
        "title": "穆桂英挂帅·选段（教学样例）",
        "genre": "京剧",
        "era": "经典",
        "artist": "教学样例音源",
        "copyright_status": "teaching",
        "media_uri": "/static/media/opera_muguiying.wav",
        "duration_seconds": 3,
        "tone_hz": 587.33,
    },
    {
        "type": "movie",
        "title": "我的祖国（公版旋律样例）",
        "genre": "电影插曲",
        "era": "1956",
        "artist": "公版旋律",
        "copyright_status": "public_domain",
        "media_uri": "/static/media/movie_woguoxiang.wav",
        "duration_seconds": 3,
        "tone_hz": 659.26,
    },
]


def generate_tone_wav(path: Path, tone_hz: float, seconds: int = 3, rate: int = 8000) -> None:
    """生成带淡入淡出的正弦音（16-bit 单声道）——演示用可播放音频。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = int(rate * seconds)
    frames = bytearray()
    for i in range(total):
        fade = min(1.0, i / (rate * 0.2), (total - i) / (rate * 0.2))
        value = int(32767 * 0.5 * fade * math.sin(2 * math.pi * tone_hz * i / rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(frames))


def generate_all_samples() -> None:
    for sample in SAMPLES:
        path = STATIC_MEDIA_DIR / Path(sample["media_uri"]).name
        if not path.exists():  # 已存在则跳过（幂等，不重写）
            generate_tone_wav(path, sample["tone_hz"], sample["duration_seconds"])


async def import_samples(session) -> dict[str, int]:
    """按 title 幂等 upsert（存在→更新字段，不存在→插入）。返回 {"inserted": n, "updated": n}。"""
    generate_all_samples()
    inserted = updated = 0
    for sample in SAMPLES:
        existing = (
            await session.execute(
                select(MediaLibrary).where(MediaLibrary.title == sample["title"])
            )
        ).scalar_one_or_none()
        fields = {
            k: v for k, v in sample.items() if k not in ("tone_hz",)
        }
        fields.setdefault("status", "active")  # 样例默认上架（模型 ORM 默认 hidden）
        if existing is None:
            session.add(MediaLibrary(**fields))
            inserted += 1
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
            updated += 1
    await session.commit()
    return {"inserted": inserted, "updated": updated}


async def main(db_url: str | None = None) -> None:
    url = db_url or get_settings().database_url
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await import_samples(session)
    await engine.dispose()
    print(f"[import_media] inserted={result['inserted']} updated={result['updated']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="覆盖数据库 URL（默认读 Settings）")
    args = parser.parse_args()
    asyncio.run(main(args.db))
