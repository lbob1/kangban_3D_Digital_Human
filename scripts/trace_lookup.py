"""trace_lookup：一条命令串一轮对话全链（Day10 步骤 4/5，D2 变更纯本地 trace 方案）。

数据源：PG `decision_logs` + `conversation_messages` + `conversations`（表结构零改动）。
主键口径（用户裁决 2026-11-12）：**thread_id 为主键**——两表均有索引，一轮对话的
「敏感词表→规则层→System One→主控 agent→工具调用→回复」可完整串起；
conversation_messages.seq 保证同轮 user/assistant 顺序。

trace_id 兼容输入（尽力而为）：trace_id 只存在于 stdout JSON 日志（无落盘文件），
本脚本尝试从 `docker logs kangban-backend`（或 --log-file 指定文件）解析该 trace 的
请求完成时间，再取决策表时间窗内的行——窗口命不中时如实提示改用 thread_id。

产出：
- 终端摘要（1 分钟内人读全链）；
- 自包含 HTML 报告（单文件、离线可开、分层时间线+耗时条）存 eval/reports/trace/，
  作为原「Cloud 截图留档」的等价留档物。

用法：
    uv run python scripts/trace_lookup.py <thread_id>
    uv run python scripts/trace_lookup.py --latest            # 最近一条有决策的对话
    uv run python scripts/trace_lookup.py <trace_id> --window 3
    uv run python scripts/trace_lookup.py <thread_id> --no-html
    uv run python scripts/trace_lookup.py <thread_id> --db postgresql+asyncpg://...
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ---- 分层展示口径（与 decision_logs.route_layer 取值一致） ----

LAYER_LABELS = {
    "safety": "①敏感词表",
    "rule": "②规则层",
    "system_one": "③System One",
    "agent": "④主控 Agent",
}
LAYER_COLORS = {
    "safety": "#c0392b",
    "rule": "#2471a3",
    "system_one": "#1e8449",
    "agent": "#7d3c98",
}


@dataclass
class DecisionStep:
    route_layer: str
    intent: str | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    tool_calls: int | None = None
    escalated_to_agent: bool = False
    degraded: bool = False
    sensitive_hit: str | None = None
    input_text: str | None = None
    created_at: datetime | None = None


@dataclass
class MessageStep:
    role: str
    content: str
    seq: int = 0
    created_at: datetime | None = None


@dataclass
class ThreadMeta:
    thread_id: str
    elder_id: str | None = None
    identity_source: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass
class TraceChain:
    meta: ThreadMeta
    decisions: list[DecisionStep] = field(default_factory=list)
    messages: list[MessageStep] = field(default_factory=list)

    @property
    def total_latency_ms(self) -> float:
        return sum(d.latency_ms or 0 for d in self.decisions)

    @property
    def closed_by_s1(self) -> bool:
        return any(
            d.route_layer == "system_one" and not d.escalated_to_agent for d in self.decisions
        )


def _row_to_decision(row) -> DecisionStep:
    return DecisionStep(
        route_layer=row.route_layer,
        intent=row.intent,
        confidence=row.confidence,
        latency_ms=row.latency_ms,
        tool_calls=row.tool_calls,
        escalated_to_agent=bool(row.escalated_to_agent),
        degraded=bool(row.degraded),
        sensitive_hit=row.sensitive_hit,
        input_text=row.input_text,
        created_at=row.created_at,
    )


def _row_to_message(row) -> MessageStep:
    return MessageStep(
        role=row.role, content=row.content, seq=row.seq, created_at=row.created_at
    )


# ---- 纯函数：链路组装与渲染（单测覆盖） ----


def build_text_summary(chain: TraceChain) -> str:
    """终端摘要：按序打印每层决策与耗时，最后给链路结论。"""
    lines = [f"== 对话全链 {chain.meta.thread_id[:8]}… =="]
    if chain.meta.elder_id:
        lines.append(f"elder={chain.meta.elder_id} 身份={chain.meta.identity_source} 状态={chain.meta.status}")
    if not chain.decisions:
        lines.append("（无 decision_logs 记录）")
    for d in chain.decisions:
        label = LAYER_LABELS.get(d.route_layer, d.route_layer)
        parts = [f"[{label}]"]
        if d.intent:
            parts.append(f"intent={d.intent}")
        if d.confidence is not None:
            parts.append(f"conf={d.confidence:.2f}")
        if d.latency_ms is not None:
            parts.append(f"{d.latency_ms:.0f}ms")
        if d.tool_calls:
            parts.append(f"tools={d.tool_calls}")
        if d.degraded:
            parts.append("degraded")
        if d.escalated_to_agent:
            parts.append("→升级")
        lines.append(" ".join(parts))
        if d.sensitive_hit:
            lines.append(f"  敏感命中：{d.sensitive_hit}")
        if d.input_text:
            lines.append(f"  输入：{d.input_text[:60]}")
    for m in chain.messages:
        lines.append(f"[{m.role}] {m.content[:80]}")
    lines.append(
        f"结论：{len(chain.decisions)} 层决策，分层累计 {chain.total_latency_ms:.0f}ms，"
        + ("S1 闭环" if chain.closed_by_s1 else "经主控/其他层闭环")
    )
    return "\n".join(lines)


def render_html_report(chain: TraceChain, generated_at: datetime | None = None) -> str:
    """自包含单文件 HTML：分层时间线（耗时条宽∝latency）+ 决策表 + 对话记录。"""
    generated = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    max_latency = max((d.latency_ms or 0 for d in chain.decisions), default=0) or 1

    timeline_rows: list[str] = []
    for d in chain.decisions:
        color = LAYER_COLORS.get(d.route_layer, "#666")
        width = max(2.0, (d.latency_ms or 0) / max_latency * 100)
        label = LAYER_LABELS.get(d.route_layer, d.route_layer)
        extra = []
        if d.confidence is not None:
            extra.append(f"conf {d.confidence:.2f}")
        if d.tool_calls:
            extra.append(f"tools {d.tool_calls}")
        if d.degraded:
            extra.append("降级")
        if d.escalated_to_agent:
            extra.append("升级")
        meta = " · ".join(extra)
        timeline_rows.append(
            f"<div class='bar-row'><span class='bar-label'>{html.escape(label)}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{width:.1f}%;"
            f"background:{color}'></div></div>"
            f"<span class='bar-value'>{d.latency_ms or 0:.0f}ms"
            f"{' · ' + html.escape(meta) if meta else ''}</span></div>"
        )

    decision_rows: list[str] = []
    for d in chain.decisions:
        decision_rows.append(
            "<tr>"
            f"<td style='color:{LAYER_COLORS.get(d.route_layer, '#666')}'>"
            f"{html.escape(LAYER_LABELS.get(d.route_layer, d.route_layer))}</td>"
            f"<td>{html.escape(d.intent or '-')}</td>"
            f"<td>{'-' if d.confidence is None else f'{d.confidence:.2f}'}</td>"
            f"<td>{'-' if d.latency_ms is None else f'{d.latency_ms:.0f}'}</td>"
            f"<td>{d.tool_calls if d.tool_calls else '-'}</td>"
            f"<td>{'是' if d.escalated_to_agent else '否'}</td>"
            f"<td>{'是' if d.degraded else '否'}</td>"
            f"<td>{html.escape((d.sensitive_hit or '-')[:40])}</td>"
            f"<td>{html.escape((d.input_text or '-')[:60])}</td>"
            "</tr>"
        )

    message_rows: list[str] = []
    for m in chain.messages:
        message_rows.append(
            f"<tr><td class='role-{html.escape(m.role)}'>{html.escape(m.role)}</td>"
            f"<td>{html.escape(m.content)}</td><td>{m.seq}</td></tr>"
        )

    def _section(title: str, body: str) -> str:
        return f"<h2>{html.escape(title)}</h2>\n{body}" if body else ""

    timeline_html = _section("分层时间线（耗时∝条宽）", "\n".join(timeline_rows))
    table_html = _section(
        "决策明细",
        "<table><thead><tr><th>层</th><th>意图</th><th>置信度</th><th>延迟ms</th>"
        "<th>工具数</th><th>升级</th><th>降级</th><th>敏感命中</th><th>输入(截断)</th></tr></thead>"
        f"<tbody>{''.join(decision_rows)}</tbody></table>",
    )
    messages_html = _section(
        "对话记录（seq 保序）",
        "<table><thead><tr><th>角色</th><th>内容</th><th>seq</th></tr></thead>"
        f"<tbody>{''.join(message_rows)}</tbody></table>",
    )

    elder = html.escape(str(chain.meta.elder_id or "匿名/未绑定"))
    conclusion = (
        f"{len(chain.decisions)} 层决策 · 分层累计 {chain.total_latency_ms:.0f}ms · "
        + ("S1 闭环" if chain.closed_by_s1 else "经主控或其他层闭环")
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>trace 报告 {html.escape(chain.meta.thread_id[:8])}</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:24px;max-width:960px;color:#222}}
h1{{font-size:20px;border-bottom:2px solid #1e8449;padding-bottom:8px}}
h2{{font-size:15px;margin-top:28px}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
td,th{{border:1px solid #ddd;padding:4px 8px;text-align:left}}
th{{background:#f4f6f7}}
.bar-row{{display:flex;align-items:center;margin:6px 0;font-size:12px}}
.bar-label{{width:110px;flex:none}}
.bar-track{{flex:1;background:#eef1f2;border-radius:4px;height:14px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px}}
.bar-value{{width:180px;flex:none;padding-left:8px}}
.role-user{{color:#2471a3;font-weight:bold}}.role-assistant{{color:#1e8449;font-weight:bold}}
.meta{{font-size:12px;color:#666}}footer{{margin-top:32px;font-size:11px;color:#999}}
</style></head><body>
<h1>康伴 trace 全链报告 · {html.escape(chain.meta.thread_id)}</h1>
<p class="meta">elder={elder} · 身份源={html.escape(str(chain.meta.identity_source or '-'))}
 · 状态={html.escape(str(chain.meta.status or '-'))} · {html.escape(conclusion)}</p>
{timeline_html}
{table_html}
{messages_html}
<footer>生成于 {generated} · scripts/trace_lookup.py · 数据源 decision_logs + conversation_messages（纯本地方案，D2 变更）</footer>
</body></html>
"""


# ---- DB 访问（查询函数接收连接：测试用事务连接注入，回滚隔离不落库） ----


async def load_chain(conn, thread_id: str) -> TraceChain | None:
    from sqlalchemy import text

    conv = (
        await conn.execute(
            text(
                "SELECT thread_id, elder_id::text AS elder_id, identity_source, "
                "status, started_at, ended_at FROM conversations WHERE thread_id = :t"
            ),
            {"t": thread_id},
        )
    ).first()
    if conv is None:
        return None
    decisions = [
        _row_to_decision(r)
        for r in (
            await conn.execute(
                text(
                    "SELECT route_layer, intent, confidence, latency_ms, tool_calls, "
                    "escalated_to_agent, degraded, sensitive_hit, input_text, created_at "
                    "FROM decision_logs WHERE thread_id = :t ORDER BY created_at"
                ),
                {"t": thread_id},
            )
        ).all()
    ]
    messages = [
        _row_to_message(r)
        for r in (
            await conn.execute(
                text(
                    "SELECT role, content, seq, created_at FROM conversation_messages "
                    "WHERE thread_id = :t ORDER BY seq"
                ),
                {"t": thread_id},
            )
        ).all()
    ]
    return TraceChain(
        meta=ThreadMeta(
            thread_id=str(conv.thread_id),
            elder_id=conv.elder_id,
            identity_source=conv.identity_source,
            status=conv.status,
            started_at=conv.started_at,
            ended_at=conv.ended_at,
        ),
        decisions=decisions,
        messages=messages,
    )


async def load_latest_thread(conn) -> str | None:
    from sqlalchemy import text

    row = (
        await conn.execute(
            text(
                "SELECT thread_id::text FROM decision_logs "
                "WHERE thread_id IS NOT NULL ORDER BY created_at DESC LIMIT 1"
            )
        )
    ).first()
    return str(row[0]) if row else None


async def load_threads_in_window(conn, moment: datetime, window: float) -> list[str]:
    from sqlalchemy import text

    rows = (
        await conn.execute(
            text(
                "SELECT DISTINCT thread_id::text FROM decision_logs "
                "WHERE created_at BETWEEN :lo AND :hi ORDER BY 1"
            ),
            {
                "lo": moment - timedelta(seconds=window),
                "hi": moment + timedelta(seconds=window),
            },
        )
    ).all()
    return [str(r[0]) for r in rows]


async def fetch_chain(db_url: str, thread_id: str) -> TraceChain | None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return await load_chain(conn, thread_id)
    finally:
        await engine.dispose()


async def fetch_latest_thread_id(db_url: str) -> str | None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return await load_latest_thread(conn)
    finally:
        await engine.dispose()


def resolve_trace_id_from_logs(trace_id: str, log_file: str | None) -> datetime | None:
    """从 stdout JSON 日志解析 trace 完成时间（尽力而为；找不到返回 None）。"""
    lines: list[str] = []
    if log_file:
        lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        try:
            proc = subprocess.run(
                ["docker", "logs", "kangban-backend"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            lines = (proc.stdout + proc.stderr).splitlines()
        except (OSError, subprocess.SubprocessError):
            return None
    for line in lines:
        if trace_id not in line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = payload.get("timestamp")
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


async def fetch_threads_in_window(db_url: str, moment: datetime, window: float) -> list[str]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return await load_threads_in_window(conn, moment, window)
    finally:
        await engine.dispose()


def write_html_report(chain: TraceChain, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"trace_{chain.meta.thread_id[:8]}_{stamp}.html"
    path.write_text(render_html_report(chain), encoding="utf-8")
    return path


# ---- CLI ----


async def _run(args: argparse.Namespace) -> int:
    from app.core.config import get_settings

    db_url = args.db or get_settings().database_url

    if args.latest:
        thread_id = await fetch_latest_thread_id(db_url)
        if not thread_id:
            print("decision_logs 为空：先跑一轮对话再查链", file=sys.stderr)
            return 2
    else:
        thread_id = args.key

    chain = await fetch_chain(db_url, thread_id)

    if chain is None and not args.latest:
        # trace_id 尽力而为：日志解析时间窗 → 窗口内 thread
        moment = resolve_trace_id_from_logs(thread_id, args.log_file)
        if moment is None:
            print(
                f"DB 无 thread {thread_id}；且日志解析不到该 trace_id（只支持 stdout JSON 日志）。\n"
                "请改用 thread_id（uv run python scripts/trace_lookup.py --latest 可取最近对话）。",
                file=sys.stderr,
            )
            return 2
        threads = await fetch_threads_in_window(db_url, moment, args.window)
        if not threads:
            print(
                f"日志定位到 {moment.isoformat()}，但 ±{args.window}s 内 decision_logs 无行。\n"
                "请改用 thread_id 或放大 --window。",
                file=sys.stderr,
            )
            return 2
        print(f"trace_id → 时间窗命中 {len(threads)} 个 thread：{', '.join(t[:8] for t in threads)}")
        chain = await fetch_chain(db_url, threads[0])
        if chain is None:  # pragma: no cover —— 窗口与查询竞态，理论不可达
            print("thread 查询失败（竞态）", file=sys.stderr)
            return 2

    print(build_text_summary(chain))
    if not args.no_html:
        report = write_html_report(chain, Path(args.out))
        print(f"\nHTML 报告：{report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="一轮对话全链 trace 查询（thread_id 主键）")
    parser.add_argument("key", nargs="?", help="thread_id（主键）或 trace_id（尽力而为）")
    parser.add_argument("--latest", action="store_true", help="取最近一条有决策的对话")
    parser.add_argument("--window", type=float, default=3.0, help="trace_id 时间窗秒数")
    parser.add_argument("--log-file", default=None, help="替代 docker logs 的本地日志文件")
    parser.add_argument("--out", default="eval/reports/trace", help="HTML 报告输出目录")
    parser.add_argument("--db", default=None, help="覆盖 DATABASE_URL")
    parser.add_argument("--no-html", action="store_true", help="只打印终端摘要")
    args = parser.parse_args()

    if not args.latest and not args.key:
        parser.error("需要 thread_id/trace_id，或使用 --latest")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
