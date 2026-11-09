"""Day6 POC-08 复测：天气快路径延迟对比（大纲 5.2 / POC-08 Day6 部分）。

用法：uv run --extra system-one --no-sync python scripts/bench_day6_weather.py [--turns 20]

- 复用 Day5 bench_latency.py 手法：文字通道（handle_text，与语音轮同一回复管线）
  串行发送「今天天气怎么样」，并发 1、间隔 ≥3s；
- Day6 管线：rule 层 weather_today → weather_fast_path handler
  （真实 MCP stdio get_weather + 1 次受控 LLM 精简，llm_used 单列计量）；
- 每轮 1 行落 kangban_dev.latency_metrics；P50/P95 用 SQL percentile_cont 复算
  （与 POC-08 同口径），对比 Day5 基线 e2e P50=2560.7 / P95=4560.1；
- token 单列口径：精简调用 token 不入 latency_metrics，按 weather_answered
  日志 llm_used/source/degraded 元数据单列计量（原始音频/token 不落盘）；
- 原始逐次 → .cache/bench_day6/raw_weather.jsonl，汇总 → summary_weather.txt
  （均不入库）。
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
from app.tools.weather_mcp import get_weather_client

UTTERANCE = "今天天气怎么样"
INTERVAL_SECONDS = 3.0
OUT_DIR = Path(".cache/bench_day6")
DAY5_BASELINE = {"e2e_p50": 2560.7, "e2e_p95": 4560.1}  # POC-08 Day5 复测口径

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


def delta(current: float | None, base: float) -> str:
    if current is None:
        return "-"
    pct = (current - base) / base * 100
    return f"{pct:+.0f}%（{current - base:+.1f}ms）"


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
    weather = next((e for e in events if e["type"] == "weather"), None)
    data = (weather or {}).get("data", {})
    return {
        "turn": idx,
        "wall_ms": round(wall_ms, 1),
        "reply_ok": bool(done and done["reply"]),
        "weather_event": weather is not None,
        "weather_source": data.get("source"),
        "weather_degraded": bool(data.get("degraded")),
        "llm_condensed": bool(weather and weather.get("full_text")),
        "reply_chars": len((done or {}).get("reply") or ""),
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
            f" weather={'Y' if row['weather_event'] else 'N'}"
            f" source={row['weather_source']} degraded={'Y' if row['weather_degraded'] else 'N'}"
        )
        if i < args.turns:
            await asyncio.sleep(INTERVAL_SECONDS)

    with open(OUT_DIR / "raw_weather.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async with factory() as db:
        agg = (await db.execute(SUMMARY_SQL, {"started": started})).fetchone()
    (
        turns, ok, placeholders,
        asr_p50, llm_first_p50, llm_total_p50, tts_first_p50, e2e_p50, e2e_p95,
    ) = agg
    walls = [r["wall_ms"] for r in rows]
    degraded = sum(1 for r in rows if r["weather_degraded"])

    lines = [
        "== Day6 天气快路径延迟复测（文字通道固定语句 ×20，真实 MCP + LLM 精简） ==",
        f"turns={turns} success={ok} placeholder_used_turns={placeholders}"
        f" weather_degraded_turns={degraded}",
        f"客户端墙钟 wall_ms: P50={statistics.median(walls):.1f} mean={statistics.mean(walls):.1f}"
        f" min={min(walls):.1f} max={max(walls):.1f}",
        "",
        "-- 服务端口径（latency_metrics，percentile_cont 复算）--",
        f"e2e_ms（终句→首句TTS首包）: P50={fmt(e2e_p50)}  P95={fmt(e2e_p95)}",
        f"  vs Day5 基线 P50={DAY5_BASELINE['e2e_p50']}"
        f"（{delta(e2e_p50, DAY5_BASELINE['e2e_p50'])}）"
        f"  P95={DAY5_BASELINE['e2e_p95']}（{delta(e2e_p95, DAY5_BASELINE['e2e_p95'])}）",
        f"tts_first_ms P50={fmt(tts_first_p50)}",
        f"llm_first_ms P50={fmt(llm_first_p50)}  llm_total_ms P50={fmt(llm_total_p50)}"
        "（天气轮走 rule 快路径，预期 -：无主控流式决策）",
        f"asr_wait_ms P50={fmt(asr_p50)}（文字轮无 ASR，预期 -）",
        "",
        "-- token 单列口径 --",
        "精简 LLM 调用 token 不入 latency_metrics；按 weather_answered 日志"
        " llm_used/source/degraded 单列计量（每轮至多 1 次受控精简，短 prompt 无历史注入）。",
        "",
        "-- 结构性归因（如实记录）--",
        "Day5 主控流式：首 token 即分句 TTS（llm_first P50≈483ms 起播）；",
        "Day6 天气快路径：须等 MCP 数据完整 + 精简句完整生成（受控单句，不分句流式）",
        "才起播，e2e 首包上升为结构性代价；换来总轮时长下降（wall 对比 Day5"
        " P50=6042.6）与确定性路由（决策零 token，精简单列）。",
    ]
    summary = "\n".join(lines)
    print("\n" + summary)
    (OUT_DIR / "summary_weather.txt").write_text(summary + "\n", encoding="utf-8")
    await get_weather_client().close()  # 显式关 MCP stdio 会话（避免跨 task cancel scope 报错）


if __name__ == "__main__":
    asyncio.run(main())
