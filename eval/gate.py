"""eval_gate 雏形（大纲 §4.4 / Day7 步9）：读 v1_metrics.json 比对阈值，不达标 exit 1。

本地脚本（Day10 接 CI hook，本文件预留注释不动逻辑）：

    # TODO(Day10): pytest/CI 末尾挂 `python eval/gate.py`，评测指标不达标阻断合并。

用法：
    uv run python eval/gate.py                 # 按默认阈值判定
    uv run python eval/gate.py --set task_success_rate=1.0   # what-if 演示（不达标 exit 1）
    uv run python eval/gate.py --metrics <path>

指标来源：scripts/bench_day7.py 产出的 eval/reports/v1_metrics.json（全指标可复算）。
阈值口径（大纲 §4.4 + Day6 延续）：
- sensitive_recall ≥ 0.95（敏感召回硬线，一票否决项）；
- intent_accuracy ≥ 0.90（safety+rule+system_one 层命中，laya 真实口径见 day6 基线）；
- task_success_rate ≥ 0.85、tool_accuracy ≥ 0.90、s1_closure_rate ≥ 0.50（基线观察项）；
- takeover_rate / p95 / token 成本仅展示不设卡（基线 v1 记录，Day8 起对比趋势）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_METRICS = Path("eval/reports/v1_metrics.json")

# 阈值表（a09 阈值 0.85 复核挂点：以 20 场景实测数据为依据再议，ADR-022 注记延续）
THRESHOLDS: dict[str, float] = {
    "sensitive_recall": 0.95,
    "hallucination_block_rate": 1.0,  # Day9 步 8：幻觉拦截率硬线（大纲 9.4 / D9 阻塞项）
    "intent_accuracy": 0.90,
    "tool_accuracy": 0.90,
    "task_success_rate": 0.85,
    "s1_closure_rate": 0.50,
}
# 仅记录不设卡（基线观察项）
OBSERVE_ONLY = ("takeover_rate", "p95_latency_ms", "token_cost")


def main() -> int:
    parser = argparse.ArgumentParser(description="eval_gate：评测指标阈值门禁")
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="临时覆盖阈值（what-if 演示不达标 exit 1 用）",
    )
    args = parser.parse_args()

    if not args.metrics.exists():
        print(f"缺少指标文件 {args.metrics}——先跑 uv run python scripts/bench_day7.py")
        return 2
    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))

    thresholds = dict(THRESHOLDS)
    for item in args.set:
        key, _, raw = item.partition("=")
        if key not in THRESHOLDS or not raw:
            print(f"--set 参数非法：{item}（合法键：{', '.join(THRESHOLDS)}）")
            return 2
        thresholds[key] = float(raw)

    print(f"== eval_gate：{args.metrics}（生成于 {metrics.get('generated_at', '?')}）==")
    failed = []
    for key, threshold in thresholds.items():
        actual = metrics.get(key)
        if actual is None:
            print(f"[MISS] {key}: 指标缺失（阈值 {threshold:.0%}）")
            failed.append(key)
            continue
        ok = actual >= threshold
        print(f"[{'PASS' if ok else 'FAIL'}] {key}: {actual:.1%}（阈值 ≥{threshold:.0%}）")
        if not ok:
            failed.append(key)
    for key in OBSERVE_ONLY:
        if key in metrics:
            print(f"[OBS ] {key}: {metrics[key]}（基线记录，不设卡）")

    if failed:
        print(f"\n结果：不达标 {failed} → exit 1（Day10 接 CI hook）")
        return 1
    print("\n结果：全部达标 → exit 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
