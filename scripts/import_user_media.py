"""用户自备媒体导入（Day7+）：扫描 music/ 与 yuejv/ 目录，登记进 media_library。

目录约定（仓库根）：
- music/  → type=music（「我想听音乐」命中）
- yuejv/  → type=opera（「我想听越剧」命中）

安全口径：素材由用户自行提供并放入目录，脚本只做登记不做下载；
copyright_status 置 licensed 并在 genre 标注「用户自备」，仅限本地教学演示，
不可对外分发或商用（红线：不调用未授权媒体资源——授权责任在素材提供方）。

幂等：按 (title, media_uri) 查重，重复执行零副作用；已存在同名文件不覆盖。
支持格式：mp3 / wav / m4a / flac。

用法：uv run python scripts/import_user_media.py
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import MediaLibrary
from app.models.enums import MediaCopyrightStatus, MediaStatus

try:  # 模块方式与直接脚本运行双兼容
    from scripts.import_media import STATIC_MEDIA_DIR
except ImportError:
    from import_media import STATIC_MEDIA_DIR  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac"}

# (目录名, media type, 默认流派标注)
SOURCES = [
    ("music", "music", "用户自备·音乐"),
    ("yuejv", "opera", "用户自备·戏曲"),
]


def collect_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


async def main() -> None:
    STATIC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    imported = 0
    async with factory() as session:
        for folder_name, media_type, genre in SOURCES:
            for src in collect_files(REPO_ROOT / folder_name):
                dest = STATIC_MEDIA_DIR / f"user_{folder_name}_{src.name}"
                if not dest.exists():
                    shutil.copy2(src, dest)
                media_uri = f"/static/media/{dest.name}"
                exists = (
                    await session.execute(
                        select(MediaLibrary).where(
                            MediaLibrary.title == src.stem,
                            MediaLibrary.media_uri == media_uri,
                        )
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    print(f"[import_user_media] 已存在，跳过：{src.name}")
                    continue
                session.add(
                    MediaLibrary(
                        type=media_type,
                        title=src.stem,
                        genre=genre,
                        media_uri=media_uri,
                        copyright_status=MediaCopyrightStatus.LICENSED,
                        status=MediaStatus.ACTIVE,
                    )
                )
                imported += 1
                print(f"[import_user_media] 登记：{src.name} → {media_type} {media_uri}")
        await session.commit()
    await engine.dispose()
    print(f"[import_user_media] 新增 {imported} 条；提示：重启浏览器页面后即可点播")


if __name__ == "__main__":
    asyncio.run(main())
