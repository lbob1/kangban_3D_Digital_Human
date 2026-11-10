"""play_media 工具（Day7+，2026-11-10 用户反馈「点播完整解决方案」）。

背景：规则快路径只认固定话术（响应快但句式封闭），规则不命中的自由表达
（「播放富士山茶」「来点陈奕迅的歌」「放个戏曲听听」）落主控 Agent——此前
LLM 只能生成文字回复、无法真正执行播放。本工具让 LLM 理解后经确定性代码执行。

链路与规则快路径完全汇合（同一 media_service）：曲库内模糊匹配（容忍 ASR
错字，如「富士山茶」→「富士山下」）→ media_history 落库（voice_rule）→
WS 下行 media_play（前端播放器弹窗）。

安全口径：
- elder_id 由服务端 runtime_ctx 注入（AGENTS 禁区 8），schema 不含 elder_id；
- 只在本库 active 媒体内匹配，不调用外部媒体资源（AGENTS 禁区 4）；
- 匹配不到时如实返回 not_found + 曲库名单，禁止「编造播放成功」；
- 播放本体属安全动作（无敏感副作用），不做 HITL 拦截。
"""

from __future__ import annotations

import difflib
import re

from sqlalchemy import select

from app.tools.registry import ToolSpec

# ASR 常带句末标点/空白；匹配前归一化（去空白与常见标点，转小写）
_NOISE = re.compile(r"[\s！!。.？?，,～~、的了首段一上下个]")

# 匹配阈值：低于视为找不到（宁可不播也不张冠李戴）
_MATCH_THRESHOLD = 0.45


def _normalize(text: str) -> str:
    return _NOISE.sub("", (text or "").lower())


def _score(query_norm: str, target: str) -> float:
    """归一化相似度：对标题与标题分段（如「陈奕迅 - 富士山下」的段）取最大。"""
    target_norm = _normalize(target)
    if not target_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query_norm, target_norm).ratio()
    if query_norm and query_norm in target_norm:
        ratio = max(ratio, 0.9)  # 包含关系（query 是标题子串）视为高度匹配
    best = ratio
    for segment in re.split(r"[-—·()（）\[\]]", target):
        seg_norm = _normalize(segment)
        if not seg_norm:
            continue
        best = max(
            best,
            difflib.SequenceMatcher(None, query_norm, seg_norm).ratio(),
            0.9 if (query_norm and query_norm in seg_norm) else 0.0,
        )
    return best


def best_match(query: str, candidates: list) -> object | None:
    """曲库内择优：对 title/artist 打分取最高，低于阈值返回 None。"""
    query_norm = _normalize(query)
    if not query_norm:
        return None
    best, best_media = 0.0, None
    for media in candidates:
        score = max(
            _score(query_norm, media.title or ""),
            _score(query_norm, media.artist or ""),
        )
        if score > best:
            best, best_media = score, media
    return best_media if best >= _MATCH_THRESHOLD else None


def build_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="play_media",
        description=(
            "点播曲库中的音乐或戏曲并播放（支持按歌名/歌手/剧种模糊匹配）。"
            "用户想听任何具体曲目、歌手的歌、或戏曲时调用；纯泛指（如只说"
            "「放段音乐」不带曲名）优先由规则层处理。匹配不到会返回曲库清单。"
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "曲目名/歌手名/剧种关键词，如：富士山下、陈奕迅、越剧",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["music", "opera"],
                    "description": "可选：指定音乐或戏曲；无法判断时不传",
                },
            },
            "required": ["query"],
        },
        handler=play_media_handler,
    )


async def play_media_handler(args: dict, runtime_ctx: dict) -> dict:
    """主控 Agent 工具执行入口：签名与其他工具一致（registry 约定）。"""
    from app.db.session import get_session_factory
    from app.models.enums import MediaStatus
    from app.models.media import MediaLibrary
    from app.router.cascade import RuntimeBridge
    from app.services import media as media_service

    query = str(args.get("query") or "").strip()
    media_type = args.get("media_type")
    if not query:
        return {"status": "error", "reply_hint": "请告诉我您想听什么曲目。"}

    ctx = RuntimeBridge.get() or {}
    elder_id = (runtime_ctx or {}).get("elder_id") or ctx.get("elder_id")
    if not elder_id:
        return {
            "status": "need_voiceprint",
            "reply_hint": "好的。请先完成声纹确认，我再为您点播。",
        }
    factory = (
        (runtime_ctx or {}).get("db_factory")
        or ctx.get("db_factory")
        or get_session_factory()  # 兜底：无 Bridge 上下文的入口（如 API 直调 agent）也可执行
    )

    async with factory() as db:
        stmt = select(MediaLibrary).where(MediaLibrary.status == MediaStatus.ACTIVE)
        if media_type in ("music", "opera"):
            stmt = stmt.where(MediaLibrary.type == media_type)
        candidates = (await db.execute(stmt)).scalars().all()
        media = best_match(query, list(candidates))
        if media is None:
            return {
                "status": "not_found",
                "reply_hint": f"曲库里没有找到「{query}」。",
                "library_titles": [m.title for m in candidates][:20],
            }
        await media_service.record_voice_play(db, elder_id=elder_id, media=media)
        await db.commit()

    send_event = ctx.get("send_event")
    if send_event is not None:
        try:
            await send_event(
                {
                    "type": "media_play",
                    "media_id": str(media.media_id),
                    "title": media.title,
                    "media_uri": media.media_uri,
                    "artist": media.artist,
                    "duration_seconds": media.duration_seconds,
                }
            )
        except Exception:  # noqa: BLE001 —— 下行失败不阻断话术
            pass
    return {
        "status": "playing",
        "title": media.title,
        "artist": media.artist,
        "reply_hint": (
            f"正在为您播放《{media.title}》。播放器已打开，"
            "您可以在里面暂停或切换上一首下一首。"
        ),
    }
