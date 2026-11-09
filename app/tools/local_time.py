"""本地时间 Tool（Day6 P1-7）：零网络确定性工具，验收项 10 复杂例第二工具。

与天气（weather MCP）组合成 ReAct 多工具例：「外面天气如何，顺便现在几点了」。
"""

from __future__ import annotations

from datetime import datetime


async def local_time_handler(args: dict, runtime_ctx: dict) -> dict:
    del args, runtime_ctx  # 只读公共数据，不使用 elder_id
    now = datetime.now()
    return {
        "time_text": f"{now:%H点%M分}",
        "date_text": f"{now:%m月%d日}",
        "weekday": "一二三四五六日"[now.weekday()],
        "iso": now.isoformat(timespec="seconds"),
    }


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="local_time",
        description="查询当前本地日期与时间（几点、星期几）",
        schema={"type": "object", "properties": {}},  # 无参数；同样不含 elder_id
        handler=local_time_handler,
    )
