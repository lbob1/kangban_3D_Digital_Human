"""Day5 P2-8：延迟基线跑批（大纲 5.2 / POC-08，D3 决策口径）。

用法：uv run python scripts/bench_latency.py [--turns 20]

- 经文字通道（handle_text，与语音轮同一回复管线）串行发送固定语句
  「今天天气怎么样」，并发 1、间隔 ≥3s；
- TTS 真实 EdgeTTS、LLM 按 .env 真实配置（失败自动模板降级，success 如实记录）；
- 每轮 1 行落 kangban_dev.latency_metrics；
- 原始逐次数据 → .cache/bench_day5/raw.jsonl，汇总 → summary.txt（均不入库）；
- P50/P95 用 SQL percentile_cont 复算（与 POC-08 同口径）；
- 目标 P95 ≤1.5s（大纲课时 3.3），达不达标都如实记录——Day6 三级路由对比基线。
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.db.session import get_session_factory
from app.services.voice import VoiceSession

UTTERANCE = "今天天气怎么样"
INTERVAL_SECONDS = 3.0
OUT_DIR = Path(".cache/bench_day5")

SUMMARY_SQL = text(
    """
    SELECT
      count(*) AS turns,
      count(*) FILTER (WHERE success) AS success_turns,
      count(*) FILTER (WHERE placeholder_used) AS placeholder_turns,
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY asr_wait_ms)   AS asr_p50,
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY llm_first_ms)  AS llm_first_p50,
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY llm_total_ms)  AS llm_total_p50,
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY tts_first_ms)  AS tts_first_p50,
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY e2e_ms)        AS e2e_p50,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e_ms)        AS e2e_p95
    FROM latency_metrics
    WHERE created_at >= :started AND mode = 'anonymous'
    """
)


def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


async def run_turn(factory, idx: int) -> dict:
    events: list[dict] = []

    async def collect(event: dict) -> None:
        events.append(event)

    session = VoiceSession(
        device_id=uuid.uuid4(),
        candidates=[],
        embeddings={},
        send_event=collect,
        db_factory=factory,
    )
    t0 = time.perf_counter()
    await session.handle_text(UTTERANCE)
    wall_ms = (time.perf_counter() - t0) * 1000
    done = next((e for e in events if e["type"] == "done"), None)
    hints = sum(1 for e in events if e["type"] == "hint")
    placeholder = any(
        e["type"] == "tts" and "我想一想" in e.get("text", "") for e in events
    )
    return {
        "turn": idx,
        "wall_ms": round(wall_ms, 1),
        "reply_ok": bool(done and done["reply"]),
        "placeholder_tts": placeholder,
        "hint_events": hints,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=20)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    factory = get_session_factory()
    started = datetime.now(timezone.utc)
    rows = []
    for i in range(1, args.turns + 1):
        row = await run_turn(factory, i)
        rows.append(row)
        print(
            f"#{i:02d} wall={row['wall_ms']:7.1f}ms reply={'OK' if row['reply_ok'] else 'EMPTY'}"
            f" placeholder={'Y' if row['placeholder_tts'] else 'N'} hints={row['hint_events']}"
        )
        if i < args.turns:
            await asyncio.sleep(INTERVAL_SECONDS)

    with open(OUT_DIR / "raw.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async with factory() as db:
        agg = (await db.execute(SUMMARY_SQL, {"started": started})).fetchone()
    (
        turns, ok, placeholders,
        asr_p50, llm_first_p50, llm_total_p50, tts_first_p50, e2e_p50, e2e_p95,
    ) = agg
    walls = [r["wall_ms"] for r in rows]

    lines = [
        "== Day5 延迟基线（2026-11-07，文字通道固定语句 ×20，真实 EdgeTTS/LLM） ==",
        f"turns={turns} success={ok} placeholder_used_turns={placeholders}",
        f"客户端墙钟 wall_ms: P50={statistics.median(walls):.1f} mean={statistics.mean(walls):.1f}"
        f" min={min(walls):.1f} max={max(walls):.1f}",
        "",
        "-- 服务端口径（latency_metrics，percentile_cont 复算）--",
        f"e2e_ms（终句→首句TTS首包）: P50={fmt(e2e_p50)}  P95={fmt(e2e_p95)}   [目标 P95≤1500ms]",
        f"tts_first_ms P50={fmt(tts_first_p50)}",
        f"llm_first_ms P50={fmt(llm_first_p50)}  llm_total_ms P50={fmt(llm_total_p50)}",
        f"asr_wait_ms P50={fmt(asr_p50)}（文字轮无 ASR，预期 -）",
        "",
        f"结论：P95 {'达标' if (e2e_p95 or 9e9) <= 1500 else '不达标'}；"
        "大头占比见上（预期 LLM/TTS 首包主导，Day6 三级路由对比基线）。",
    ]
    summary = "\n".join(lines)
    print("\n" + summary)
    (OUT_DIR / "summary.txt").write_text(summary + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
