"""play_media Tool（Day7+ 点播完整解决方案）单元测试。

覆盖：曲库模糊匹配容忍 ASR 错字（富士山茶→富士山下）；schema 安全口径
（不含 elder_id、query 必填）；handler 落库 + WS 事件 + 未验证拒绝 +
匹配不到如实返回曲库名单（禁编造播放成功）。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import MediaHistory, MediaLibrary
from app.models.enums import MediaCopyrightStatus, MediaStatus
from app.router.cascade import RuntimeBridge
from app.tools import play_media, registry


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    monkeypatch.setitem(registry._REGISTRY, "play_media", play_media.build_tool_spec())


class _FakeFactory:
    """async with factory() as db 形态（factory 可调用后返回自身上下文管理器）。"""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


def _media(title: str, artist: str | None = None, mtype: str = "music"):
    return SimpleNamespace(title=title, artist=artist, type=mtype)


# ---- 模糊匹配（纯函数）----


def test_best_match_tolerates_asr_typos():
    """ASR 错字（富士山茶）与歌手名都能匹配到正确曲目。"""
    lib = [_media("陈奕迅 - 富士山下"), _media("凤凰传奇 - 奢香夫人", mtype="opera")]
    assert play_media.best_match("富士山茶", lib).title == "陈奕迅 - 富士山下"
    assert play_media.best_match("陈奕迅", lib).title == "陈奕迅 - 富士山下"
    assert play_media.best_match("富士山下", lib).title == "陈奕迅 - 富士山下"
    assert play_media.best_match("奢香夫人", lib).title == "凤凰传奇 - 奢香夫人"


def test_best_match_none_when_off_library():
    """曲库外内容与空串：返回 None（handler 据此如实告知）。"""
    lib = [_media("陈奕迅 - 富士山下")]
    assert play_media.best_match("量子力学", lib) is None
    assert play_media.best_match("", lib) is None


def test_schema_no_elder_id_and_query_required():
    """schema 安全口径：elder_id 只能服务端注入，query 必填，handler 已挂。"""
    spec = play_media.build_tool_spec()
    assert spec.name == "play_media"
    assert "elder_id" not in spec.schema["properties"]
    assert spec.schema["required"] == ["query"]
    assert spec.handler is not None


# ---- handler（db_session 事务回滚隔离）----


async def _seed_active_music(db_session) -> MediaLibrary:
    media = MediaLibrary(
        type="music",
        title="陈奕迅 - 富士山下",
        artist="陈奕迅",
        media_uri="/static/media/user_music_陈奕迅 - 富士山下.mp3",
        copyright_status=MediaCopyrightStatus.LICENSED,
        status=MediaStatus.ACTIVE,
    )
    db_session.add(media)
    await db_session.flush()
    return media


async def test_handler_plays_records_and_sends_event(db_session, seed):
    elder_id = str(seed["elders"]["single"].elder_id)
    await _seed_active_music(db_session)
    events: list[dict] = []

    async def send_event(ev: dict) -> None:
        events.append(ev)

    token = RuntimeBridge.set(
        {"db_factory": _FakeFactory(db_session), "send_event": send_event}
    )
    try:
        result = await play_media.play_media_handler({"query": "富士山茶"}, {"elder_id": elder_id})
    finally:
        RuntimeBridge.reset(token)

    assert result["status"] == "playing"
    assert "富士山下" in result["title"]
    assert events and events[0]["type"] == "media_play"
    assert events[0]["media_id"]  # WS 下行载荷齐备（前端弹窗定位用）
    rows = (await db_session.execute(select(MediaHistory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].elder_id == seed["elders"]["single"].elder_id
    assert rows[0].source == "voice_rule"  # 主控工具点播与规则快路径同口径留痕


async def test_handler_not_found_returns_library_titles(db_session, seed):
    elder_id = str(seed["elders"]["single"].elder_id)
    await _seed_active_music(db_session)
    token = RuntimeBridge.set({"db_factory": _FakeFactory(db_session)})
    try:
        result = await play_media.play_media_handler(
            {"query": "量子力学"}, {"elder_id": elder_id}
        )
    finally:
        RuntimeBridge.reset(token)

    assert result["status"] == "not_found"
    assert "量子力学" in result["reply_hint"]
    assert "陈奕迅 - 富士山下" in result["library_titles"]
    rows = (await db_session.execute(select(MediaHistory))).scalars().all()
    assert rows == []  # 匹配不到不留痕（禁编造播放成功）


async def test_handler_without_elder_refuses():
    """未声纹验证（无 elder_id）：拒绝点播并给出声纹引导话术。"""
    result = await play_media.play_media_handler({"query": "富士山下"}, {})
    assert result["status"] == "need_voiceprint"
    assert "声纹" in result["reply_hint"]


async def test_handler_empty_query_error():
    result = await play_media.play_media_handler({"query": "  "}, {})
    assert result["status"] == "error"
