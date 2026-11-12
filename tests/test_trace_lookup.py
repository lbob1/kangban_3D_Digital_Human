"""trace_lookup 单测（Day10 步骤 4/5）：链路组装 / HTML 报告 / 日志解析 / DB 串链。"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "trace_lookup", Path(__file__).resolve().parent.parent / "scripts" / "trace_lookup.py"
)
tl = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("trace_lookup", tl)
_SPEC.loader.exec_module(tl)


def _make_chain() -> tl.TraceChain:
    now = datetime(2026, 11, 12, 8, 0, 0, tzinfo=timezone.utc)
    return tl.TraceChain(
        meta=tl.ThreadMeta(
            thread_id="1f0e8d5c-0000-4000-8000-000000000001",
            elder_id="0a0e8d5c-0000-4000-8000-0000000000aa",
            identity_source="voiceprint",
            status="ended",
            started_at=now,
        ),
        decisions=[
            tl.DecisionStep(
                route_layer="safety", sensitive_hit="自伤词",
                input_text="<script>alert(1)</script>", created_at=now,
            ),
            tl.DecisionStep(
                route_layer="rule", intent="play_music",
                confidence=0.92, latency_ms=12.0, created_at=now,
            ),
            tl.DecisionStep(
                route_layer="system_one", intent="chat",
                confidence=0.88, latency_ms=180.0, created_at=now,
            ),
        ],
        messages=[
            tl.MessageStep(role="user", content="放一首越剧", seq=1, created_at=now),
            tl.MessageStep(role="assistant", content="好的，这就为您播放", seq=2, created_at=now),
        ],
    )


# ---- 纯函数 ----


def test_summary_layers_in_order_and_closure():
    text = tl.build_text_summary(_make_chain())
    assert "[①敏感词表]" in text and "敏感命中：自伤词" in text
    assert "[②规则层]" in text and "intent=play_music" in text
    assert "[③System One]" in text and "180ms" in text
    assert "[user] 放一首越剧" in text
    assert "S1 闭环" in text
    assert "192ms" in text  # 12+180 分层累计


def test_summary_empty_decisions():
    chain = tl.TraceChain(meta=tl.ThreadMeta(thread_id="x"))
    assert "无 decision_logs 记录" in tl.build_text_summary(chain)


def test_html_report_self_contained_and_escaped():
    chain = _make_chain()
    html_text = tl.render_html_report(chain)
    assert html_text.startswith("<!DOCTYPE html>")
    assert html_text.rstrip().endswith("</html>")
    assert "bar-fill" in html_text and "trace_lookup.py" in html_footer(html_text)
    # XSS 输入被转义
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text
    # 分层颜色映射
    assert "#c0392b" in html_text and "#2471a3" in html_text


def html_footer(html_text: str) -> str:
    return html_text[html_text.index("<footer>"):]


def test_html_report_escalated_not_closed():
    chain = _make_chain()
    chain.decisions = [d for d in chain.decisions if d.route_layer != "system_one"]
    chain.decisions.append(
        tl.DecisionStep(
            route_layer="agent", escalated_to_agent=True, latency_ms=900.0, tool_calls=2
        )
    )
    assert not chain.closed_by_s1
    text = tl.build_text_summary(chain)
    assert "经主控/其他层闭环" in text
    assert "→升级" in text and "tools=2" in text


def test_write_html_report_creates_file(tmp_path):
    path = tl.write_html_report(_make_chain(), tmp_path)
    assert path.exists() and path.suffix == ".html"
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


# ---- 日志解析（trace_id 尽力而为通道） ----


def test_resolve_trace_id_from_log_file(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(
        "\n".join(
            [
                "not json",
                json.dumps(
                    {
                        "timestamp": "2026-11-12T08:01:02.000000+00:00",
                        "event": "request_completed",
                        "trace_id": "abc-123",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    got = tl.resolve_trace_id_from_logs("abc-123", str(log))
    assert got is not None and got.year == 2026 and got.hour == 8


def test_resolve_trace_id_missing_returns_none(tmp_path):
    log = tmp_path / "app.log"
    log.write_text('{"timestamp":"2026-11-12T08:00:00+00:00","trace_id":"other"}', encoding="utf-8")
    assert tl.resolve_trace_id_from_logs("abc-123", str(log)) is None


# ---- DB 串链（事务连接注入，回滚隔离） ----


async def test_load_latest_thread_skips_null(db_connection):
    """匿名/脱敏场景 decision_logs.thread_id 可为 NULL：--latest 必须跳过。"""
    from sqlalchemy import text

    await db_connection.execute(
        text(
            "INSERT INTO decision_logs (route_layer, escalated_to_agent) "
            "VALUES ('safety', false)"
        )
    )
    assert await tl.load_latest_thread(db_connection) is None


async def test_load_chain_assembles_full_chain(db_connection):
    import uuid

    from sqlalchemy import text

    thread_id = str(uuid.uuid4())
    await db_connection.execute(
        text(
            "INSERT INTO conversations (thread_id, session_seq, identity_source, status) "
            "VALUES (:t, 1, 'voiceprint', 'ended')"
        ),
        {"t": thread_id},
    )
    await db_connection.execute(
        text(
            "INSERT INTO decision_logs (thread_id, route_layer, intent, confidence, "
            "latency_ms, tool_calls, escalated_to_agent, input_text) "
            "VALUES (:t, 'rule', 'play_music', 0.9, 15.0, 0, false, '放越剧'), "
            "(:t, 'agent', 'chat', NULL, 700.0, 2, true, '聊聊天')"
        ),
        {"t": thread_id},
    )
    await db_connection.execute(
        text(
            "INSERT INTO conversation_messages (thread_id, role, content) "
            "VALUES (:t, 'user', '放越剧'), (:t, 'assistant', '好的')"
        ),
        {"t": thread_id},
    )

    chain = await tl.load_chain(db_connection, thread_id)
    assert chain is not None
    assert chain.meta.elder_id is None
    assert [d.route_layer for d in chain.decisions] == ["rule", "agent"]
    assert chain.decisions[1].tool_calls == 2
    assert [m.role for m in chain.messages] == ["user", "assistant"]
    assert not chain.closed_by_s1
    assert chain.total_latency_ms == 715.0


async def test_load_chain_missing_returns_none(db_connection):
    import uuid

    assert await tl.load_chain(db_connection, str(uuid.uuid4())) is None
