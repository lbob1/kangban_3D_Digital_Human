"""MCP 家属消息 Tool（Day9 步 6，大纲 9.3「推家属微信消息」，主文档 §4.3）：教学 mock 微信。

口径：
- 官方 mcp SDK（FastMCP），stdio 传输；独立 pyproject 与主项目依赖隔离；
- tool push_family_message(elder_id, message, severity)（主文档 §4.3 明文签名）：
  elder_id 由**客户端（app 侧可信代码）从服务端 Runtime Context 注入**传入——
  主控模型经 registry schema 调用时传不到本字段（schema 禁含 elder_id，禁区 8）；
- mock 家属绑定：查 data/mock_wechat/bindings.json（elder_id → 家属 openid 映射，
  运行时文件不入库）；无绑定返回 skipped 不报错（家属未关联是正常业务态）；
- mock 落地：追加 data/mock_wechat/outbox.jsonl（mock openid、消息体、时间戳）——
  教学口径：真微信需厂商 API，本日只验协议与留痕；
- 上下文边界：消息内容不含病历明细（由客户端侧约束），server 无状态、不返回
  家属资料（只回投递状态），不注入对话历史（MCP 审计表口径）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kangban-family-notify")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "mock_wechat"
OUTBOX_FILE = DATA_DIR / "outbox.jsonl"
BINDINGS_FILE = DATA_DIR / "bindings.json"

VALID_SEVERITIES = ("info", "warning", "urgent")
_MESSAGE_MAX = 200


def _log(event: str, **fields: Any) -> None:
    """结构化日志（stdio 传输下 stdout 即 MCP 协议通道——写 stderr）。"""
    payload = {"event": event, "ts": datetime.now().isoformat(), **fields}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _load_bindings() -> dict[str, str]:
    """elder_id → 家属 mock openid 映射；文件缺失视为无绑定（skipped）。"""
    if not BINDINGS_FILE.exists():
        return {}
    try:
        data = json.loads(BINDINGS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        _log("bindings_file_unparseable", path=str(BINDINGS_FILE))
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


@mcp.tool()
def push_family_message(elder_id: str, message: str, severity: str = "info") -> dict[str, Any]:
    """推送一条消息给长者家属（教学 mock 微信）。
    elder_id 必须由服务端注入；无家属绑定时返回 skipped（正常业务态，不报错）。"""
    severity = severity if severity in VALID_SEVERITIES else "info"
    message = str(message).strip()[:_MESSAGE_MAX]
    if not message:
        return {"status": "skipped", "reason": "empty_message"}

    openid = _load_bindings().get(elder_id)
    if openid is None:
        _log("family_notify_skipped", elder_id=elder_id, reason="no_binding")
        return {"status": "skipped", "reason": "no_binding"}

    row = {
        "mock_openid": openid,  # 教学口径：真微信为厂商 openi d，此处仅留痕映射
        "elder_id": elder_id,
        "message": message,
        "severity": severity,
        "sent_at": datetime.now().isoformat(),
        "channel": "mock_wechat",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUTBOX_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _log("family_message_pushed", elder_id=elder_id, severity=severity, openid=openid[:8])
    return {"status": "sent", "severity": severity, "channel": "mock_wechat"}


def main() -> None:
    _log("family_notify_server_started", transport="stdio")
    mcp.run()  # stdio 传输（默认）


if __name__ == "__main__":
    main()
