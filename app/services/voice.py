"""语音会话编排服务（Day3）：声纹门控 → ASR → 模板回复 → TTS。

链路口径（Day3 规划 §3，四项决策 D1/D2）：
- 音频承载：WebRTC 轨（app/realtime 由 POST /api/v1/rtc/offer 建立），
  realtime 解码重采样为 16k mono int16 PCM 后推入本服务；
- 事件承载：同源 WS /api/v1/voice/ws（用户决策①，DataChannel 留后续演进）；
- 静音检测：服务端 TEN-VAD（用户决策②）；utterance 端点先用能量粗分，
  再交声纹 Provider 的 TEN-VAD 精过滤；
- 声纹存储：voiceprints 表 + pgvector（D2 决策），本服务负责入库与身份判定，
  Provider 只做向量提取/相似度；
- 身份铁律（ADR-005）：单人间 elder_id 由设备→房间级联锁定；双人间由声纹在
  在住长者集合中匹配确定；elder_id 永不接受前端传参；
- 临时 Token：仅内存（进程内字典），不落库，默认 300s（voiceprint_token_ttl）；
- 降级（验收项 5）：FunASR 不可达/声纹未配置 → WS 事件可见降级 + 文字输入通道
  （HTTP 已有），绝不静默；
- 可观测（验收项 10）：终句写 asr_confidence/asr_e2e_ms/tts_first_byte_ms，
  低置信（<0.6）打标 asr_low_conf；原始音频不落盘不入日志（TDD §4 禁区）。
- LLM 对话属 Day5+：回复固定模板「你刚才说的是：<text>」（大纲课时 3.4）。
"""

import asyncio
import base64
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import WebSocketException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations import (
    ASRResult,
    ProviderUnavailable,
    TTSProvider,
    VoiceprintProvider,
)
from app.integrations.edge_tts import EdgeTTSProvider
from app.integrations.funasr_ws import FunASRWSProvider
from app.integrations.sherpa_voiceprint import SherpaVoiceprintProvider
from app.models import Elder, Voiceprint
from app.models.enums import ElderStatus, VoiceprintStatus

logger = get_logger(__name__)

# ---- 可调常量（utterance 端点粗分；TEN-VAD 精过滤在 Provider 内）----
ENERGY_THRESHOLD = 500  # int16 RMS 阈值：低于视为静音
SILENCE_MS_TO_CLOSE = 800  # 静音多久判定一句话结束
MIN_UTTERANCE_MS = 300  # 短于此的发声丢弃（喘息/碰撞噪声）
MAX_BUFFER_MS = 15_000  # 单句上限，防缓冲失控
LOW_CONFIDENCE = 0.6  # 低置信打标线（大纲课时 3.3）
MAX_VERIFY_FAILURES = 3  # 连续失败触发「主动确认」（ADR-008）


def template_reply(text: str) -> str:
    """Day3 固定模板回复（Day5 换 LLM）。"""
    return f"你刚才说的是：{text}"


# ---------------------------------------------------------------------------
# Provider 惰性单例（缺省可启动：未配置时返回 None，由调用方走降级）
# ---------------------------------------------------------------------------

_voiceprint_provider: VoiceprintProvider | None = None
_voiceprint_checked = False

_tts_provider: TTSProvider | None = None
_tts_checked = False


def get_voiceprint_provider() -> VoiceprintProvider | None:
    """返回 CAMPPlus Provider；未配置/缺模型时 None（声纹门控整体降级）。"""
    global _voiceprint_provider, _voiceprint_checked
    if not _voiceprint_checked:
        _voiceprint_checked = True
        settings = get_settings()
        if settings.sherpa_model_dir:
            try:
                _voiceprint_provider = SherpaVoiceprintProvider(settings.sherpa_model_dir)
                logger.info(
                    "voiceprint provider loaded",
                    extra={"event": "voiceprint_provider_loaded"},
                )
            except ProviderUnavailable as exc:
                logger.warning(
                    "voiceprint provider unavailable",
                    extra={"event": "voiceprint_provider_unavailable", "error": str(exc)},
                )
    return _voiceprint_provider


def get_tts_provider() -> TTSProvider:
    """返回 Edge-TTS Provider（无外部依赖门槛，构造即用）。"""
    global _tts_provider, _tts_checked
    if not _tts_checked:
        _tts_checked = True
        _tts_provider = EdgeTTSProvider(voice=get_settings().edge_tts_voice)
    return _tts_provider


def create_asr_session() -> FunASRWSProvider:
    """按当前配置构造一次 FunASR 会话；未配置抛 ProviderUnavailable。"""
    settings = get_settings()
    return FunASRWSProvider(settings.funasr_url, insecure=settings.funasr_insecure)


# ---------------------------------------------------------------------------
# 临时声纹 Token（仅内存，不落库）
# ---------------------------------------------------------------------------


@dataclass
class VoiceTokenRecord:
    token: str
    device_id: UUID
    elder_id: UUID
    score: float
    expires_at: float  # time.monotonic() 基准


class VoiceTokenStore:
    """进程内免验 Token 存储；重启即失效（Day3 口径，仅内存）。"""

    def __init__(self) -> None:
        self._records: dict[str, VoiceTokenRecord] = {}

    def issue(self, device_id: UUID, elder_id: UUID, score: float) -> tuple[str, float]:
        ttl = get_settings().voiceprint_token_ttl
        token = uuid.uuid4().hex
        expires_at = time.monotonic() + ttl
        self._records[token] = VoiceTokenRecord(
            token=token, device_id=device_id, elder_id=elder_id, score=score, expires_at=expires_at
        )
        self._sweep()
        return token, ttl

    def validate(self, token: str, device_id: UUID) -> VoiceTokenRecord | None:
        record = self._records.get(token)
        if record is None:
            return None
        if record.device_id != device_id or time.monotonic() > record.expires_at:
            self._records.pop(token, None)
            return None
        return record

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._records.items() if v.expires_at <= now]:
            self._records.pop(key, None)


voice_token_store = VoiceTokenStore()


# ---------------------------------------------------------------------------
# 声纹入库与查询（voiceprints 表）
# ---------------------------------------------------------------------------


async def enroll_voiceprint(
    session: AsyncSession,
    *,
    elder_id: UUID,
    audio_samples: list[bytes],
    sample_uri: str | None = None,
) -> Voiceprint:
    """录入声纹：多段音频向量取平均归一化后写 voiceprints（status=active）。

    身份校验在路由层完成（单人间设备级联 / 双人间管理员指定），本函数只做
    「样本有效性 → 向量 → 落库」。样本无效抛 ValueError（路由映射 422）。
    """
    provider = get_voiceprint_provider()
    if not audio_samples:
        raise ValueError("缺少声纹样本")

    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise LookupError("长者不存在")
    if provider is None:
        raise ProviderUnavailable("声纹模型未配置（SHERPA_MODEL_DIR），无法录入")

    embeddings = [await asyncio.to_thread(provider.extract_embedding, s) for s in audio_samples]
    mean = [
        sum(col) / len(embeddings) for col in zip(*embeddings, strict=True)
    ]
    normalized = SherpaVoiceprintProvider._normalize_list(mean)

    fingerprint = provider.model_fingerprint()
    record = Voiceprint(
        elder_id=elder_id,
        embedding=normalized,
        embedding_model=f"campplus-3dspeaker:{fingerprint[:12]}",
        sample_uri=sample_uri,
        status=VoiceprintStatus.ACTIVE,
    )
    session.add(record)
    await session.commit()
    logger.info(
        "voiceprint enrolled",
        extra={
            "event": "voiceprint_enrolled",
            "elder_id": str(elder_id),
            "voiceprint_id": str(record.voiceprint_id),
            "samples": len(audio_samples),
        },
    )
    return record


async def list_voiceprints(session: AsyncSession, elder_id: UUID) -> list[Voiceprint]:
    """查询某长者的声纹记录（管理员接口用，不返回向量本体）。"""
    return list(
        (
            await session.execute(
                select(Voiceprint)
                .where(Voiceprint.elder_id == elder_id)
                .order_by(Voiceprint.created_at.desc())
            )
        ).scalars()
    )


async def load_room_voiceprints(
    session: AsyncSession, elder_ids: list[UUID]
) -> dict[UUID, list[list[float]]]:
    """加载在住长者的 active 声纹快照（WS 建连时取一次，Day3 不做会话内刷新）。"""
    if not elder_ids:
        return {}
    rows = (
        await session.execute(
            select(Voiceprint).where(
                Voiceprint.elder_id.in_(elder_ids),
                Voiceprint.status == VoiceprintStatus.ACTIVE,
            )
        )
    ).scalars()
    result: dict[UUID, list[list[float]]] = {}
    for row in rows:
        result.setdefault(row.elder_id, []).append(list(row.embedding))
    return result


# ---------------------------------------------------------------------------
# 语音会话状态机（每个 WS 连接一个实例）
# ---------------------------------------------------------------------------

SendMessage = Callable[[dict], Awaitable[None]]


@dataclass
class _ElderSnapshot:
    """房间内可声纹匹配的长者（建连时快照）。"""

    elder_id: UUID
    name: str
    embeddings: list[list[float]]


class VoiceSession:
    """单设备语音会话：能量端点 →（首句）声纹门控 →（后续）ASR→模板→TTS。"""

    def __init__(
        self,
        device_id: UUID,
        candidates: list[tuple[UUID, str]],  # (elder_id, name)
        embeddings: dict[UUID, list[list[float]]],
        send_event: SendMessage,
        embedding_reloader: Callable[[], Awaitable[dict[UUID, list[list[float]]]]]
        | None = None,
    ) -> None:
        self.device_id = device_id
        self.send_event = send_event
        self._candidates = [
            _ElderSnapshot(eid, name, embeddings.get(eid, [])) for eid, name in candidates
        ]
        # 会话建立后才录入的声纹不在快照里：验证前允许重查一次库（录入后无需重连）
        self._embedding_reloader = embedding_reloader
        self._embeddings_reloaded = False
        self.buffer = bytearray()
        self._speech_start_ms: float | None = None
        self._silence_ms = 0.0
        self._processing = False
        self.verified_elder_id: UUID | None = None
        self.verify_failures = 0
        self.closed = False

    # ---- 音频入口（realtime 推入，16k mono int16 PCM 60ms 帧）----

    def feed_pcm(self, pcm: bytes) -> None:
        if self.closed or self._processing:
            return
        self.buffer.extend(pcm)
        self._endpoint(pcm)

    def _endpoint(self, newest_pcm: bytes) -> None:
        """能量端点：说话开始记起点，静音超过阈值判定整句结束。"""
        import numpy as np

        samples = np.frombuffer(newest_pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
        frame_ms = len(samples) / 16.0  # 16kHz
        max_buffer_bytes = int(MAX_BUFFER_MS / 1000 * 16000 * 2)

        if len(self.buffer) >= max_buffer_bytes:
            asyncio.get_running_loop().create_task(self._flush_utterance(force=True))
            return

        if rms >= ENERGY_THRESHOLD:
            if self._speech_start_ms is None:
                self._speech_start_ms = 0.0
            self._silence_ms = 0.0
        elif self._speech_start_ms is not None:
            self._silence_ms += frame_ms
            if self._silence_ms >= SILENCE_MS_TO_CLOSE:
                asyncio.get_running_loop().create_task(self._flush_utterance())

    async def _flush_utterance(self, force: bool = False) -> None:
        pcm = bytes(self.buffer)
        self.buffer.clear()
        self._speech_start_ms = None
        self._silence_ms = 0.0
        if len(pcm) < int(MIN_UTTERANCE_MS / 1000 * 16000 * 2) and not force:
            return
        if not pcm:
            return
        self._processing = True
        try:
            if self.verified_elder_id is None:
                await self._handle_verification(pcm)
            else:
                await self._handle_transcription(pcm)
        except Exception as exc:  # noqa: BLE001 —— 单句处理崩溃必须可见可恢复，绝不静默
            logger.error(
                "utterance processing crashed",
                extra={"event": "utterance_crashed", "error": type(exc).__name__ + ": " + str(exc)},
            )
            try:
                await self.send_event(
                    {
                        "type": "error",
                        "code": "utterance_failed",
                        "message": "本轮处理出错，请再说一次",
                    }
                )
            except Exception:  # noqa: BLE001 —— 连接已死时不再抛
                pass
        finally:
            self._processing = False

    # ---- 首句：声纹门控 ----

    async def _handle_verification(self, pcm: bytes) -> None:
        provider = get_voiceprint_provider()
        if provider is None:
            await self.send_event(
                {
                    "type": "error",
                    "code": "voiceprint_unavailable",
                    "message": "声纹服务未配置，请使用文字输入",
                }
            )
            return
        if not any(c.embeddings for c in self._candidates):
            if self._embedding_reloader is not None and not self._embeddings_reloaded:
                self._embeddings_reloaded = True
                fresh = await self._embedding_reloader()
                for c in self._candidates:
                    c.embeddings = fresh.get(c.elder_id, c.embeddings)
        if not any(c.embeddings for c in self._candidates):
            await self.send_event(
                {
                    "type": "error",
                    "code": "no_enrolled_voiceprint",
                    "message": "房间内长者尚未录入声纹",
                }
            )
            return

        try:
            embedding = await asyncio.to_thread(provider.extract_embedding, pcm)
        except (ValueError, ProviderUnavailable) as exc:
            await self.send_event(
                {"type": "verification", "accepted": False, "score": None, "reason": str(exc)}
            )
            return

        best: tuple[float, UUID] | None = None
        for candidate in self._candidates:
            for reference in candidate.embeddings:
                score = provider.cosine(embedding, reference)
                if best is None or score > best[0]:
                    best = (score, candidate.elder_id)

        assert best is not None
        score, elder_id = best
        threshold = get_settings().voiceprint_threshold
        passed = score >= threshold
        logger.info(
            "voiceprint verified",
            extra={
                "event": "voiceprint_verified",
                "device_id": str(self.device_id),
                "elder_id": str(elder_id),
                "score": round(score, 4),
                "accepted": passed,
                "identity_source": "voiceprint_match",
            },
        )
        if passed:
            self.verified_elder_id = elder_id
            token, ttl = voice_token_store.issue(self.device_id, elder_id, score)
            expires_at = datetime.now(UTC).timestamp() + ttl
            # 单条 verification 事件同时携带分数与免验 Token（协议：accepted/score/token/expires_at）
            await self.send_event(
                {
                    "type": "verification",
                    "accepted": True,
                    "score": round(score, 4),
                    "token": token,
                    "expires_at": expires_at,
                    "elder_id": str(elder_id),
                }
            )
            await self.send_event({"type": "state", "state": "verified"})
        else:
            self.verify_failures += 1
            await self.send_event(
                {"type": "verification", "accepted": False, "score": round(score, 4)}
            )
            if self.verify_failures >= MAX_VERIFY_FAILURES:
                await self.send_event(
                    {
                        "type": "need_confirm",
                        "message": "连续多次声纹未通过，请在大屏选择长者",
                        "candidates": [c.name for c in self._candidates],
                    }
                )

    # ---- 后续句：ASR → 模板回复 → TTS ----

    async def _handle_transcription(self, pcm: bytes) -> None:
        settings = get_settings()
        asr: FunASRWSProvider
        try:
            asr = create_asr_session()
            await asr.start_session()
        except ProviderUnavailable:
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unavailable",
                    "message": "语音识别服务未配置，请使用文字输入",
                }
            )
            return
        except OSError as exc:
            # FunASR 容器不可达：可见降级，不白屏不静默（验收项 5）
            logger.warning(
                "funasr unreachable",
                extra={"event": "funasr_unreachable", "error": str(exc)},
            )
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unreachable",
                    "message": "语音识别服务暂不可用，请使用文字输入",
                }
            )
            return
        except WebSocketException as exc:
            # websockets 库异常（握手被拒/连接被关）不是 OSError，必须单独兜住
            logger.warning(
                "funasr ws error",
                extra={"event": "funasr_unreachable", "error": f"WebSocketException: {exc}"},
            )
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unreachable",
                    "message": "语音识别服务连接失败，请稍后再说一次",
                }
            )
            return

        t0 = time.perf_counter()
        try:
            chunk = 16000 // 10  # 100ms 一块喂入
            for i in range(0, len(pcm), chunk * 2):
                await asr.feed_audio(pcm[i : i + chunk * 2])
            await asr.finish()

            final_text: str | None = None
            final_confidence = 0.0
            async for result in asr.results():
                if result.text:
                    await self.send_event(
                        {
                            "type": "transcript",
                            "text": result.text,
                            "mode": result.mode,
                            "is_final": result.is_final,
                            "confidence": round(result.confidence, 4),
                        }
                    )
                if result.is_final and "offline" in result.mode:
                    final_text = result.text
                    final_confidence = result.confidence
                    asr_e2e_ms = (time.perf_counter() - t0) * 1000
                    logger.info(
                        "asr final",
                        extra={
                            "event": "asr_final",
                            "elder_id": str(self.verified_elder_id),
                            "asr_confidence": round(final_confidence, 4),
                            "asr_e2e_ms": round(asr_e2e_ms, 1),
                            "mode": result.mode,
                            "asr_low_conf": final_confidence < LOW_CONFIDENCE,
                        },
                    )
                    break
        finally:
            await asr.close()

        if not final_text:
            await self.send_event({"type": "done", "text": ""})
            return

        reply = template_reply(final_text)
        await self._synthesize_and_send(reply)
        await self.send_event({"type": "done", "text": final_text, "reply": reply})

    async def _synthesize_and_send(self, text: str) -> None:
        tts = get_tts_provider()
        try:
            pcm = await tts.synthesize(text)
        except Exception as exc:  # noqa: BLE001 —— TTS 失败可见降级，不中断会话
            logger.warning(
                "tts failed",
                extra={"event": "tts_failed", "error": str(exc)},
            )
            await self.send_event(
                {
                    "type": "error",
                    "code": "tts_failed",
                    "message": "语音合成失败，已用文字回复",
                    "reply_text": text,
                }
            )
            return
        await self.send_event(
            {
                "type": "tts",
                "text": text,
                "audio": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": tts.sample_rate(),
            }
        )

    # ---- Token 免验续期（前端可携带上次 token 重连）----

    def try_resume(self, token: str) -> bool:
        record = voice_token_store.validate(token, self.device_id)
        if record is None:
            return False
        self.verified_elder_id = record.elder_id
        return True

    def close(self) -> None:
        self.closed = True


class VoiceSessionManager:
    """设备级会话注册表：realtime 音频按 device_id 找会话。"""

    def __init__(self) -> None:
        self._sessions: dict[UUID, VoiceSession] = {}

    def register(self, session: VoiceSession) -> None:
        old = self._sessions.get(session.device_id)
        if old is not None:
            old.close()
        self._sessions[session.device_id] = session

    def unregister(self, device_id: UUID) -> None:
        session = self._sessions.pop(device_id, None)
        if session is not None:
            session.close()

    def feed_pcm(self, device_id: UUID, pcm: bytes) -> None:
        session = self._sessions.get(device_id)
        if session is not None:
            session.feed_pcm(pcm)


voice_session_manager = VoiceSessionManager()
