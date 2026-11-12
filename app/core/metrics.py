"""零依赖进程内指标采集（Day10 步骤 3，主文档 §4.2）。

设计口径：
- 手写 Prometheus 文本格式，不引第三方库（零框架项目口径）；
- 进程内计数器 + 环形缓冲（线程安全：uvicorn 单进程多协程，锁开销可忽略）；
- DB 口径指标（分层命中率 / S1 闭环率）由 routes/metrics.py 查 decision_logs 聚合，
  与 eval/gate.py 阈值口径同源（s1_closure_rate = escalated_to_agent=false 占比）；
- hallucination_block_total = 运行时幻觉工具调用拦截计数（guard 拒绝 + 未注册工具名），
  与 eval「escalated + reply=None + tool_calls=0」断言同一拦截面。

METRICS_ENABLED=false 时中间件与端点整体停用（端点 404）。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

# 环形缓冲容量：1000 个样本足够 P50/P95 平滑（内存 ~8KB）
_DURATION_MAXLEN = 1000

_lock = threading.Lock()
_counters: dict[str, int] = {}
_durations: deque[float] = deque(maxlen=_DURATION_MAXLEN)


def inc(name: str, value: int = 1) -> None:
    """通用计数器自增（http_requests_total / hallucination_block_total 等）。"""
    with _lock:
        _counters[name] = _counters.get(name, 0) + value


def record_http_duration(duration_seconds: float) -> None:
    """记录一次请求耗时（秒）进环形缓冲，用于 P50/P95 分位计算。"""
    with _lock:
        _durations.append(duration_seconds)


def add_llm_usage(usage: dict[str, Any] | None) -> None:
    """累计 LLM token 用量（llm.py 单一调用点接线）。

    字段名按供应商原样识别常见键：prompt/completion（OpenAI 兼容）优先，
    其次 input/output；非数值忽略。
    """
    if not usage:
        return
    input_tokens = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("input_token_count")
        or 0
    )
    output_tokens = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("output_token_count")
        or 0
    )
    if isinstance(input_tokens, (int, float)) and input_tokens > 0:
        inc("llm_tokens_input_total", int(input_tokens))
    if isinstance(output_tokens, (int, float)) and output_tokens > 0:
        inc("llm_tokens_output_total", int(output_tokens))


def _quantile(sorted_values: list[float], q: float) -> float:
    """最近邻分位（样本不足返回 0.0）。"""
    if not sorted_values:
        return 0.0
    index = min(int(q * (len(sorted_values) - 1)), len(sorted_values) - 1)
    return sorted_values[index]


def snapshot() -> dict:
    """采集快照（测试断言与 render 共用）。"""
    with _lock:
        counters = dict(_counters)
        durations = sorted(_durations)
    return {
        "counters": counters,
        "http_p50": _quantile(durations, 0.50),
        "http_p95": _quantile(durations, 0.95),
        "http_samples": len(durations),
    }


def _fmt(value: float) -> str:
    """数值格式化：整数不带小数点，浮点保留 6 位。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}"


def render(db_lines: list[str] | None = None) -> str:
    """渲染 Prometheus 文本格式（ exposition format）。

    db_lines：DB 聚合指标行（routes/metrics.py 传入），与进程指标拼合同一响应。
    """
    snap = snapshot()
    counters = snap["counters"]
    lines: list[str] = []

    lines.append("# HELP http_requests_total Total HTTP requests processed.")
    lines.append("# TYPE http_requests_total counter")
    lines.append(f"http_requests_total {counters.get('http_requests_total', 0)}")

    lines.append("# HELP http_request_duration_seconds HTTP request latency quantiles (seconds).")
    lines.append("# TYPE http_request_duration_seconds gauge")
    lines.append(f'http_request_duration_seconds{{quantile="0.5"}} {_fmt(snap["http_p50"])}')
    lines.append(f'http_request_duration_seconds{{quantile="0.95"}} {_fmt(snap["http_p95"])}')

    lines.append("# HELP llm_tokens_total LLM token consumption by kind.")
    lines.append("# TYPE llm_tokens_total counter")
    lines.append(f'lllm_tokens_total{{kind="input"}} {counters.get("llm_tokens_input_total", 0)}')
    lines.append(f'lllm_tokens_total{{kind="output"}} {counters.get("llm_tokens_output_total", 0)}')

    lines.append("# HELP hallucination_block_total Runtime hallucinated tool-call interceptions.")
    lines.append("# TYPE hallucination_block_total counter")
    lines.append(f"hallucination_block_total {counters.get('hallucination_block_total', 0)}")

    if db_lines:
        lines.extend(db_lines)
    return "\n".join(lines) + "\n"
