"""Day9 步 5：MCP workorder 客户端 + emergency_report 改造测试（主文档 §4.3）。

覆盖：正常 MCP 路径（返回结构不变）/ 超时降级本地 ORM / 客户端异常降级 /
开关关闭直走 ORM / 授权忽略（meta elder_id 取 runtime_ctx 不取模型传参）/
幂等（server 直测，同 key 同工单号）。server 模块经 importlib 直载（教学 mock，
无网络依赖），留痕文件指到 tmp_path（不污染 data/mock_his）。
"""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- fake client（handler 级测试注入）----


class _FakeClient:
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def create_workorder(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


@pytest.fixture
def _load_server():
    """直载 mcp-servers/workorder/server.py 模块（独立文件，非包）。"""
    spec = importlib.util.spec_from_file_location(
        "workorder_server", _REPO_ROOT / "mcp-servers" / "workorder" / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def _tmp_server(_load_server, tmp_path, monkeypatch):
    mod = _load_server
    data_dir = tmp_path / "mock_his"
    monkeypatch.setattr(mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(mod, "WORKORDERS_FILE", data_dir / "workorders.jsonl")
    monkeypatch.setattr(mod, "_IDEMPOTENCY", {})
    return mod


# ---- handler 级：正常 / 降级 / 授权忽略 ----


@pytest.fixture
def _mcp_on(monkeypatch, disable_mcp_workorder_by_default):
    """用例内重开 MCP 路径（显式依赖 conftest 默认关闭项，保证覆盖顺序在其后）。"""

    class _Stub:
        workorder_mcp_enabled = True

    from app.tools import emergency_report

    monkeypatch.setattr(emergency_report, "get_settings", lambda: _Stub())


def _patch_factory(monkeypatch, db_session):
    """ORM 降级路径测试：handler 的会话工厂指向测试会话（不连开发库）。"""
    from app.tools import emergency_report

    class _FakeFactory:
        def __init__(self, session):
            self._s = session

        def __call__(self):
            return self

        async def __aenter__(self):
            return self._s

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        emergency_report, "get_session_factory", lambda: _FakeFactory(db_session)
    )


async def test_report_via_mcp_returns_same_structure(db_session, seed, monkeypatch, _mcp_on):
    """正常路径：MCP 成功 → 对外返回值结构不变（既有契约）；不落本地 ORM 工单；
    调用链日志字段（workorder_no）由 fake client 捕获断言。"""
    from sqlalchemy import select

    from app.models.workorder import Workorder
    from app.tools import emergency_report

    fake = _FakeClient(result={"workorder_no": "WH-TEST-0001", "status": "created"})
    monkeypatch.setattr(emergency_report, "get_workorder_client", lambda: fake)
    elder_id = str(seed["elders"]["single"].elder_id)

    result = await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "摔倒了"}, {"elder_id": elder_id}
    )
    assert result == {"reported": True, "category": "fall"}  # 对外契约零变化
    assert fake.calls[0]["elder_id"] == elder_id
    assert fake.calls[0]["priority"] == "urgent"
    assert fake.calls[0]["idempotency_key"].startswith(elder_id)
    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert rows == []  # MCP 成功不落本地 ORM（避免双写）


async def test_report_falls_back_when_mcp_unavailable(db_session, seed, monkeypatch, _mcp_on):
    """降级路径：MCP 返回 None（重试耗尽）→ 回退本地 ORM 原逻辑，返回结构不变。"""
    from sqlalchemy import select

    from app.models.workorder import Workorder
    from app.tools import emergency_report

    _patch_factory(monkeypatch, db_session)
    fake = _FakeClient(result=None)
    monkeypatch.setattr(emergency_report, "get_workorder_client", lambda: fake)
    elder_id = str(seed["elders"]["single"].elder_id)

    result = await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "摔倒了"}, {"elder_id": elder_id}
    )
    assert result == {"reported": True, "category": "fall"}
    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1 and str(rows[0].elder_id) == elder_id


async def test_report_falls_back_when_client_raises(db_session, seed, monkeypatch, _mcp_on):
    """降级路径：客户端意外抛异常 → 不向上传播，回退本地 ORM（可见降级）。"""
    from sqlalchemy import select

    from app.models.workorder import Workorder
    from app.tools import emergency_report

    _patch_factory(monkeypatch, db_session)
    fake = _FakeClient(raise_exc=RuntimeError("stdio dead"))
    monkeypatch.setattr(emergency_report, "get_workorder_client", lambda: fake)
    elder_id = str(seed["elders"]["single"].elder_id)

    result = await emergency_report.report_emergency_handler(
        {"category": "other", "description": "x"}, {"elder_id": elder_id}
    )
    assert result == {"reported": True, "category": "other"}
    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_report_direct_orm_when_disabled(db_session, seed, monkeypatch):
    """开关关闭（workorder_mcp_enabled=false）→ 不经 MCP，直走本地 ORM（降级测试口径）。"""
    from sqlalchemy import select

    from app.models.workorder import Workorder
    from app.tools import emergency_report

    _patch_factory(monkeypatch, db_session)
    fake = _FakeClient(result={"workorder_no": "SHOULD-NOT-CALL"})
    monkeypatch.setattr(emergency_report, "get_workorder_client", lambda: fake)
    elder_id = str(seed["elders"]["single"].elder_id)

    result = await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "摔倒了"}, {"elder_id": elder_id}
    )
    assert result == {"reported": True, "category": "fall"}
    assert fake.calls == []  # 未触达 MCP
    rows = (
        (await db_session.execute(select(Workorder).where(Workorder.type == "sensitive_alert")))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_report_model_elder_id_ignored_on_mcp_path(db_session, seed, monkeypatch, _mcp_on):
    """授权忽略：模型 args 显式传 elder_id=受害者 → 客户端 meta 只带 runtime_ctx 注入值。"""
    from app.tools import emergency_report

    fake = _FakeClient(result={"workorder_no": "WH-TEST-0002"})
    monkeypatch.setattr(emergency_report, "get_workorder_client", lambda: fake)
    self_elder = str(seed["elders"]["single"].elder_id)
    victim_elder = str(seed["elders"]["other"].elder_id)

    await emergency_report.report_emergency_handler(
        {"category": "fall", "description": "伪造受害者工单", "elder_id": victim_elder},
        {"elder_id": self_elder},
    )
    assert fake.calls[0]["elder_id"] == self_elder
    assert fake.calls[0]["elder_id"] != victim_elder


# ---- server 直测：幂等 / 留痕 / 入参归一 ----


async def test_server_idempotency_same_key_same_workorder_no(_tmp_server):
    """幂等：同 idempotency_key 重复调用返回同工单号，留痕只落一行。"""
    mod = _tmp_server
    first = mod.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        idempotency_key="K1", meta={"elder_id": "e1"},
    )
    second = mod.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        idempotency_key="K1", meta={"elder_id": "e1"},
    )
    assert first["workorder_no"] == second["workorder_no"]
    assert first["idempotent"] is False and second["idempotent"] is True
    lines = mod.WORKORDERS_FILE.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["elder_id"] == "e1"  # 归属记录在留痕中


async def test_server_records_elder_from_meta_and_normalizes(_tmp_server):
    """入参归一 + 归属记录：非法类目归 other、非法优先级归 normal；
    elder_id 只来自客户端 meta（server 不信任其内容，仅记录）。"""
    mod = _tmp_server
    out = mod.create_workorder(
        category="not_a_category", title="x" * 300, description="y" * 600,
        priority="bogus", meta={"elder_id": str(uuid4())},
    )
    assert out["status"] == "created"
    row = json.loads(mod.WORKORDERS_FILE.read_text(encoding="utf-8").strip())
    assert row["category"] == "other"
    assert row["priority"] == "normal"
    assert len(row["title"]) <= 200 and len(row["description"]) <= 500
    assert "elder_id" in row


async def test_client_retry_exhausted_returns_none(monkeypatch):
    """客户端：会话持续失败 → 重试 2 次后返回 None（调用方降级），不抛异常。"""
    from app.tools import workorder_mcp

    client = workorder_mcp.WorkorderMcpClient(command=["python", "-c", "pass"])

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("spawn failed")

        async def __aexit__(self, *args):
            return False

    async def _fake_enter(self, cm):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(
        "mcp.client.stdio.stdio_client", lambda params: _Boom(), raising=False
    )
    # stdio_client 由模块内 import，monkeypatch 源头
    out = await client.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e", idempotency_key="K",
    )
    assert out is None
    await client.close()


def test_build_idempotency_key_deterministic():
    """幂等键确定性：同（主体,类目,描述）同键；描述/主体不同则键不同。"""
    from app.tools.workorder_mcp import build_idempotency_key

    a = build_idempotency_key("e1", "fall", "摔倒了")
    b = build_idempotency_key("e1", "fall", "摔倒了")
    c = build_idempotency_key("e2", "fall", "摔倒了")
    d = build_idempotency_key("e1", "fall", "滑倒了")
    assert a == b and a != c and a != d


async def test_client_sends_meta_and_no_elder_id_in_business_args(monkeypatch):
    """客户端契约：elder_id 只经 meta 附加（业务参数无 elder_id），idempotency_key 随行。"""
    from app.tools import workorder_mcp

    captured: dict = {}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            captured["name"] = name
            captured["arguments"] = arguments

            class _R:
                isError = False
                content = [type("C", (), {"text": json.dumps({"workorder_no": "WH-X"})})()]

            return _R()

    client = workorder_mcp.WorkorderMcpClient(command=["python", "-c", "pass"])
    session = _FakeSession()

    async def _fake_ensure():
        return session

    monkeypatch.setattr(client, "_ensure_session", _fake_ensure)
    out = await client.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e-ctx", idempotency_key="K9",
    )
    assert out == {"workorder_no": "WH-X"}
    args = captured["arguments"]
    assert "elder_id" not in args or args.get("meta", {}).get("elder_id") == "e-ctx"
    assert args["idempotency_key"] == "K9"
    await client.close()
