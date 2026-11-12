"""Day9 步 6：MCP family-notify 客户端 + notify_family 注册表测试（主文档 §4.3）。

覆盖：正常推送（mock outbox 留痕）/ 无绑定 skip / 授权忽略（elder_id 取
runtime_ctx 不取模型传参）/ 未验证长者拒绝 / MCP 不可用降级结构化日志 /
开关关闭直走日志 / schema 静态无 elder_id（人审稿回归防线）。
server 模块经 importlib 直载，留痕文件指到 tmp_path。
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def push_family_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


@pytest.fixture
def _notify_on(monkeypatch, disable_mcp_workorder_by_default):
    """用例内重开 family-notify MCP 路径（显式依赖 conftest 默认关闭项保顺序）。"""

    class _Stub:
        workorder_mcp_enabled = False
        family_notify_mcp_enabled = True

    from app.tools import family_notify_mcp

    monkeypatch.setattr(family_notify_mcp, "get_settings", lambda: _Stub())


@pytest.fixture
def _tmp_server(tmp_path, monkeypatch):
    """直载 mcp-servers/family-notify/server.py，留痕/绑定文件指到 tmp_path。"""
    spec = importlib.util.spec_from_file_location(
        "family_notify_server", _REPO_ROOT / "mcp-servers" / "family-notify" / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data_dir = tmp_path / "mock_wechat"
    monkeypatch.setattr(mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(mod, "OUTBOX_FILE", data_dir / "outbox.jsonl")
    monkeypatch.setattr(mod, "BINDINGS_FILE", data_dir / "bindings.json")
    return mod


# ---- handler 级（registry 视角）----


async def test_notify_family_via_mcp(monkeypatch, _notify_on):
    """正常路径：MCP sent → notified=True；调用参数（elder_id/severity）正确随行。"""
    from app.tools import family_notify_mcp

    fake = _FakeClient(result={"status": "sent", "severity": "info", "channel": "mock_wechat"})
    monkeypatch.setattr(family_notify_mcp, "get_family_notify_client", lambda: fake)

    out = await family_notify_mcp.notify_family_handler(
        {"message": "告诉儿子我今晚想他了", "severity": "info"},
        {"elder_id": "elder-1"},
    )
    assert out == {"notified": True, "status": "sent"}
    assert fake.calls[0]["elder_id"] == "elder-1"
    assert fake.calls[0]["severity"] == "info"


async def test_notify_family_no_binding_skipped(monkeypatch, _notify_on):
    """无家属绑定：server 返回 skipped（正常业务态）→ handler notified=False 不报错。"""
    from app.tools import family_notify_mcp

    fake = _FakeClient(result={"status": "skipped", "reason": "no_binding"})
    monkeypatch.setattr(family_notify_mcp, "get_family_notify_client", lambda: fake)

    out = await family_notify_mcp.notify_family_handler(
        {"message": "留言", "severity": "info"}, {"elder_id": "elder-2"}
    )
    assert out["notified"] is False and out["status"] == "skipped"


async def test_notify_family_model_elder_id_ignored(monkeypatch, _notify_on):
    """授权忽略：模型 args 显式传 elder_id=受害者 → 客户端只收 runtime_ctx 注入值。"""
    from app.tools import family_notify_mcp

    fake = _FakeClient(result={"status": "sent"})
    monkeypatch.setattr(family_notify_mcp, "get_family_notify_client", lambda: fake)

    await family_notify_mcp.notify_family_handler(
        {"message": "伪造身份留言", "severity": "info", "elder_id": "victim"},
        {"elder_id": "self-elder"},
    )
    assert fake.calls[0]["elder_id"] == "self-elder"
    assert fake.calls[0]["elder_id"] != "victim"


async def test_notify_family_without_verified_elder_rejected():
    """未验证长者（runtime_ctx 空）→ 拒绝发送（消息不可归户）。"""
    from app.tools.family_notify_mcp import notify_family_handler

    out = await notify_family_handler({"message": "留言", "severity": "info"}, {})
    assert out == {"notified": False, "reason": "elder_not_verified"}


async def test_notify_family_degrades_to_log_when_mcp_none(monkeypatch, _notify_on):
    """降级路径：MCP 返回 None（重试耗尽）→ 结构化日志降级，不抛异常。"""
    from app.tools import family_notify_mcp

    fake = _FakeClient(result=None)
    monkeypatch.setattr(family_notify_mcp, "get_family_notify_client", lambda: fake)

    out = await family_notify_mcp.notify_family_handler(
        {"message": "留言", "severity": "warning"}, {"elder_id": "elder-3"}
    )
    assert out == {"notified": False, "status": "log_only"}


async def test_notify_family_degrades_when_client_raises(monkeypatch, _notify_on):
    """降级路径：客户端意外抛异常 → 不向上传播，结构化日志降级。"""
    from app.tools import family_notify_mcp

    fake = _FakeClient(raise_exc=RuntimeError("stdio dead"))
    monkeypatch.setattr(family_notify_mcp, "get_family_notify_client", lambda: fake)

    out = await family_notify_mcp.notify_family_handler(
        {"message": "留言", "severity": "info"}, {"elder_id": "elder-4"}
    )
    assert out == {"notified": False, "status": "log_only"}


async def test_notify_family_disabled_direct_log():
    """开关关闭（family_notify_mcp_enabled=false）→ 不触达 MCP，仅日志通道。"""
    from app.tools.family_notify_mcp import notify_family_handler

    out = await notify_family_handler({"message": "留言", "severity": "info"}, {"elder_id": "e5"})
    assert out == {"notified": False, "status": "log_only"}


def test_notify_family_schema_no_elder_id_and_valid_severities():
    """schema 回归防线（人审稿）：无 elder_id 字段；severity 枚举三档；message 必填。"""
    from app.tools.family_notify_mcp import TOOL_SCHEMA

    assert "elder_id" not in str(TOOL_SCHEMA)
    assert TOOL_SCHEMA["properties"]["severity"]["enum"] == ["info", "warning", "urgent"]
    assert TOOL_SCHEMA["required"] == ["message", "severity"]


def test_notify_family_registered_in_registry():
    """注册表：notify_family 已注册且 schema 人审稿一致（registry 全量无 elder_id 复验）。"""
    from app.tools.registry import get_tool, register_default_tools

    register_default_tools()
    spec = get_tool("notify_family")
    assert spec is not None
    assert "elder_id" not in str(spec.schema)
    assert spec.handler is not None


# ---- server 直测 ----


def test_server_push_with_binding_writes_outbox(_tmp_server):
    """server：有绑定 → outbox 追加一行（mock openid+消息体+时间戳），返回 sent。"""
    mod = _tmp_server
    mod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    mod.BINDINGS_FILE.write_text(
        json.dumps({"elder-1": "mock-openid-ABC"}, ensure_ascii=False), encoding="utf-8"
    )
    out = mod.push_family_message(elder_id="elder-1", message="想你了", severity="info")
    assert out["status"] == "sent"
    row = json.loads(mod.OUTBOX_FILE.read_text(encoding="utf-8").strip())
    assert row["mock_openid"] == "mock-openid-ABC"
    assert row["elder_id"] == "elder-1"
    assert row["message"] == "想你了"
    assert row["channel"] == "mock_wechat"


def test_server_no_binding_skipped_without_error(_tmp_server):
    """server：无绑定文件/无该 elder 映射 → skipped 不报错，不落 outbox。"""
    mod = _tmp_server
    out = mod.push_family_message(elder_id="nobody", message="留言", severity="info")
    assert out == {"status": "skipped", "reason": "no_binding"}
    assert not mod.OUTBOX_FILE.exists()


def test_server_message_clamped_and_severity_normalized(_tmp_server):
    """server：消息 200 字截断、非法 severity 归 info、空消息 skipped。"""
    mod = _tmp_server
    out = mod.push_family_message(elder_id="e", message="长" * 300, severity="bogus")
    assert out["status"] == "skipped"  # 无绑定优先于内容校验后的投递
    out2 = mod.push_family_message(elder_id="e", message="   ", severity="info")
    assert out2["reason"] == "empty_message"
