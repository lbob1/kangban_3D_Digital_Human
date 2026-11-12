"""MCP 建工单 Tool（Day9 步 5，大纲 9.3，主文档 §4.3）：教学 mock HIS。

口径（Day9 主文档 §4.3 / D1）：
- 官方 mcp SDK（FastMCP），stdio 传输；独立 pyproject 与主项目依赖隔离；
- tool create_workorder(category, title, description, priority, idempotency_key, meta)
  —— **业务 schema 不含 elder_id**（铁律 4：服务端 Runtime Context 注入，
  AGENTS 禁区 8）；elder_id 由客户端经 meta 字典附加，server 收到后**仅作归属
  记录，不作信任来源**（模型永远够不到本 server，调用链只有 app 侧客户端）；
- mock 落地：追加 data/mock_his/workorders.jsonl（工单号 WH-时间戳-随机尾 + 全字段）
  + stdout 结构化日志（教学口径：真 HIS 需厂商 API，本日只验协议与留痕）；
- 幂等：同 idempotency_key 重复调用返回同工单号（扫描既有留痕行 + 内存缓存）；
- 上下文边界：无状态、不注入对话历史、不返回既有工单内容（MCP 审计表口径）。
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kangban-workorder")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "mock_his"
WORKORDERS_FILE = DATA_DIR / "workorders.jsonl"

# 工单类目白名单（与 app/tools/emergency_report.VALID_CATEGORIES 对齐 + 图内 health_alert）
VALID_CATEGORIES = (
    "acute_symptom",
    "fall",
    "lost",
    "emotional_crisis",
    "emergency_call",
    "abuse",
    "other",
    "general",
    "health_alert",
)
VALID_PRIORITIES = ("low", "normal", "high", "urgent")

_DESC_SANITIZE_MAX = 500
# idempotency 内存缓存（server 生命周期内；进程重启后靠留痕文件扫描兜底）
_IDEMPOTENCY: dict[str, str] = {}


def _load_idempotency_from_file() -> None:
    """启动/调用前扫描既有留痕行，恢复 idempotency 映射（跨重启幂等）。"""
    if not WORKORDERS_FILE.exists():
        return
    for line in WORKORDERS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key, no = row.get("idempotency_key"), row.get("workorder_no")
        if key and no and key not in _IDEMPOTENCY:
            _IDEMPOTENCY[key] = no


def _log(event: str, **fields: Any) -> None:
    """stdout 结构化日志（stdio 传输下 stdout 即 MCP 协议通道——写 stderr）。"""
    payload = {"event": event, "ts": datetime.now().isoformat(), **fields}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


@mcp.tool()
def create_workorder(
    category: str,
    title: str,
    description: str,
    priority: str = "normal",
    idempotency_key: str | None = None,
    meta: dict | None = None,
) -> dict[str, Any]:
    """创建工单（教学 mock HIS）。idempotency_key 相同的重复调用返回同一工单号；
    meta 仅承载客户端附加的归属记录（不作为信任来源），server 不校验其内容。"""
    _load_idempotency_from_file()

    # 入参归一（LLM/客户端输出不信任：类目白名单外归 other，非法优先级归 normal）
    category = category if category in VALID_CATEGORIES else "other"
    priority = priority if priority in VALID_PRIORITIES else "normal"
    title = str(title)[:200]
    description = str(description)[:_DESC_SANITIZE_MAX]

    # 幂等：同 key 返回同工单号（先查内存，未命中再查留痕文件）
    if idempotency_key:
        existing = _IDEMPOTENCY.get(idempotency_key)
        if existing:
            _log("workorder_idempotent_hit", workorder_no=existing)
            return {"workorder_no": existing, "status": "duplicated", "idempotent": True}

    workorder_no = f"WH-{datetime.now():%Y%m%d}-{random.randint(0, 0xFFFF):04X}"
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = workorder_no

    row = {
        "workorder_no": workorder_no,
        "category": category,
        "title": title,
        "description": description,
        "priority": priority,
        "idempotency_key": idempotency_key,
        # 归属记录（仅记录）：elder_id 由客户端在 meta 中附加，server 不信任不校验
        "elder_id": (meta or {}).get("elder_id"),
        "created_at": datetime.now().isoformat(),
        "source": "mcp_workorder_mock_his",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with WORKORDERS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _log("workorder_created", workorder_no=workorder_no, category=category, priority=priority)
    return {"workorder_no": workorder_no, "status": "created", "idempotent": False}


def main() -> None:
    _log("workorder_server_started", transport="stdio")
    mcp.run()  # stdio 传输（默认）


if __name__ == "__main__":
    main()
