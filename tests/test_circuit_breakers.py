"""Day5 超时熔断三档单测（大纲 5.2）。

阈值经 Settings 实例属性 monkeypatch 压小，避免真实等待：
- 一档 ASR：终句等待超时 → hint asr_slow（原死线行为不变，仍出终句回复）；
- 二档 LLM：首 token 超时 → 「我想一想」占位每轮至多 1 次，placeholder_used 落库；
- 三档 TTS：首包超时 → hint tts_slow，每轮至多 1 次，播放不阻塞。
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.integrations import ASRResult, SynthesisResult
from app.services.voice import VoiceSession


class SlowFirstTokenLLM:
    """首 token 前停顿的假 LLM 流。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def __call__(self, _messages):
        import asyncio

        await asyncio.sleep(self.delay)
        yield "今天天气不错，适合出去走走。"


class FakeTTS:
    def __init__(self, first_byte_ms: float = 5.0) -> None:
        self.last_first_byte_ms = first_byte_ms

    async def synthesize(self, text: str) -> bytes:
        return b"\x01\x00" * 32

    async def synthesize_with_viseme(self, text: str) -> SynthesisResult:
        return SynthesisResult(await self.synthesize(text))

    def sample_rate(self) -> int:
        return 16000


class SlowASRSession:
    """终句前停顿的假 FunASR 会话（熔断一档触发场景）。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def start_session(self) -> None:
        pass

    async def feed_audio(self, audio_16k: bytes) -> None:
        pass

    async def finish(self) -> None:
        pass

    async def results(self):
        import asyncio

        await asyncio.sleep(self.delay)
        yield ASRResult(
            text="今天天气怎么样", is_final=True, mode="2pass-offline", confidence=0.9
        )

    async def close(self) -> None:
        pass


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
def _fast_tts(monkeypatch):
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: FakeTTS(first_byte_ms=5.0))


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch):
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: False)


# ---- 熔断二档：LLM 占位 ----


async def test_llm_slow_first_token_sends_placeholder_once(db_connection, db_session, monkeypatch):
    get_settings().llm_placeholder_seconds = 0.05
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.voice.stream_chat", SlowFirstTokenLLM(delay=0.2)
    )

    events: list[dict] = []
    session = make_session(db_connection, events)
    reply = await session._reply_and_respond("今天天气怎么样")

    placeholders = [e for e in events if e["type"] == "tts" and e["text"] == "我想一想，请稍等。"]
    assert len(placeholders) == 1  # 每轮至多 1 次
    assert reply  # 原回复照常到达
    rows = await db_session.execute(text("SELECT placeholder_used FROM latency_metrics"))
    assert rows.scalars().all() == [True]


async def test_llm_fast_no_placeholder(db_connection, db_session, monkeypatch):
    get_settings().llm_placeholder_seconds = 5.0
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.voice.stream_chat", SlowFirstTokenLLM(delay=0.01)
    )

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._reply_and_respond("今天天气怎么样")

    assert not [e for e in events if e["type"] == "tts" and "我想一想" in e["text"]]
    rows = await db_session.execute(text("SELECT placeholder_used FROM latency_metrics"))
    assert rows.scalars().all() == [False]


# ---- 熔断三档：TTS 文字先行 ----


async def test_tts_slow_first_byte_sends_hint_once(db_connection, db_session, monkeypatch):
    get_settings().tts_slow_seconds = 0.01  # 10ms：fake 首包 87.5ms 必触发
    monkeypatch.setattr(
        "app.services.voice.get_tts_provider", lambda: FakeTTS(first_byte_ms=87.5)
    )

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._reply_and_respond("今天天气怎么样")

    hints = [e for e in events if e["type"] == "hint" and e["code"] == "tts_slow"]
    assert len(hints) == 1  # 每轮至多 1 次（多分句不重复）
    assert hints[0]["text"]


async def test_tts_fast_no_hint(db_connection, db_session):
    get_settings().tts_slow_seconds = 60.0
    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._reply_and_respond("今天天气怎么样")
    assert not [e for e in events if e["type"] == "hint"]


# ---- 熔断一档：ASR 慢提示（原死线行为不变）----


async def test_asr_slow_sends_hint_but_still_replies(
    db_connection, db_session, monkeypatch
):
    get_settings().asr_prompt_seconds = 0.05
    monkeypatch.setattr(
        "app.services.voice.create_asr_session", lambda: SlowASRSession(delay=0.2)
    )

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._handle_transcription(b"\x00\x00" * 1600)

    hints = [e for e in events if e["type"] == "hint" and e["code"] == "asr_slow"]
    assert len(hints) == 1
    assert hints[0]["text"] == "没听清，请再说一遍"
    # 原链路不受影响：终句照常回复
    dones = [e for e in events if e["type"] == "done"]
    assert dones and dones[0]["reply"]


async def test_asr_fast_no_hint(db_connection, db_session, monkeypatch):
    get_settings().asr_prompt_seconds = 60.0
    monkeypatch.setattr(
        "app.services.voice.create_asr_session", lambda: SlowASRSession(delay=0.01)
    )

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._handle_transcription(b"\x00\x00" * 1600)
    assert not [e for e in events if e["type"] == "hint"]
