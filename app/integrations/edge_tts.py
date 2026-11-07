"""Edge-TTS Provider（微软免费合成，无需 API Key）。

参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/providers/edge_tts.py，
适配本项目改动：
- MP3 → 16kHz PCM 解码改用 PyAV（aiortc 既有依赖），替代样板的 pydub，
  免去 ffmpeg 外部安装；
- 首包延迟（tts_first_byte_ms）与总耗时在每次合成后写结构化日志，
  供 POC-02 回填与 Day5 延迟基线（大纲课时 3.3 硬要求）；
- 音色来自 Settings.edge_tts_voice，构造即校验 voice 参数合法性。

Day5 增量（D2 决策）：synthesize_with_viseme 以 boundary="WordBoundary" 合成，
捕获词级时间戳（100ns tick，基准=合成音频起点），经 pypinyin 韵母主元音映射
生成五口型时间轴（aa/ih/uu/eh/oh）。映射为纯函数，可离线单测。
"""

import io
import time

import av
import numpy as np

from app.core.logging import get_logger
from app.integrations import SynthesisResult, TTSProvider

logger = get_logger(__name__)

TARGET_RATE = 16000

# 韵母主元音 → three-vrm VRM 标准口型 blendshape（a/i/u/e/o 五口型，大纲 5.1）
_VOWEL_TO_VISEME = {"a": "aa", "i": "ih", "u": "uu", "ü": "uu", "e": "eh", "o": "oh"}


def word_to_visemes(word: str) -> list[str]:
    """词 → 逐音节口型序列（取各音节韵母主元音）。

    非中文字符/标点/数字不产生音节（继承前值，由调用方处理）；
    韵母无元音（罕见，如「嗯」）回落 "aa"。
    """
    from pypinyin import Style, lazy_pinyin  # 延迟导入：与其他 Provider 依赖同口径

    finals = lazy_pinyin(word, style=Style.FINALS, v_to_u=True, errors="ignore")
    visemes: list[str] = []
    for final in finals:
        v = next((c for c in final if c in _VOWEL_TO_VISEME), None)
        visemes.append(_VOWEL_TO_VISEME.get(v, "aa") if v else "aa")
    return visemes


def build_viseme_timeline(
    words: list[tuple[str, float, float]], lead_ms: int = 40
) -> list[dict]:
    """WordBoundary 词列表 → 五口型时间轴。

    words 项 = (词文本, offset_ms, duration_ms)；词内多音节按时长均分；
    相邻同口型合并；非中文词（无音节）跳过——其时长自然由前后口型覆盖；
    首音节提前 lead_ms 起口型（预判张嘴，观感更贴近真人开口）。
    """
    timeline: list[dict] = []
    for text, offset_ms, duration_ms in words:
        visemes = word_to_visemes(text)
        if not visemes or duration_ms <= 0:
            continue
        lead = lead_ms if not timeline else 0  # 仅首个词整体提前
        step = duration_ms / len(visemes)
        for i, v in enumerate(visemes):
            t = max(0, round(offset_ms + step * i - lead))
            item = {"v": v, "t": t, "d": round(step)}
            if timeline and timeline[-1]["v"] == v:
                # 相邻同口型：无缝合并（覆盖合并产生的间隙/重叠）
                timeline[-1]["d"] = item["t"] + item["d"] - timeline[-1]["t"]
            else:
                timeline.append(item)
    return timeline


class EdgeTTSProvider(TTSProvider):
    """Edge-TTS 流式拉取 → PyAV 解码重采样为 16kHz mono int16 PCM。"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%") -> None:
        self.voice = voice
        self.rate = rate
        self.last_first_byte_ms: float | None = None  # 最近一次合成首包延迟
        self.last_total_ms: float | None = None  # 最近一次合成总耗时

    def sample_rate(self) -> int:
        return TARGET_RATE

    async def synthesize(self, text: str) -> bytes:
        import edge_tts  # 延迟导入：未安装时不拖垮模块加载

        t0 = time.perf_counter()
        self.last_first_byte_ms = None
        mp3 = io.BytesIO()
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                if self.last_first_byte_ms is None:
                    self.last_first_byte_ms = (time.perf_counter() - t0) * 1000
                mp3.write(chunk["data"])
        pcm = await self._decode_mp3(mp3.getvalue())
        self.last_total_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "tts_synthesized",
            extra={
                "event": "tts_synthesized",
                "tts_first_byte_ms": round(self.last_first_byte_ms or 0, 1),
                "tts_total_ms": round(self.last_total_ms, 1),
                "pcm_bytes": len(pcm),
                "voice": self.voice,
            },
        )
        return pcm

    async def synthesize_with_viseme(self, text: str) -> SynthesisResult:
        """合成 + WordBoundary 词级时间戳 → 五口型时间轴（D2 决策）。

        与 synthesize 同链路（首包/总耗时口径一致）；时间轴 t 基准 =
        合成音频起点，与 PCM 时间轴同源，前端以分句播放起点为 t0 直接调度。
        """
        import edge_tts  # 延迟导入：未安装时不拖垮模块加载

        t0 = time.perf_counter()
        self.last_first_byte_ms = None
        mp3 = io.BytesIO()
        words: list[tuple[str, float, float]] = []
        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, boundary="WordBoundary"
        )
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                if self.last_first_byte_ms is None:
                    self.last_first_byte_ms = (time.perf_counter() - t0) * 1000
                mp3.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(
                    (
                        chunk.get("text", ""),
                        (chunk.get("offset") or 0) / 1e4,  # 100ns tick → ms
                        (chunk.get("duration") or 0) / 1e4,
                    )
                )
        pcm = await self._decode_mp3(mp3.getvalue())
        self.last_total_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "tts_synthesized",
            extra={
                "event": "tts_synthesized",
                "tts_first_byte_ms": round(self.last_first_byte_ms or 0, 1),
                "tts_total_ms": round(self.last_total_ms, 1),
                "pcm_bytes": len(pcm),
                "voice": self.voice,
                "viseme_words": len(words),
            },
        )
        return SynthesisResult(pcm, build_viseme_timeline(words))

    async def _decode_mp3(self, mp3_bytes: bytes) -> bytes:
        """MP3 → 16kHz mono s16 PCM（PyAV 重采样，纯内存零临时文件）。"""

        def _decode() -> bytes:
            container = av.open(io.BytesIO(mp3_bytes))
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=TARGET_RATE
            )
            out = io.BytesIO()
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    out.write(resampled.to_ndarray().astype(np.int16).tobytes())
            return out.getvalue()

        import asyncio

        return await asyncio.to_thread(_decode)
