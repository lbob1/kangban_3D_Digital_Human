"""/metrics 端点：Prometheus 文本格式（Day10 步骤 3，主文档 §4.2）。

- 进程内指标（请求数/延迟分位/token/幻觉拦截）来自 app.core.metrics；
- DB 聚合指标（分层命中率 / S1 闭环率 / 工具调用分布）按时间窗查 decision_logs，
  口径与 eval/gate.py 同源（s1_closure_rate = system_one 层 escalated_to_agent=false 占比）；
- METRICS_ENABLED=false → 404（主文档 D5 口径）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.metrics import render
from app.db.session import get_session_factory

router = APIRouter()


async def _db_aggregate_lines(session, window_hours: int) -> list[str]:
    """decision_logs 时间窗聚合 → Prometheus 行（无数据时输出 0 值而非缺行，便于告警规则）。

    接收外部 session（路由层用连接工厂开；测试用事务连接传入，回滚隔离不落库）。
    """
    layer_rows = (
        await session.execute(
            text(
                "SELECT route_layer, count(*) AS n FROM decision_logs "
                "WHERE created_at >= now() - make_interval(hours => :h) "
                "GROUP BY route_layer"
            ),
            {"h": window_hours},
        )
    ).all()
    tool_row = (
        await session.execute(
            text(
                "SELECT coalesce(max(tool_calls), 0) AS max_calls, "
                "coalesce(round(avg(tool_calls)::numeric, 2), 0) AS avg_calls "
                "FROM decision_logs "
                "WHERE created_at >= now() - make_interval(hours => :h)"
            ),
            {"h": window_hours},
        )
    ).one()
    closure_row = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE NOT escalated_to_agent) AS closed "
                "FROM decision_logs "
                "WHERE route_layer = 'system_one' "
                "AND created_at >= now() - make_interval(hours => :h)"
            ),
            {"h": window_hours},
        )
    ).one()

    lines: list[str] = []
    lines.append("# HELP router_requests_total Routing layer hits (decision_logs window).")
    lines.append("# TYPE router_requests_total counter")
    known_layers = {"safety", "rule", "system_one", "agent"}
    layer_counts = {row.route_layer: row.n for row in layer_rows}
    for layer in sorted(known_layers | set(layer_counts)):
        lines.append(f'router_requests_total{{layer="{layer}"}} {layer_counts.get(layer, 0)}')

    total = closure_row.total or 0
    closed = closure_row.closed or 0
    rate = (closed / total) if total else 0.0
    lines.append("# HELP s1_closure_rate System-One closure rate (not escalated, gate.py口径).")
    lines.append("# TYPE s1_closure_rate gauge")
    lines.append(f"s1_closure_rate {rate:.4f}")
    lines.append("# HELP decision_total Decision log rows in window.")
    lines.append("# TYPE decision_total gauge")
    lines.append(f"decision_total {sum(layer_counts.values())}")

    lines.append("# HELP tool_calls_max Max tool calls in one decision (window).")
    lines.append("# TYPE tool_calls_max gauge")
    lines.append(f"tool_calls_max {tool_row.max_calls}")
    lines.append("# HELP tool_calls_avg Average tool calls per decision (window).")
    lines.append("# TYPE tool_calls_avg gauge")
    lines.append(f"tool_calls_avg {tool_row.avg_calls}")
    return lines


@router.get("/metrics")
async def metrics(
    window_hours: int = Query(1, ge=1, le=168, description="DB 聚合时间窗（小时）"),
) -> Response:
    settings = get_settings()
    if not settings.metrics_enabled:
        return JSONResponse(status_code=404, content={"detail": "metrics disabled"})
    db_lines: list[str] = []
    try:
        factory = get_session_factory()
        async with factory() as session:
            db_lines = await _db_aggregate_lines(session, window_hours)
    except Exception:  # noqa: BLE001 —— DB 不可用时进程指标照常输出（可观测不互相拖死）
        db_lines = [
            "# HELP metrics_db_error decision_logs aggregation unavailable.",
            "# TYPE metrics_db_error gauge",
            "metrics_db_error 1",
        ]
    return Response(content=render(db_lines), media_type="text/plain; version=0.0.4; charset=utf-8")
