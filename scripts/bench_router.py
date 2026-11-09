"""Day6 P2-10：三级路由评测基线（50 条 fixture，词表/规则真实 + System One 真实 laya 推理）。

用法：uv run --extra system-one python scripts/bench_router.py

- 逐条 cascade.decide()：①词表 ②规则 ③laya（warm 推理）；④主控只判层不执行
  （天气 handler 注入静态桩，不拉 MCP 子进程、不调 LLM——工具执行口径归 POC-08）；
- 指标（数字可复算）：分层占比 / 意图准确率（safety+rule+s1 共 40 条，目标 ≥90%）
  / 敏感召回（8 条，目标 ≥95%）/ 闭环率（非 agent 占比）/ 各层延迟 P50/P95；
- 报告 → eval/reports/day6_router_baseline.md；bad case 回流清单 → 同目录
  bad_cases.md；原始逐条 → .cache/bench_day6/raw.jsonl（不入 Git）。
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from app.router.cascade import CascadeOutcome, CascadeRouter, _register_default_handlers
from app.router.cascade import get_cascade_router  # noqa: F401 —— 保证天气桩注册语义一致
from app.system_one.provider_laya import LayaProvider

FIXTURE = Path("eval/fixtures/day6_intents.jsonl")
REPORT_DIR = Path("eval/reports")
RAW_DIR = Path(".cache/bench_day6")


def load_fixture() -> list[dict]:
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def judge(row: dict, outcome) -> tuple[bool, bool]:
    """返回 (层正确, 整行正确)。agent 行只判层；其余行列+意图都须命中。"""
    layer_ok = outcome.route_layer == row["expected_layer"]
    if row["expected_layer"] == "agent":
        return layer_ok, layer_ok
    intent_ok = outcome.intent == row["expected_intent"]
    return layer_ok, layer_ok and intent_ok


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warm-timeout", type=float, default=120.0)
    args = parser.parse_args()

    cases = load_fixture()
    router = CascadeRouter()
    _register_default_handlers(router)
    # 天气桩：路由评测不执行工具（静态 CascadeOutcome，tool_calls=1 与真实一致）
    router.register_handler(
        "weather_fast_path",
        CascadeOutcome(reply_text="(bench stub)", ws_events=[], tool_calls=1),
    )

    provider = LayaProvider()
    provider.warmup()
    t0 = time.monotonic()
    while True:
        try:
            await provider.classify("你好")
            break
        except Exception:
            if time.monotonic() - t0 > args.warm_timeout:
                raise SystemExit("laya 预热超时（120s）——请确认 uv --extra system-one 与模型缓存")
            await asyncio.sleep(1.0)
    router.attach_system_one(provider)
    print(f"laya warm ok in {time.monotonic() - t0:.1f}s, {len(cases)} cases")

    results = []
    for case in cases:
        t = time.perf_counter()
        outcome = await router.decide(case["text"])
        wall_ms = (time.perf_counter() - t) * 1000
        layer_ok, row_ok = judge(case, outcome)
        results.append(
            {
                **case,
                "got_layer": outcome.route_layer,
                "got_intent": outcome.intent,
                "confidence": round(outcome.confidence or 0.0, 3),
                "degraded": outcome.degraded,
                "wall_ms": round(wall_ms, 2),
                "layer_ok": layer_ok,
                "ok": row_ok,
            }
        )
        flag = "OK " if row_ok else "BAD"
        conf = f"{outcome.confidence:.2f}" if outcome.confidence is not None else "-"
        print(
            f"[{flag}] {case['id']} {case['text'][:18]:<18} "
            f"expect={case['expected_layer']}/{case['expected_intent']} "
            f"got={outcome.route_layer}/{outcome.intent} conf={conf} {wall_ms:.1f}ms"
        )

    # ---- 指标（可复算口径）----
    total = len(results)
    layer_counts = Counter(r["got_layer"] for r in results)
    scored = [r for r in results if r["expected_layer"] != "agent"]
    intent_accuracy = sum(1 for r in scored if r["ok"]) / len(scored)
    safety_rows = [r for r in results if r["expected_layer"] == "safety"]
    sensitive_recall = sum(1 for r in safety_rows if r["ok"]) / len(safety_rows)
    closure_rate = sum(1 for r in results if r["got_layer"] != "agent") / total
    degraded = sum(1 for r in results if r["degraded"])
    lat_by_layer = {
        layer: [r["wall_ms"] for r in results if r["got_layer"] == layer]
        for layer in ("safety", "rule", "system_one", "agent")
    }

    lines = [
        "== Day6 三级路由评测基线（50 条，词表/规则/laya 真实推理） ==",
        f"用例：{total}（safety 8 / rule 12 / system_one 10 / agent 20）",
        "",
        "## 分层占比（实际路由）",
        *(f"- {k}: {v} ({v / total:.0%})" for k, v in sorted(layer_counts.items())),
        "",
        f"## 意图准确率（safety+rule+system_one 共 {len(scored)} 条，目标 ≥90%）",
        f"- {sum(1 for r in scored if r['ok'])}/{len(scored)} = {intent_accuracy:.1%}"
        f" → {'达标' if intent_accuracy >= 0.9 else '不达标'}",
        "",
        "## 敏感召回（目标 ≥95%）",
        f"- {sum(1 for r in safety_rows if r['ok'])}/{len(safety_rows)} = {sensitive_recall:.1%}"
        f" → {'达标' if sensitive_recall >= 0.95 else '不达标'}",
        "",
        "## 闭环率（不经主控即完成应答的占比）",
        f"- {closure_rate:.0%}（rule/safety/system_one 层直答；闭环率按路由口径，"
        "回答质量归 Day7 记忆/对话评估）",
        f"- S1 降级轮数（degraded=true，超时/未就绪）：{degraded}",
        "",
        "## 各层端到端路由延迟（decide 墙钟，含 laya warm 推理）",
        *(
            f"- {layer}: P50={pctl(v, 0.5):.1f}ms P95={pctl(v, 0.95):.1f}ms n={len(v)}"
            for layer, v in lat_by_layer.items()
            if v
        ),
        "",
        "## 成本对比口径（每 50 轮 LLM 调用次数估算）",
        "- Day5 基线：每轮 1 次流式调用 = 50 次",
        f"- Day6 实测：agent 轮 {layer_counts.get('agent', 0)} 次（决策轮，直答免二次）"
        f" + 精简答复按 POC-08 实测单列",
    ]
    report = "\n".join(lines)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "day6_router_baseline.md").write_text(report + "\n", encoding="utf-8")
    with open(RAW_DIR / "raw.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    bad = [r for r in results if not r["ok"]]
    bad_lines = ["# Day6 bad case 回流清单", ""]
    for r in bad:
        bad_lines.append(
            f"- [{r['id']}] 「{r['text']}」期望 {r['expected_layer']}/{r['expected_intent']}"
            f"，实际 {r['got_layer']}/{r['got_intent']}（conf={r['confidence']}）"
        )
    if not bad:
        bad_lines.append("（无 bad case）")
    (REPORT_DIR / "bad_cases.md").write_text("\n".join(bad_lines) + "\n", encoding="utf-8")

    print("\n" + report)
    print(f"\n报告 → {REPORT_DIR / 'day6_router_baseline.md'}；bad case → bad_cases.md")


if __name__ == "__main__":
    asyncio.run(main())
