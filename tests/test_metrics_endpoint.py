"""/metrics 端点与指标采集单测（Day10 步骤 3）。

覆盖：
- core.metrics 纯函数：计数器/分位/token 累计/Prometheus 文本格式；
- 端点：200 文本格式（进程指标 + DB 聚合行拼接）、METRICS_ENABLED=false → 404、
  DB 异常时降级输出 metrics_db_error 且进程指标照常；
- DB 聚合口径：decision_logs 事务内插行 → 分层计数 / s1_closure_rate 与 gate.py 同源可复算。
"""

from types import SimpleNamespace

import pytest

from app.api.routes import metrics as metrics_route
from app.core import metrics as core_metrics


@pytest.fixture(autouse=True)
def _reset_metrics_state():
    """进程级计数器是全局单例：每例前后清空，避免跨用例串数。"""
    core_metrics._counters.clear()
    core_metrics._durations.clear()
    yield
    core_metrics._counters.clear()
    core_metrics._durations.clear()


# ---- core.metrics 纯函数 ----


def test_inc_and_snapshot_counters():
    core_metrics.inc("http_requests_total")
    core_metrics.inc("http_requests_total", 2)
    snap = core_metrics.snapshot()
    assert snap["counters"]["http_requests_total"] == 3


def test_quantiles_empty_and_values():
    assert core_metrics.snapshot()["http_p50"] == 0.0
    for value in (0.01, 0.02, 0.03, 0.10):
        core_metrics.record_http_duration(value)
    snap = core_metrics.snapshot()
    assert snap["http_samples"] == 4
    # 最近邻分位口径：index = int(q*(n-1))
    assert snap["http_p50"] == pytest.approx(0.02)
    assert snap["http_p95"] == pytest.approx(0.03)


def test_add_llm_usage_prompt_completion():
    core_metrics.add_llm_usage({"prompt_tokens": 100, "completion_tokens": 30})
    core_metrics.add_llm_usage({"input_tokens": 5, "output_tokens": 7})
    snap = core_metrics.snapshot()
    assert snap["counters"]["llm_tokens_input_total"] == 105
    assert snap["counters"]["llm_tokens_output_total"] == 37


def test_add_llm_usage_none_and_garbage_safe():
    core_metrics.add_llm_usage(None)
    core_metrics.add_llm_usage({"prompt_tokens": "abc"})
    assert "llm_tokens_input_total" not in core_metrics.snapshot()["counters"]


def test_render_prometheus_text_format():
    core_metrics.inc("http_requests_total", 5)
    core_metrics.inc("hallucination_block_total", 2)
    core_metrics.add_llm_usage({"prompt_tokens": 10, "completion_tokens": 4})
    text = core_metrics.render(["s1_closure_rate 0.8333"])
    assert text.endswith("\n")
    assert "# TYPE http_requests_total counter" in text
    assert "http_requests_total 5" in text
    assert 'http_request_duration_seconds{quantile="0.5"} 0' in text
    assert 'llm_tokens_total{kind="input"} 10' in text
    assert 'llm_tokens_total{kind="output"} 4' in text
    assert "hallucination_block_total 2" in text
    assert "s1_closure_rate 0.8333" in text


# ---- 端点 ----


async def test_metrics_endpoint_200_format(client, monkeypatch):
    async def fake_lines(session, window_hours):
        return ["router_requests_total{layer=\"rule\"} 7", "s1_closure_rate 0.6667"]

    monkeypatch.setattr(metrics_route, "_db_aggregate_lines", fake_lines)
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "# TYPE http_requests_total counter" in body
    assert "http_requests_total" in body
    assert 'router_requests_total{layer="rule"} 7' in body
    assert "s1_closure_rate 0.6667" in body


async def test_metrics_endpoint_disabled_404(client, monkeypatch):
    monkeypatch.setattr(
        metrics_route, "get_settings",
        lambda: SimpleNamespace(metrics_enabled=False),
    )
    resp = await client.get("/metrics")
    assert resp.status_code == 404


async def test_metrics_endpoint_db_down_degrades(client, monkeypatch):
    """DB 挂掉：进程指标照常输出 + metrics_db_error 标记（可观测不互相拖死）。"""
    async def broken(session, window_hours):
        raise RuntimeError("db down")

    monkeypatch.setattr(metrics_route, "_db_aggregate_lines", broken)
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "metrics_db_error 1" in resp.text
    assert "http_requests_total" in resp.text


# ---- DB 聚合口径（事务内插行，回滚隔离） ----


async def test_db_aggregate_layers_and_closure(db_session):
    from datetime import datetime, timezone

    from app.models.decision_log import DecisionLog

    now = datetime.now(timezone.utc)
    rows = [
        DecisionLog(route_layer="system_one", escalated_to_agent=False, created_at=now),
        DecisionLog(route_layer="system_one", escalated_to_agent=False, created_at=now),
        DecisionLog(route_layer="system_one", escalated_to_agent=True, created_at=now),
        DecisionLog(route_layer="rule", escalated_to_agent=False, created_at=now),
        DecisionLog(route_layer="agent", escalated_to_agent=True, created_at=now,
                    tool_calls=3),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    lines = await metrics_route._db_aggregate_lines(db_session, window_hours=1)
    joined = "\n".join(lines)
    assert 'router_requests_total{layer="system_one"} 3' in joined
    assert 'router_requests_total{layer="rule"} 1' in joined
    assert 'router_requests_total{layer="agent"} 1' in joined
    assert 'router_requests_total{layer="safety"} 0' in joined
    # S1 层 3 行，2 行未升级 → 闭环率 2/3（与 gate.py escalated_to_agent=false 占比同口径）
    assert "s1_closure_rate 0.6667" in joined
    assert "tool_calls_max 3" in joined
    assert "tool_calls_avg 3.00" in joined
    assert "decision_total 5" in joined
