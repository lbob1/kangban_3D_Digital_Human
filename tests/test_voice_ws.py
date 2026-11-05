"""WS 事件协议状态机测试（mock ASR/TTS Provider，不依赖容器与模型）。

httpx 不支持 WebSocket，这里对 WS /voice/ws 所下发的事件序列做等价实测：
VoiceSession 正是 WS 事件流的唯一生产者（routes/voice.py 仅做鉴权与转发），
事件类型顺序 = WS 协议：verification → state → transcript → tts → done。
"""

from uuid import uuid4

from app.services.voice import VoiceSession, VoiceTokenStore
from tests.test_voiceprint_service import (
    FAKE_VEC_A,
    FakeASRSession,
    FakeTTSProvider,
    FakeVoiceprintProvider,
)


async def test_full_event_protocol_sequence(monkeypatch):
    """首句验证 + 第二句转写：事件类型序列与字段符合 WS 协议约定。"""
    provider = FakeVoiceprintProvider()
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: provider)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: FakeTTSProvider())
    fake_asr = FakeASRSession()
    monkeypatch.setattr("app.services.voice.create_asr_session", lambda: fake_asr)

    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    elder = uuid4()
    session = VoiceSession(
        uuid4(), [(elder, "张奶奶")], {elder: [FAKE_VEC_A]}, send_event
    )

    # 第一句：声纹门控
    session.feed_pcm(b"\x00\x00" * 16000 * 2)
    await session._flush_utterance()
    # 第二句：转写（免验）
    session.feed_pcm(b"\x00\x00" * 16000)
    await session._flush_utterance()

    types = [e["type"] for e in events]
    # verification(accepted=false 的不存在) → verification(token) → state → transcript → tts → done
    assert types[0] == "verification"
    assert events[0]["accepted"] is True
    assert events[0]["token"]
    assert "state" in types and types.index("state") < types.index("transcript")
    assert "transcript" in types and "tts" in types
    assert types[-1] == "done"
    # done 事件带最终文本与模板回复
    done = events[-1]
    assert done["text"] == "你好康伴"
    assert done["reply"] == "你刚才说的是：你好康伴"


async def test_verification_failure_event_shape(monkeypatch):
    """验证失败事件：accepted=false 且不发 token（协议字段完整性）。"""
    provider = FakeVoiceprintProvider()
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: provider)

    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    # 房间档案向量与说话人不匹配（threshold=0.99 必然失败）
    monkeypatch.setattr(
        "app.services.voice.get_settings",
        lambda: type("S", (), {"voiceprint_threshold": 0.99, "voiceprint_token_ttl": 300})(),
    )
    elder = uuid4()
    session = VoiceSession(
        uuid4(), [(elder, "李爷爷")], {elder: [[0.0, 1.0] + [0.0] * 190]}, send_event
    )
    session.feed_pcm(b"\x00\x00" * 16000 * 2)  # 偶数首字节 → A 向量 ≠ 房间 B 向量
    await session._flush_utterance()

    failures = [
        e for e in events if e["type"] == "verification" and e["accepted"] is False
    ]
    assert len(failures) == 1
    assert "token" not in failures[0]
    assert not any(e["type"] == "state" and e.get("state") == "verified" for e in events)


def test_token_ttl_default_300s():
    """免验 Token 默认 300 秒（验收项 9 前提）。"""
    store = VoiceTokenStore()
    _, ttl = store.issue(uuid4(), uuid4(), 0.9)
    assert ttl == 300
