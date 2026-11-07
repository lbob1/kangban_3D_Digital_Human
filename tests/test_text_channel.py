"""Day5 文字通道单测（大纲 4.6）。

VoiceSession.handle_text 与语音轮同一管线；服务端校验：
- 空文本 → error empty_text，不产生回复与打点；
- 超长（>500）→ 截断至 500 字符照常回复；
- 正常文本 → action/tts/done 事件齐全，done.reply 与 tts 文本一致；
- LLM 路径（mock 流）→ done.reply 来自 LLM，打点 llm 字段落库。
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.services.voice import VoiceSession


class FakeTTS:
    def __init__(self) -> None:
        self.last_first_byte_ms = 5.0

    async def synthesize(self, text: str) -> bytes:
        return b"\x01\x00" * 32

    async def synthesize_with_viseme(self, text: str):
        from app.integrations import SynthesisResult

        return SynthesisResult(await self.synthesize(text))

    def sample_rate(self) -> int:
        return 16000


def make_session(db_connection, events: list[dict]) -> VoiceSession:
    async def collect(event: dict) -> None:
        events.append(event)

    factory = async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    return VoiceSession(
        device_id=uuid.uuid4(),
        candidates=[],
        embeddings={},
        send_event=collect,
        db_factory=factory,
    )


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: FakeTTS())
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: False)


async def test_text_turn_full_pipeline(db_connection, db_session):
    events: list[dict] = []
    session = make_session(db_connection, events)
    await session.handle_text("今天天气怎么样")

    types = [e["type"] for e in events]
    assert "tts" in types and "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["text"] == "今天天气怎么样"
    assert done["reply"]
    tts = next(e for e in events if e["type"] == "tts")
    assert tts["text"] in done["reply"]  # TTS 分句来自同一回复
    rows = await db_session.execute(text("SELECT asr_wait_ms, success FROM latency_metrics"))
    assert rows.fetchall() == [(None, True)]  # 文字轮无 ASR、正常成功


async def test_empty_text_rejected_without_turn(db_connection, db_session):
    events: list[dict] = []
    session = make_session(db_connection, events)
    await session.handle_text("   ")

    assert [e["type"] for e in events] == ["error"]
    assert events[0]["code"] == "empty_text"
    rows = await db_session.execute(text("SELECT COUNT(*) FROM latency_metrics"))
    assert rows.scalar() == 0


async def test_overlong_text_truncated_to_500(db_connection, db_session):
    events: list[dict] = []
    session = make_session(db_connection, events)
    await session.handle_text("好" * 800)

    done = next(e for e in events if e["type"] == "done")
    assert len(done["text"]) == 500
    rows = await db_session.execute(text("SELECT COUNT(*) FROM latency_metrics"))
    assert rows.scalar() == 1  # 截断后照常成轮


async def test_text_uses_llm_when_configured(db_connection, db_session, monkeypatch):
    async def fake_stream(_messages):
        yield "今天天气很好。"

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session.handle_text("今天天气怎么样")

    done = next(e for e in events if e["type"] == "done")
    assert done["reply"] == "今天天气很好。"
    rows = await db_session.execute(text("SELECT llm_first_ms, success FROM latency_metrics"))
    llm_first, success = rows.fetchone()
    assert llm_first is not None and success is True


async def test_placeholder_threshold_env_not_required():
    """熔断阈值默认值即大纲值，.env 不配置也生效（口径确认测试）。"""
    s = get_settings()
    assert (s.asr_prompt_seconds, s.llm_placeholder_seconds, s.tts_slow_seconds) == (
        2.0,
        3.0,
        1.0,
    )
