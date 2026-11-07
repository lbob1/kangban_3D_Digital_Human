"""Day5 延迟打点单测（大纲 5.2，D3 决策）。

连接 kangban_test（conftest session 级迁移已含 0007），函数级事务回滚：
- 模板轮：asr_wait 落库、llm 字段为空、tts_first/e2e 捕获、success=true；
- LLM 轮（mock 流）：llm_first/llm_total 落库、success=true；
- LLM 失败降级轮：fallback 模板、success=false；
- 文字轮（无 ASR）：asr_wait 为空。
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.integrations import SynthesisResult
from app.services.voice import VoiceSession


class FakeTTSWithTiming:
    """带首包计时口径的 fake：模拟 EdgeTTSProvider.last_first_byte_ms。"""

    def __init__(self) -> None:
        self.last_first_byte_ms: float | None = None

    async def synthesize(self, text: str) -> bytes:
        self.last_first_byte_ms = 87.5
        return b"\x01\x00" * 32

    async def synthesize_with_viseme(self, text: str) -> SynthesisResult:
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


async def fetch_metrics(db_session) -> list:
    rows = await db_session.execute(
        text(
            "SELECT asr_wait_ms, llm_first_ms, llm_total_ms, tts_first_ms, e2e_ms, "
            "placeholder_used, success, mode, thread_id FROM latency_metrics"
        )
    )
    return rows.fetchall()


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    tts = FakeTTSWithTiming()
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: tts)
    return tts


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch):
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: False)


async def test_template_turn_persists_one_row(db_connection, db_session):
    events: list[dict] = []
    session = make_session(db_connection, events)
    reply = await session._reply_and_respond("今天天气怎么样", asr_wait_ms=1234.5)

    assert reply
    rows = await fetch_metrics(db_session)
    assert len(rows) == 1
    (asr_w, llm_first, llm_total, tts_first, e2e, placeholder, success, mode, thread) = rows[0]
    assert asr_w == pytest.approx(1234.5)
    assert llm_first is None and llm_total is None  # 模板轮无 LLM
    assert tts_first == pytest.approx(87.5)  # 首句 TTS 首包（fake 固定口径）
    assert e2e is not None and e2e > 0  # 真实时钟，不与 fake 首包值比大小（避免时序抖动）
    assert placeholder is False
    assert success is True
    assert mode == "anonymous"  # 未声纹核身
    assert thread is None  # device 随机 UUID 无 FK 依赖，降级为不落库


async def test_llm_turn_records_first_token(db_connection, db_session, monkeypatch):
    async def fake_stream(_messages):
        yield "今天天气不错。"
        yield "适合出去走走。"

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    events: list[dict] = []
    session = make_session(db_connection, events)
    await session._reply_and_respond("今天天气怎么样")

    rows = await fetch_metrics(db_session)
    assert len(rows) == 1
    (_, llm_first, llm_total, _, _, _, success, _, _) = rows[0]
    assert llm_first is not None and llm_first > 0
    assert llm_total is not None and llm_total >= llm_first
    assert success is True


async def test_llm_failure_fallback_marks_unsuccessful(db_connection, db_session, monkeypatch):
    from app.integrations.llm import LlmUnavailable

    async def broken_stream(_messages):
        raise LlmUnavailable("连接被拒")
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", broken_stream)

    events: list[dict] = []
    session = make_session(db_connection, events)
    reply = await session._reply_and_respond("今天天气怎么样")
    assert reply  # 模板兜底，通话不中断

    rows = await fetch_metrics(db_session)
    (asr_w, llm_first, llm_total, _, _, _, success, _, _) = rows[0]
    assert success is False  # 降级轮如实标记
    assert llm_first is None and llm_total is None
    assert asr_w is None  # 文字轮（未传 asr_wait）


async def test_metrics_not_persisted_without_db_factory(db_session, monkeypatch):
    # db_factory=None：纯内存会话不打点，也不抛异常
    events: list[dict] = []

    async def collect(event: dict) -> None:
        events.append(event)

    session = VoiceSession(
        device_id=uuid.uuid4(), candidates=[], embeddings={}, send_event=collect, db_factory=None
    )
    reply = await session._reply_and_respond("今天天气怎么样")
    assert reply
    rows = await fetch_metrics(db_session)
    assert rows == []
