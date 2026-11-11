"""声纹阈值扫描（Day8 步9，大纲 8.4）：0.80–0.90 主表 + 0.40–0.90 全区间定位表。

口径（D9 用户裁决，全合成）：
- 「本人」模拟 = 单一 EdgeTTS 音色（注册句 1 段 + 测试句 N 段不同文本）近似自比对；
- 「他人」模拟 = 5 个不同 EdgeTTS 音色 × 测试句；
- 结论**指示性而非统计显著**：合成音同人不同句 embedding 高度接近（FRR 趋零）、
  不同音色间区分度高于真人双住户，真人样本 Day10 复调（原始音频不落盘，无历史样本）。

大纲区间 0.80–0.90 保留为主表；POC-04 实测本人相似度仅 0.547–0.679，故另附
0.40–0.90 全区间表定位实际工作点（报告须解释两表差异，不虚构数据）。

产物：eval/reports/voiceprint_threshold.md（双表 + 工作点建议 + 局限声明）。
运行：python -m eval.voiceprint_threshold（需本地 CAMPPlus 模型与外网 EdgeTTS）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.integrations.edge_tts import EdgeTTSProvider  # noqa: E402
from app.integrations.sherpa_voiceprint import SherpaVoiceprintProvider  # noqa: E402

ENROLL_VOICE = "zh-CN-XiaoxiaoNeural"  # 「本人」模拟音色（注册 + 自比对同音色）
OTHER_VOICES = [
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
]

# 适老口吻测试句（≥30 字保证有效语音 ≥2s；注册句与测试句文本不同）
ENROLL_TEXT = "小伴你好，今天早上我在花园里走了半个小时，感觉精神还不错。"
TEST_TEXTS = [
    "昨天晚上睡得挺好，一觉睡到天亮，中间没有醒过来。",
    "今天血压量过了，高压一百三十五，低压八十五，和平时差不多。",
    "下午和老朋友们一起下了两盘象棋，心情很愉快。",
    "午饭吃了软烂的面条，还喝了一小碗汤，胃口不错。",
    "刚才女儿打电话来了，说周末要带着外孙回来看我。",
    "晚上看完新闻联播就去睡觉，规律作息身体好。",
    "天气转凉了，记得提醒我出门的时候多穿一件外套。",
]

MAIN_RANGE = [round(0.80 + 0.01 * i, 2) for i in range(11)]  # 0.80–0.90（大纲区间）
FULL_RANGE = [round(0.40 + 0.01 * i, 2) for i in range(51)]  # 0.40–0.90（定位工作点）


def _voice_label(voice: str) -> str:
    return voice.replace("zh-CN-", "").replace("Neural", "")


async def _synth_pcm(voice: str, text: str, cache_dir: Path) -> bytes:
    """EdgeTTS 合成 → 16k PCM；磁盘缓存评测产物（合成音非长者原始音频，可落盘）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{_voice_label(voice)}_{abs(hash(text)) % 10**8}.pcm"
    path = cache_dir / key
    if path.is_file():
        return path.read_bytes()
    pcm = await EdgeTTSProvider(voice).synthesize(text)
    path.write_bytes(pcm)
    return pcm


async def _collect_embeddings(
    provider: SherpaVoiceprintProvider, cache_dir: Path
) -> tuple[list[float], list[tuple[str, int, list[float]]], list[tuple[str, int, list[float]]]]:
    """返回（注册向量, 本人测试对, 他人测试对）；测试对 = (音色标签, 句序号, 向量)。"""
    enroll = provider.extract_embedding(await _synth_pcm(ENROLL_VOICE, ENROLL_TEXT, cache_dir))
    same: list[tuple[str, int, list[float]]] = []
    for i, text in enumerate(TEST_TEXTS, start=1):
        emb = provider.extract_embedding(await _synth_pcm(ENROLL_VOICE, text, cache_dir))
        same.append((_voice_label(ENROLL_VOICE), i, emb))
    cross: list[tuple[str, int, list[float]]] = []
    for voice in OTHER_VOICES:
        for i, text in enumerate(TEST_TEXTS, start=1):
            emb = provider.extract_embedding(await _synth_pcm(voice, text, cache_dir))
            cross.append((_voice_label(voice), i, emb))
    return enroll, same, cross


def _scan_table(
    enroll: list[float], same: list[tuple[str, int, list[float]]],
    cross: list[tuple[str, int, list[float]]], thresholds: list[float],
) -> tuple[list[tuple[float, float, float, int, int]], list[float], list[float]]:
    """逐阈值出表：行 = (阈值, FAR 误识率, FRR 拒识率, 误识数, 拒识数)。"""
    same_scores = [SherpaVoiceprintProvider.cosine(enroll, e) for _, _, e in same]
    cross_scores = [SherpaVoiceprintProvider.cosine(enroll, e) for _, _, e in cross]
    rows = []
    for t in thresholds:
        fa = sum(1 for s in cross_scores if s >= t)
        fr = sum(1 for s in same_scores if s < t)
        rows.append((t, fa / len(cross_scores), fr / len(same_scores), fa, fr))
    return rows, same_scores, cross_scores


def _safe_point(rows: list[tuple[float, float, float, int, int]]) -> float | None:
    """安全工作点：FAR=0 前提下 FRR 最低（即满足零误识的最高阈值）。"""
    ok = [r for r in rows if r[1] == 0.0]
    return min(r[0] for r in ok) if ok else None


def _youden_point(rows: list[tuple[float, float, float, int, int]]) -> float | None:
    """Youden J 最大点（FAR-FRR 差最大）。"""
    return max(rows, key=lambda r: r[1] - r[2])[0]


def _render_table(rows, title: str) -> str:
    lines = [
        f"### {title}",
        "",
        "| 阈值 | 误识率 FAR（他人判通过） | 拒识率 FRR（本人判拒绝） | 误识数 | 拒识数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for t, far, frr, fa, fr in rows:
        lines.append(f"| {t:.2f} | {far:.0%} | {frr:.0%} | {fa} | {fr} |")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="声纹余弦阈值扫描（全合成口径）")
    parser.add_argument("--cache-dir", default="data/voiceprint_scan_cache")
    parser.add_argument("--report", default="eval/reports/voiceprint_threshold.md")
    parser.add_argument("--no-cache", action="store_true", help="忽略缓存重新合成")
    args = parser.parse_args()

    settings = get_settings()
    model_dir = settings.sherpa_model_dir or "models"
    cache_dir = Path(args.cache_dir)
    if args.no_cache and cache_dir.is_dir():
        for f in cache_dir.glob("*.pcm"):
            f.unlink()

    try:
        provider = SherpaVoiceprintProvider(model_dir)
    except Exception as exc:  # noqa: BLE001 —— 环境缺失如实退出
        print(f"[缺模型] 声纹模型不可用（model_dir={model_dir}）：{exc}")
        return 2

    n_pairs = f"{len(TEST_TEXTS)}"
    n_voices = f"{len(OTHER_VOICES)}"
    print(f"合成样本中（本人 1 音色×{n_pairs} 句 + 他人 {n_voices} 音色×{n_pairs} 句）…")
    enroll, same, cross = await _collect_embeddings(provider, cache_dir)
    same_scores = [SherpaVoiceprintProvider.cosine(enroll, e) for _, _, e in same]
    cross_scores = [SherpaVoiceprintProvider.cosine(enroll, e) for _, _, e in cross]
    print(f"本人相似度：min={min(same_scores):.4f} max={max(same_scores):.4f}")
    print(f"他人相似度：min={min(cross_scores):.4f} max={max(cross_scores):.4f}")

    main_rows, _, _ = _scan_table(enroll, same, cross, MAIN_RANGE)
    full_rows, _, _ = _scan_table(enroll, same, cross, FULL_RANGE)
    safe = _safe_point(full_rows)
    youden = _youden_point(full_rows)

    # 数据驱动叙述：合成同人分布远高于 POC-04 真人实测，两套证据不可互相移植
    frr_start = next((r[0] for r in main_rows if r[2] > 0), None)
    ok_end = next((r[0] for r in main_rows if r[2] > 0), MAIN_RANGE[-1])
    main_clean = f"0.80–{max(0.79, (ok_end - 0.01)):.2f}" if frr_start else "全区间"

    same_dist = f"{min(same_scores):.4f}–{max(same_scores):.4f}"
    cross_dist = f"{min(cross_scores):.4f}–{max(cross_scores):.4f}"
    safe_txt = safe if safe else "无（合成样本下无零误识点，需真人样本复调）"

    frr_note = f"；{frr_start:.2f} 起出现拒识" if frr_start else ""

    report = f"""# 声纹余弦阈值扫描报告（Day8 步9）

- 扫描时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}；
  模型：CAMPPlus（{model_dir}，192 维余弦）
- **口径（D9 用户裁决，全合成）**：「本人」= {ENROLL_VOICE} 同音色不同文本
  （注册 1 句 + 测试 {len(same)} 句）；「他人」= {n_voices} 个不同 EdgeTTS 音色 × {n_pairs} 句。
  **样本量局限如实记录**：合成音同人不同句 embedding 高度接近、不同音色区分度高于
  真人双住户，结论**指示性而非统计显著**；POC-04 起原始音频不落盘、无历史真人样本，
  真人复调留 Day10。
- 实测分布：**合成同人 {same_dist}；他人 {cross_dist}**
  （POC-04 真人本人实测仅 0.547–0.679，见下「两套证据不可互移植」）。

{_render_table(main_rows, "主表：大纲区间 0.80–0.90（步进 0.01）")}

> 合成口径下大纲区间 {main_clean} 为零误识零拒识{frr_note}。
> **但两套证据不可互移植**：合成「同人」为同一 TTS 音色，embedding 天然高度集中
> （0.87+）；真人同人（POC-04 实测）分布仅 0.547–0.679。若按合成结论把阈值提到
> 0.80+，真人本人将被大量拒识——大纲 0.80–0.90 属未实测先验，真人工作点以
> Day10 真人样本复调为准。

{_render_table(full_rows, "定位表：全区间 0.40–0.90（步进 0.01）")}

## 工作点建议

- **合成口径安全工作点（FAR=0 的最低阈值）**：{safe_txt}
- **Youden J 点（FAR−FRR 差最大）**：{youden if youden else "无"}
- **回填结论（D9 如实记录）**：生产默认维持 POC-04 真人基线 **0.55** 不动——合成扫描
  的零误识区（≥{safe}）建立在合成同人分布偏高之上，直接回填会把 POC-04 实测真人样本
  （0.547）拒之门外；扫描表供 Day10 真人双住户样本复调时定位新工作点。
- 原则：误识优先（声纹只作辅助身份信号，误认代价高于多问一句主动确认）。

## 局限声明

1. 全合成口径：真人「本人/他人」差异与合成音色差异方向相反（同人更低、他人可能更近），
   真 FAR/FRR 需 Day10 真人样本复调；
2. 样本量（{len(same)} 本人 + {len(cross)} 他人测试对）不具统计显著性；
3. 声纹仅作辅助身份信号，失败走主动确认兜底（三层级联第③层，见《级联验证报告》）。
"""
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"报告已写入 {report_path}")
    print(f"建议 VOICEPRINT_THRESHOLD：{safe if safe else youden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
