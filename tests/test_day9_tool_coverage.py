"""Day9 步 8：核心工具 handler 行覆盖 100% 补齐测试（大纲 9.4，主文档 §4.5）。

目标：app/tools 全部行覆盖 100%（铁律 10）；本文件补齐 Day9 之前遗留的
分支：MCP 客户端会话建立/复用/isError 重试/单例配置分支、weather LLM 精简
（含异常长保险丝与无标点冲刷）、medication 图复入失败降级与当日计划空档、
play_media 空段打分/media_type 过滤/下行失败吞异常、health_triage 非法与
不存在的 elder_id、family_notify 客户端全路径。全部经 fake 打桩，无网络/子进程。
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeFactory:
    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]


class _FakeSession:
    """最小 fake 会话：execute 返回预置行、get 返回预置对象。"""

    def __init__(self, rows=None, get_map=None):
        self._rows = rows or []
        self._get_map = get_map or {}

    async def execute(self, stmt):
        return _FakeResult(self._rows)

    async def get(self, model, key):
        return self._get_map.get((model.__name__, str(key)))

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def rollback(self):
        return None


# ---- MCP 客户端公共 fake（stdio_client + ClientSession 打桩）----


class _FakeMcpSession:
    """可编程 fake ClientSession：按脚本逐次响应 call_tool。"""

    def __init__(self, script):
        self._script = list(script)  # 每项：dict（成功）| Exception（call_tool 抛出）
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        step = self._script.pop(0) if self._script else RuntimeError("no script")
        if step == "IS_ERROR":
            return SimpleNamespace(isError=True, content=[])
        if isinstance(step, Exception):
            raise step

        class _R:
            isError = False
            content = [SimpleNamespace(text=json.dumps(step))]

        return _R()


class _FakeStdioCm:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        return ("r", "w")

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def fake_mcp(monkeypatch):
    """把 mcp.client.stdio.stdio_client 与 mcp.ClientSession 换成可编程 fake。

    返回 controller：注入 script（call_tool 响应脚本）后，client._ensure_session
    会真实走 _ensure_session 全路径（enter_async_context → ClientSession → initialize）。
    """
    holder: dict = {"stdio": None}

    def _fake_stdio(params):
        return holder["stdio"]

    class _FakeClientSessionCls:
        def __init__(self, read, write):
            holder["stdio"].session = _FakeMcpSession(holder.get("script", []))

        async def __aenter__(self):
            return holder["stdio"].session

        async def __aexit__(self, *args):
            return False

    import mcp
    import mcp.client.stdio

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", _fake_stdio)
    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSessionCls)
    holder["cls"] = _FakeClientSessionCls
    return holder


# ---- weather_mcp 客户端 + LLM 精简 ----


async def test_weather_client_success_reuse_and_close(fake_mcp, monkeypatch):
    """成功路径 + 会话复用（第二次调用不再重建）+ close 收尾。"""
    from app.tools import weather_mcp

    fake_mcp["script"] = [{"temperature": 20, "weather_code": 1}, {"temperature": 21}]
    fake_mcp["stdio"] = _FakeStdioCm()
    client = weather_mcp.WeatherMcpClient(command=["python", "-c", "pass"])
    r1 = await client.get_weather(31.2, 121.4, "today")
    r2 = await client.get_weather(31.2, 121.4, "tomorrow")
    assert r1["temperature"] == 20 and r2["temperature"] == 21
    assert len(fake_mcp["stdio"].session.calls) == 2
    await client.close()


async def test_weather_client_is_error_falls_back_to_mock(fake_mcp):
    """isError/空 content → 重试耗尽 → 本地 mock 兜底（degraded 可见）。"""
    from app.tools import weather_mcp

    fake_mcp["script"] = [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
    fake_mcp["stdio"] = _FakeStdioCm()
    client = weather_mcp.WeatherMcpClient(command=["python", "-c", "pass"])
    out = await client.get_weather(31.2, 121.4, "today")
    assert out["degraded"] is True  # 降级可见（不静默）
    await client.close()


def test_weather_singleton_uses_settings_command(monkeypatch):
    """单例：get_settings().weather_mcp_command 分支（配置命令优先于缺省）。"""
    from app.tools import weather_mcp

    class _Stub:
        weather_mcp_command = "python -c pass"

    monkeypatch.setattr(weather_mcp, "_client", None)
    monkeypatch.setattr(weather_mcp, "get_settings", lambda: _Stub())
    client = weather_mcp.get_weather_client()
    assert client._command == ["python", "-c", "pass"]
    monkeypatch.setattr(weather_mcp, "_client", None)


async def test_weather_condense_llm_full_and_fuse(monkeypatch):
    """受控精简：正常拼接 + 异常长输出保险丝（超 CONDENSE_MAX_CHARS*4 截停）。"""
    from app.tools import weather_mcp

    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: True)

    async def fake_stream(messages, transport=None):
        yield "今天晴，" * 2000  # 远超保险丝阈值

    monkeypatch.setattr(weather_mcp, "stream_chat", fake_stream)
    out = await weather_mcp.condense_via_llm({"elder_name": "王单间", "payload": {}})
    assert out is not None and len(out) <= weather_mcp.CONDENSE_MAX_CHARS


async def test_weather_condense_streaming_sentences_and_flush(monkeypatch):
    """流式精简：句末标点即发射 + 无标点余量冲刷 + 长输出保险丝。"""
    from app.tools import weather_mcp

    monkeypatch.setattr(weather_mcp, "llm_is_configured", lambda: True)
    spoken: list[str] = []

    async def on_sentence(s: str) -> None:
        spoken.append(s)

    # 场景一：正常两句 + 无标点尾段（触发冲刷分支）
    deltas = ["今天天气晴，", "适合外出。", "记得带伞"]

    async def fake_stream(messages):
        for d in deltas:
            yield d

    monkeypatch.setattr(weather_mcp, "stream_chat", fake_stream)
    out = await weather_mcp.condense_via_llm_streaming({}, on_sentence)
    assert out == "今天天气晴，适合外出。记得带伞"
    assert spoken[-1] == "记得带伞"  # 冲刷分支命中

    # 场景二：异常长输出触发保险丝（break 分支）
    spoken.clear()

    async def long_stream(messages):
        for _ in range(5000):
            yield "句子。" * 500

    monkeypatch.setattr(weather_mcp, "stream_chat", long_stream)
    out2 = await weather_mcp.condense_via_llm_streaming({}, on_sentence)
    assert len(out2) <= weather_mcp.CONDENSE_MAX_CHARS * 4 + 2000  # 保险丝后仍可收敛


# ---- workorder_mcp 客户端 ----


async def test_workorder_client_success_and_reuse(fake_mcp):
    """成功路径 + 会话复用（52 行分支：_session 已存在直接返回）。"""
    from app.tools import workorder_mcp

    fake_mcp["script"] = [
        {"workorder_no": "WH-A", "status": "created"},
        {"workorder_no": "WH-A", "status": "duplicated"},
    ]
    fake_mcp["stdio"] = _FakeStdioCm()
    client = workorder_mcp.WorkorderMcpClient(command=["python", "-c", "pass"])
    r1 = await client.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e", idempotency_key="K",
    )
    r2 = await client.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e", idempotency_key="K",
    )
    assert r1["workorder_no"] == r2["workorder_no"]
    await client.close()


async def test_workorder_client_is_error_retries_then_none(fake_mcp):
    """isError → 重试耗尽 → 返回 None（调用方降级），不抛异常。"""
    from app.tools import workorder_mcp

    fake_mcp["script"] = [RuntimeError("x")] * 3
    fake_mcp["stdio"] = _FakeStdioCm()
    client = workorder_mcp.WorkorderMcpClient(command=["python", "-c", "pass"])
    out = await client.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e", idempotency_key="K",
    )
    assert out is None
    await client.close()


def test_workorder_singleton_uses_settings_command(monkeypatch):
    """单例：get_settings().workorder_mcp_command 分支。"""
    from app.tools import workorder_mcp

    class _Stub:
        workorder_mcp_command = "python -c pass"

    monkeypatch.setattr(workorder_mcp, "_client", None)
    monkeypatch.setattr(workorder_mcp, "get_settings", lambda: _Stub())
    client = workorder_mcp.get_workorder_client()
    assert client._command == ["python", "-c", "pass"]
    monkeypatch.setattr(workorder_mcp, "_client", None)


# ---- family_notify_mcp 客户端 ----


async def test_family_client_push_success_reuse_close(fake_mcp):
    """推送成功 + 复用 + close（客户端全路径覆盖）。"""
    from app.tools import family_notify_mcp

    fake_mcp["script"] = [{"status": "sent"}, {"status": "sent"}]
    fake_mcp["stdio"] = _FakeStdioCm()
    client = family_notify_mcp.FamilyNotifyMcpClient(command=["python", "-c", "pass"])
    r1 = await client.push_family_message(elder_id="e", message="想你了", severity="info")
    r2 = await client.push_family_message(elder_id="e", message="在吗", severity="urgent")
    assert r1["status"] == "sent" and r2["status"] == "sent"
    await client.close()


async def test_family_client_retry_exhausted_returns_none(fake_mcp):
    """持续失败 → 重试耗尽 → None（handler 降级结构化日志）。"""
    from app.tools import family_notify_mcp

    fake_mcp["script"] = [RuntimeError("x")] * 3
    fake_mcp["stdio"] = _FakeStdioCm()
    client = family_notify_mcp.FamilyNotifyMcpClient(command=["python", "-c", "pass"])
    out = await client.push_family_message(elder_id="e", message="留言", severity="info")
    assert out is None
    await client.close()


def test_family_singleton_uses_settings_command(monkeypatch):
    """单例：get_settings().family_notify_mcp_command 分支。"""
    from app.tools import family_notify_mcp

    class _Stub:
        family_notify_mcp_command = "python -c pass"

    monkeypatch.setattr(family_notify_mcp, "_client", None)
    monkeypatch.setattr(family_notify_mcp, "get_settings", lambda: _Stub())
    client = family_notify_mcp.get_family_notify_client()
    assert client._command == ["python", "-c", "pass"]
    monkeypatch.setattr(family_notify_mcp, "_client", None)


async def test_notify_family_empty_message_rejected():
    """空留言（whitespace/空串）→ empty_message 拒绝（129 行分支）。"""
    from app.tools.family_notify_mcp import notify_family_handler

    out = await notify_family_handler({"message": "   ", "severity": "info"}, {"elder_id": "e"})
    assert out == {"notified": False, "reason": "empty_message"}


async def test_mcp_clients_is_error_branch(fake_mcp):
    """三个 MCP 客户端的 isError/空 content 分支：weather→mock 兜底、
    workorder/family→重试耗尽返回 None。"""
    from app.tools import family_notify_mcp, weather_mcp, workorder_mcp

    fake_mcp["stdio"] = _FakeStdioCm()

    fake_mcp["script"] = ["IS_ERROR"] * 3
    w = weather_mcp.WeatherMcpClient(command=["python", "-c", "pass"])
    out_w = await w.get_weather(31.2, 121.4, "today")
    assert out_w["degraded"] is True
    await w.close()

    fake_mcp["script"] = ["IS_ERROR"] * 3
    wo = workorder_mcp.WorkorderMcpClient(command=["python", "-c", "pass"])
    assert await wo.create_workorder(
        category="fall", title="t", description="d", priority="urgent",
        elder_id="e", idempotency_key="K",
    ) is None
    await wo.close()

    fake_mcp["script"] = ["IS_ERROR"] * 3
    fn = family_notify_mcp.FamilyNotifyMcpClient(command=["python", "-c", "pass"])
    assert await fn.push_family_message(elder_id="e", message="留言", severity="info") is None
    await fn.close()


# ---- health_triage：非法 / 不存在的 elder_id 分支 ----


async def test_health_triage_invalid_elder_id_string():
    """runtime_ctx elder_id 非法（非 UUID，服务端防御分支）→ elder_not_verified。"""
    from app.tools.health_triage import health_triage_handler

    out = await health_triage_handler({"chief_complaint": "头晕"}, {"elder_id": "not-a-uuid"})
    assert out == {"triaged": False, "reason": "elder_not_verified"}


async def test_health_triage_elder_not_found(db_session, monkeypatch):
    """主体一致但库里无此长者 → elder_not_found（不落记录）。"""
    from app.tools import health_triage

    elder_id = str(uuid4())  # 库中不存在的合法 UUID
    monkeypatch.setattr(health_triage, "get_session_factory", lambda: _FakeFactory(db_session))
    out = await health_triage.health_triage_handler(
        {"chief_complaint": "头晕"}, {"elder_id": elder_id}
    )
    assert out == {"triaged": False, "reason": "elder_not_found"}


# ---- medication_confirm：上下文缺失 / 图复入失败 / 当日计划空档 ----


async def test_load_event_context_missing_plan_or_elder():
    """plan/elder 任一缺失 → None（confirm 据此降级通用回复）。"""
    from app.tools.medication_confirm import _load_event_context

    db = _FakeSession(get_map={})  # 任何 get 都 None
    event = SimpleNamespace(plan_id="p1", elder_id="e1", thread_id=None)
    out = await _load_event_context(db, event)
    assert out is None


async def test_confirm_latest_event_ctx_none_degrades(monkeypatch):
    """事件存在但上下文缺失 → 通用回复（confirmed=False）。"""
    from app.tools import medication_confirm

    eid = str(uuid4())
    event = SimpleNamespace(event_id="ev1", elder_id=eid, status="waiting_confirm")
    db = _FakeSession(rows=[event])
    monkeypatch.setattr(medication_confirm, "get_session_factory", lambda: _FakeFactory(db))

    async def _ctx_none(db, ev):
        return None

    monkeypatch.setattr(medication_confirm, "_load_event_context", _ctx_none)
    out = await medication_confirm.confirm_latest_event(eid)
    assert out == {"confirmed": False, "reply": medication_confirm._GENERIC_NO_EVENT}


async def test_confirm_latest_event_resume_failure_degrades(monkeypatch):
    """图复入抛异常 → 可见降级通用回复（不向上传播）。"""
    from app.tools import medication_confirm

    eid = str(uuid4())
    event = SimpleNamespace(event_id="ev1", elder_id=eid, status="waiting_confirm")
    db = _FakeSession(rows=[event])
    monkeypatch.setattr(medication_confirm, "get_session_factory", lambda: _FakeFactory(db))

    async def _ctx(db, ev):
        return {"elder_name": "王", "drug_name": "降压药", "dose": "5mg", "slot": "早"}

    monkeypatch.setattr(medication_confirm, "_load_event_context", _ctx)

    async def _boom(*args, **kwargs):
        raise RuntimeError("graph dead")

    monkeypatch.setattr(medication_confirm, "resume_with_reply", _boom)
    out = await medication_confirm.confirm_latest_event(eid)
    assert out["confirmed"] is False


async def test_today_plan_reply_expired_and_empty(monkeypatch):
    """当日计划：过期 end_date 跳过（continue 分支）+ 全空 → 空档话术。"""
    from datetime import date, timedelta

    from app.tools import medication_confirm
    from app.tools.medication_confirm import today_plan_reply

    expired = SimpleNamespace(
        drug_name="旧药", dose="1mg", schedule={"slots": [{"label": "早", "time": "08:00"}]},
        end_date=date.today() - timedelta(days=1),
    )
    no_slots = SimpleNamespace(drug_name="新药", dose="2mg", schedule={"slots": []}, end_date=None)
    db = _FakeSession(rows=[expired, no_slots])
    monkeypatch.setattr(medication_confirm, "get_session_factory", lambda: _FakeFactory(db))
    out = await today_plan_reply(str(uuid4()))
    assert "暂没有登记的用药安排" in out  # 空档话术（114/119 分支均命中）


# ---- play_media：空段打分 / media_type 过滤 / 下行失败吞异常 ----


def test_segment_ratio_skips_empty_segments():
    """分隔段归一为空串 → continue（50 行分支），不影响整体打分。"""
    lib = [SimpleNamespace(title="富士山下 - - remix", artist=None, type="music")]
    assert play_media_best("富士山下", lib).title == "富士山下 - - remix"


def play_media_best(query, lib):
    from app.tools import play_media

    return play_media.best_match(query, lib)


async def test_handler_media_type_filter(db_session, seed):
    """media_type=music → SQL 附加 type 过滤（131 行分支），命中后正常播放。"""
    from app.models import MediaLibrary
    from app.models.enums import MediaCopyrightStatus, MediaStatus
    from app.router.cascade import RuntimeBridge
    from app.tools import play_media

    media = MediaLibrary(
        type="music", title="陈奕迅 - 富士山下", artist="陈奕迅",
        media_uri="/static/media/x.mp3", copyright_status=MediaCopyrightStatus.LICENSED,
        status=MediaStatus.ACTIVE,
    )
    db_session.add(media)
    await db_session.flush()

    events: list[dict] = []

    async def send_event(ev):
        events.append(ev)

    token = RuntimeBridge.set(
        {"db_factory": _FakeFactory(db_session), "send_event": send_event}
    )
    try:
        out = await play_media.play_media_handler(
            {"query": "富士山茶", "media_type": "music"},
            {"elder_id": str(seed["elders"]["single"].elder_id)},
        )
    finally:
        RuntimeBridge.reset(token)
    assert out["status"] == "playing" and out["title"] == "陈奕迅 - 富士山下"


async def test_handler_send_event_failure_not_blocking(db_session, seed):
    """WS 下行抛异常 → 吞掉不阻断话术（156-157 行分支），仍返回 playing。"""
    from app.models import MediaLibrary
    from app.models.enums import MediaCopyrightStatus, MediaStatus
    from app.router.cascade import RuntimeBridge
    from app.tools import play_media

    media = MediaLibrary(
        type="music", title="凤凰传奇 - 奢香夫人", artist="凤凰传奇",
        media_uri="/static/media/y.mp3", copyright_status=MediaCopyrightStatus.LICENSED,
        status=MediaStatus.ACTIVE,
    )
    db_session.add(media)
    await db_session.flush()

    async def broken_send(ev):
        raise RuntimeError("ws dead")

    token = RuntimeBridge.set(
        {"db_factory": _FakeFactory(db_session), "send_event": broken_send}
    )
    try:
        out = await play_media.play_media_handler(
            {"query": "奢香夫人"},
            {"elder_id": str(seed["elders"]["single"].elder_id)},
        )
    finally:
        RuntimeBridge.reset(token)
    assert out["status"] == "playing"
