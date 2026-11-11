"""Day7 步9：20 场景评测基线 runner（大纲 §4.4，bench_router 的端到端扩展）。

用法：uv run python scripts/bench_day7.py

- 连 kangban_test（严禁 dev 库），会话级 alembic upgrade head，
  每场景独立事务整体回滚 + 场景内自建长者种子（与 tests/conftest 同模式），零脏数据；
- 离线可复算口径：S1 用确定性关键词桩（真实 laya 口径见 day6_router_baseline.md），
  LLM 用确定性桩（冲突裁判把候选全部报矛盾）、embedding 用词轴语义桩；
  --real-embedding 时改用真实 embedding（需 .env 配置，缺失即退出，不静默降级），
  报告如实标注口径；
- 场景集：eval/fixtures/scene20.json（七类 29 场景：Day7 六类 20 + Day8 健康分级 9，
  断言在 fixture 内）；
- 指标（8 项，全部可复算）：任务成功率 / P95 延迟 / S1 闭环率 / 分层命中率 /
  单会话 token 成本（调用次数口径）/ 敏感召回 / 工具调用准确率 / 人工接管率；
- 产出：eval/reports/v1_baseline.md + v1_metrics.json（gate.py 消费）+
  .cache/bench_day7/raw.jsonl（不入 Git）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

FIXTURE = Path("eval/fixtures/scene20.json")
REPORT_DIR = Path("eval/reports")
RAW_DIR = Path(".cache/bench_day7")
# 直跑 `python scripts/bench_day7.py` 时 sys.path[0]=scripts/，补项目根以导入 scripts.import_media
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---- 测试库守卫（与 tests/conftest.py 同口径，严禁 dev 库）----


def _test_db_url() -> str:
    from app.core.config import get_settings

    url = get_settings().database_url_test
    if not url:
        raise SystemExit("未配置 DATABASE_URL_TEST，无法执行评测")
    if url == get_settings().database_url or "kangban_dev?" in url or url.endswith("kangban_dev"):
        raise SystemExit("DATABASE_URL_TEST 不得指向 kangban_dev：评测严禁写入开发库")
    return url


def _upgrade_head() -> None:
    env = {**os.environ, "DATABASE_URL": _test_db_url()}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"kangban_test 迁移失败：\n{result.stdout}\n{result.stderr}")


# ---- 离线桩：词轴语义 embedding / 确定性 S1 / 确定性冲突 LLM ----

# 词轴语义桩：同轴词共享同一向量块 → 余弦可判语义近邻（维度 1024 对齐）。
# 轴只覆盖 fixture 语义域（饮食忌口/戏曲/家人/睡眠），其余字符哈希低权重兜底。
_AXES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("糖尿病", "血糖", "甜", "糖", "忌口", "月饼", "粽子", "饮食")),
    (32, ("越剧", "京剧", "戏曲", "梁祝", "听戏", "唱")),
    (64, ("孙子", "孙女", "儿子", "女儿", "家人", "深圳", "春节")),
    (96, ("睡", "失眠", "午休", "觉")),
)
_AXIS_DIMS = 32
_CHAR_DIMS = 896  # 128..1023：非轴字符哈希（低权重，不淹没轴信号）


def _stub_vector(text: str) -> list[float]:
    vec = [0.0] * 1024
    for base, words in _AXES:
        hit = sum(text.count(w) for w in words)
        if hit:
            for i in range(base, base + _AXIS_DIMS):
                vec[i] += float(hit)
    for ch in set(text):
        vec[128 + zlib.crc32(ch.encode("utf-8")) % _CHAR_DIMS] += 0.15  # crc32：跨进程可复算
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else vec


class _Counters:
    external_embedding = 0
    stub_embedding = 0
    stub_llm = 0


def _install_embedding_stub() -> None:
    from app.integrations import embedding as emb

    async def fake_embed_text(text, *, transport=None):
        del transport
        _Counters.stub_embedding += 1
        return _stub_vector(text)

    async def fake_embed_texts(texts, *, transport=None):
        del transport
        _Counters.stub_embedding += len(texts)
        return [_stub_vector(t) for t in texts]

    emb.embed_text = fake_embed_text
    emb.embed_texts = fake_embed_texts


def _install_real_embedding_counter() -> None:
    """真实 embedding 包一层计数（成本口径），未配置即退出，不静默降级。"""
    from app.core.config import get_settings
    from app.integrations import embedding as emb

    s = get_settings()
    if not (s.embedding_api_key and s.embedding_base_url):
        raise SystemExit("--real-embedding 需要 .env 配置 embedding_api_key/base_url")

    async def counting(coro_fn, text, *, transport=None):
        _Counters.external_embedding += 1
        return await coro_fn(text, transport=transport)

    orig_text, orig_texts = emb.embed_text, emb.embed_texts
    emb.embed_text = lambda t, *, transport=None: counting(orig_text, t, transport=transport)
    emb.embed_texts = lambda ts, *, transport=None: _count_texts(
        orig_texts, ts, transport=transport
    )


async def _count_texts(orig, texts, *, transport=None):
    _Counters.external_embedding += len(texts)
    return await orig(texts, transport=transport)


def _install_conflict_llm_stub() -> None:
    """确定性冲突裁判：把 user prompt 里列出的旧记忆全部报为与新记忆(0)矛盾。"""
    from app.integrations import llm as llm_mod

    async def fake_chat_with_tools(messages, tools, *, transport=None):
        del tools, transport
        _Counters.stub_llm += 1
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        old_ids = [
            line.strip()[2:].split(":", 1)[0].strip()
            for line in user.splitlines()
            if line.strip().startswith("- ") and ":" in line
        ]
        return {
            "content": "",
            "tool_calls": [
                {
                    "name": "report_memory_conflicts",
                    "arguments": {
                        "conflicts": [{"old_id": oid, "new_index": 0} for oid in old_ids]
                    },
                }
            ],
        }

    llm_mod.chat_with_tools = fake_chat_with_tools


class _StubS1:
    """确定性 System One 桩：关键词五分类（置信 0.9，不澄清不域外）。"""

    model_version = "stub-s1"

    async def classify(self, text: str):
        from app.system_one.questions import IntentLabel, SystemOneResult

        rules = (
            (IntentLabel.WEATHER, ("天气", "下雨", "冷不冷", "出门", "温度")),
            (IntentLabel.MEDICATION, ("吃药", "服药", "用药", "吃过药")),
            (IntentLabel.OPERA, ("越剧", "京剧", "戏曲", "听戏")),
            (IntentLabel.MOVIE, ("电影",)),
            (IntentLabel.HEALTH, ("血压", "血糖", "身体")),
        )
        intent = IntentLabel.CHAT
        for label, words in rules:
            if any(w in text for w in words):
                intent = label
                break
        return SystemOneResult(
            intent=intent,
            is_sensitive=False,  # 契约字段：敏感判定只认 safety.py 词表（方案 A 裁决）
            confidence=0.9,
            needs_clarification=False,
            privacy_restricted=False,
            is_out_of_domain=False,
            model_version=self.model_version,
        )


class _FakeFactory:
    """factory() → async context manager（绑定当前评测事务，可见未提交数据）。"""

    def __init__(self, holder: dict):
        self._holder = holder

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._holder["session"]

    async def __aexit__(self, *args):
        return False


def _notifier(ok: bool = True):
    async def notifier(state) -> bool:
        return ok

    return notifier


# ---- 场景执行器：返回 (primary outcome|None, evidence dict) ----


async def _seed_elders(session) -> tuple[UUID, UUID]:
    """场景内自建两长者（事务回滚即清）：self=被评长者，other=越权对照。"""
    from app.models import Elder, Persona, Room

    room_number = f"E{int(time.time() * 1000) % 100000:05d}"
    room = Room(building_code="E", floor=9, room_number=room_number, bed_count=2)
    persona = Persona(code=f"bench-{time.time_ns()}", name="小伴", is_active=True)
    session.add_all([room, persona])
    await session.flush()
    e1 = Elder(
        name="评长者甲", persona_id=persona.persona_id,
        room_id=room.room_id, bed_no=1, status="active",
    )
    e2 = Elder(
        name="评长者乙", persona_id=persona.persona_id,
        room_id=room.room_id, bed_no=2, status="active",
    )
    session.add_all([e1, e2])
    await session.flush()
    return e1.elder_id, e2.elder_id


async def _seed_memories(session, elder_id, specs):
    from app.models.enums import MemoryCategory
    from app.models.memory import MemoryChunk

    rows = []
    for spec in specs:
        row = MemoryChunk(
            elder_id=elder_id,
            content=spec["content"],
            embedding=_stub_vector(spec["content"]),
            category=MemoryCategory(spec["category"]),
            importance=spec.get("importance", 3),
        )
        if "age_days" in spec:
            row.created_at = datetime.now(UTC) - timedelta(days=spec["age_days"])
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def _run_sensitive(session, router, sc, ctx):  # noqa: ARG001
    outcome = await router.decide(sc["input"])
    return outcome, {
        "reply": outcome.reply_text,
        "workorder": outcome.create_workorder,
        "tool_calls": outcome.tool_calls,
    }


async def _run_routing(session, router, sc, ctx):  # noqa: ARG001
    outcome = await router.decide(sc["input"])
    return outcome, {
        "reply": outcome.reply_text,
        "ws_event_type": (outcome.ws_events or [{}])[0].get("type"),
        "tool_calls": outcome.tool_calls,
        "escalated": outcome.escalated_to_agent,
    }


async def _run_memory_recall(session, router, sc, ctx):  # noqa: ARG001
    from app.services import memory as ms

    params = sc["params"]
    await _seed_memories(session, ctx["elder_self"], params["seed_memories"])
    mode = params.get("recall_as", "self")
    target = (
        None if mode == "anonymous"
        else ctx["elder_other"] if mode == "other_elder"
        else ctx["elder_self"]
    )
    chunks = await ms.recall_memories(
        session, elder_id=target, query_text=params["query"], top_k=5
    )
    return None, {
        "recall_top_contains": chunks[0].content if chunks else "",
        "recall_empty": len(chunks) == 0,
        "recall_count": len(chunks),
    }


async def _run_memory_conflict(session, router, sc, ctx):  # noqa: ARG001
    from sqlalchemy import select

    from app.models.enums import MemoryStatus
    from app.services import memory as ms

    params = sc["params"]
    elder_id = ctx["elder_self"]

    if "old" in params:  # 冲突检测：旧记忆 → 新事实写入 → supersede
        old_rows = await _seed_memories(session, elder_id, [params["old"]])
        new_items = [{
            "content": params["new"]["content"],
            "category": params["new"]["category"],
            "visibility": "private",
            "importance": params["new"]["importance"],
        }]
        new_vectors = [_stub_vector(params["new"]["content"])]
        superseded = await ms.detect_and_supersede(
            session, elder_id=elder_id, new_items=new_items, new_vectors=new_vectors
        )
        await ms.store_memories(session, elder_id=elder_id, items=new_items, vectors=new_vectors)
        await session.flush()
        new_row = (
            await session.execute(
                select(ms.MemoryChunk).where(
                    ms.MemoryChunk.elder_id == elder_id,
                    ms.MemoryChunk.content == params["new"]["content"],
                )
            )
        ).scalars().one()
        return None, {
            "old_superseded": old_rows[0].status == MemoryStatus.SUPERSEDED,
            "new_active": new_row.status == MemoryStatus.ACTIVE,
            "conflict_llm_called": _Counters.stub_llm > 0,
            "superseded_count": superseded,
        }

    # 周归并：重复簇归并 + 冷记忆衰减隐藏
    await _seed_memories(session, elder_id, params["seed_memories"])
    stats = await ms.weekly_merge(session, elder_id=elder_id)
    active = (
        await session.execute(
            select(ms.MemoryChunk).where(
                ms.MemoryChunk.elder_id == elder_id,
                ms.MemoryChunk.status == MemoryStatus.ACTIVE,
            )
        )
    ).scalars().all()
    return None, {
        "merge_superseded": stats["superseded_duplicates"],
        "merge_suppressed": stats["suppressed_cold"],
        "active_count": len(active),
    }


async def _make_plan_and_start(session, ctx, params, notifier, ck_path):
    from app.graphs.medication import start_reminder
    from app.models import MedicationPlan
    from app.models.enums import MedicationEventSource, MedicationPlanStatus

    elder = ctx["elder_obj"]
    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name=params["drug_name"],
        dose=params["dose"],
        schedule={"slots": params["slots"]},
        start_date=datetime.now(UTC).date(),
        status=MedicationPlanStatus.ACTIVE,
    )
    session.add(plan)
    await session.flush()
    scheduled_at = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    event = await start_reminder(
        session,
        plan=plan,
        elder_name=elder.name,
        slot=params["slot"],
        scheduled_at=scheduled_at,
        source=MedicationEventSource.SCHEDULER,
        notifier=notifier,
        checkpoint_path=str(ck_path),
    )
    return plan, event


async def _decide_with_bridge(router, ctx, session, text) -> object:
    from app.router.cascade import RuntimeBridge

    events: list = []
    token = ctx["bridge"](ctx["elder_self"], session, events)
    try:
        return await router.decide(text)
    finally:
        RuntimeBridge.reset(token)


async def _run_medication(session, router, sc, ctx):
    from sqlalchemy import select

    from app.graphs.medication import get_graph_state, recall, resume_with_reply, start_reminder
    from app.models import MedicationPlan, Workorder
    from app.models.enums import MedicationEventStatus

    params = sc["params"]
    ck_path = RAW_DIR / f"{sc['id']}.sqlite"
    elder = ctx["elder_obj"]

    if sc["id"] == "med-01":
        _, event = await _make_plan_and_start(session, ctx, params, _notifier(True), ck_path)
        plan = await session.get(MedicationPlan, event.plan_id)
        from app.models.enums import MedicationEventSource

        again = await start_reminder(
            session, plan=plan, elder_name=elder.name, slot=params["slot"],
            scheduled_at=event.scheduled_at, source=MedicationEventSource.SCHEDULER,
            notifier=_notifier(True), checkpoint_path=str(ck_path),
        )
        state = await get_graph_state(event.thread_id, checkpoint_path=str(ck_path))
        return None, {
            "event_status": event.status,
            "state_node": (state or {}).get("current_node"),
            "idempotent": again.event_id == event.event_id,
        }

    if sc["id"] == "med-02":
        _, event = await _make_plan_and_start(session, ctx, params, _notifier(True), ck_path)
        outcome = await _decide_with_bridge(router, ctx, session, params["then_input"])
        return outcome, {
            "event_status": event.status,
            "tool_calls": outcome.tool_calls,
            "reply": outcome.reply_text,
        }

    if sc["id"] == "med-03":
        _, event = await _make_plan_and_start(session, ctx, params, _notifier(True), ck_path)
        await resume_with_reply(
            session, event=event, reply=params["reply"], elder_name=elder.name,
            drug_name=params["drug_name"], dose=params["dose"], slot=params["slot"],
            notifier=_notifier(True), checkpoint_path=str(ck_path),
        )
        return None, {"retry_count": event.retry_count, "event_status": event.status}

    if sc["id"] == "med-04":
        _, event = await _make_plan_and_start(session, ctx, params, _notifier(False), ck_path)
        for _ in range(params.get("offline_recall_times", 3)):
            if event.status == MedicationEventStatus.ESCALATED:
                break
            await recall(
                session, event=event, elder_name=elder.name,
                drug_name=params["drug_name"], dose=params["dose"], slot=params["slot"],
                notifier=_notifier(False), checkpoint_path=str(ck_path),
            )
        orders = (
            await session.execute(select(Workorder).where(Workorder.elder_id == ctx["elder_self"]))
        ).scalars().all()
        return None, {
            "event_status": event.status,
            "retry_count": event.retry_count,
            "workorder_created": any(o.category == "medication" for o in orders),
        }

    if sc["id"] == "med-05":
        await _make_plan_and_start(session, ctx, params, _notifier(True), ck_path)
        outcome = await _decide_with_bridge(router, ctx, session, params["then_input"])
        return outcome, {
            "tool_calls": outcome.tool_calls,
            "reply": outcome.reply_text,
        }

    raise SystemExit(f"未知用药场景 {sc['id']}")


async def _run_media(session, router, sc, ctx):
    from sqlalchemy import select

    from app.models import MediaHistory
    from scripts.import_media import import_samples

    params = sc["params"]
    elder_id = ctx["elder_self"]
    await import_samples(session)
    if params.get("with_preference"):
        await _seed_memories(
            session, elder_id,
            [{"content": "喜欢听越剧，年轻时最爱梁祝", "category": "preference", "importance": 3}],
        )

    events: list = []
    token = None if params.get("no_bridge") else ctx["bridge"](elder_id, session, events)
    try:
        if params.get("input") == "换一段":
            await router.decide("我想听一段越剧")  # 前置：先完成一次正常点播
            outcome = await router.decide("换一段")
        else:
            outcome = await router.decide(params["input"])
    finally:
        if token is not None:
            ctx["bridge_reset"](token)

    history = (
        await session.execute(select(MediaHistory).where(MediaHistory.elder_id == elder_id))
    ).scalars().all()
    ev = {
        "tool_calls": 0,
        "ws_event_type": events[0]["type"] if events else None,
        "history_count": len(history),
        "reply": outcome.reply_text,
    }
    if history:
        ev["history_source"] = history[0].source
    if params.get("input") == "换一段":
        ev["media_id_changed"] = len(events) == 2 and events[0]["media_id"] != events[1]["media_id"]
    return outcome, ev


async def _run_health(session, router, sc, ctx):  # noqa: ARG001
    """Day8 步10：健康分级场景（绿/黄/红各 3 例，主诉触发 3 例）。

    断言四件事：分级结论、工单副作用、图挂起站点、确认前零通知（铁律 4）。
    """
    from sqlalchemy import select

    from app.graphs.health import get_graph_state, run_triage, thread_id_for
    from app.models import HealthRecord, Workorder

    params = sc["params"]
    ck_path = RAW_DIR / f"{sc['id']}.sqlite"
    elder = ctx["elder_obj"]
    values = dict(params["record"])
    if values.get("mood") is not None:
        values["mood"] = str(values["mood"])  # 口径：mood 入库存 1–5 整数字符串
    record = HealthRecord(
        elder_id=elder.elder_id,
        recorded_at=datetime.now(UTC),
        source="checkin",
        **values,
    )
    session.add(record)
    await session.flush()

    notified: list[str] = []

    async def notifier(state):
        notified.append(state.get("notify_kind"))
        return True

    await run_triage(
        session, record=record, elder_name=elder.name,
        notifier=notifier, checkpoint_path=str(ck_path),
    )
    orders = (
        await session.execute(
            select(Workorder).where(
                Workorder.elder_id == elder.elder_id, Workorder.type == "health_alert"
            )
        )
    ).scalars().all()
    pending_site = None
    if record.triage_level != "green":
        state = await get_graph_state(
            thread_id_for(elder.elder_id, record.record_id), checkpoint_path=str(ck_path)
        )
        nxt = (state or {}).get("__next__") or ()
        pending_site = nxt[0] if nxt else None
    return None, {
        "triage_level": record.triage_level,
        "triage_reason": record.triage_reason,
        "workorder_created": len(orders) > 0,
        "pending_site": pending_site,
        "notified_before_confirm": len(notified) > 0,
    }


EXECUTORS = {
    "sensitive": _run_sensitive,
    "routing": _run_routing,
    "memory_recall": _run_memory_recall,
    "memory_conflict": _run_memory_conflict,
    "medication": _run_medication,
    "media": _run_media,
    "health": _run_health,
}

# 断言失败 → 分层归因（ASR/Laya(S1)/召回/LLM/Tool/编排；离线口径 ASR 不涉及）
ATTRIBUTION = {
    "route_layer": "编排",
    "intent": "Laya(S1)",
    "tool_calls_max": "Tool",
    "tool_calls_min": "Tool",
    "workorder": "编排",
    "reply_contains": "编排",
    "ws_event_type": "编排",
    "escalated": "编排",
    "recall_top_contains": "召回",
    "recall_count": "召回",
    "recall_empty": "召回",
    "old_superseded": "LLM",
    "new_active": "编排",
    "conflict_llm_called": "LLM",
    "superseded_count": "LLM",
    "merge_superseded": "编排",
    "merge_suppressed": "编排",
    "active_count": "编排",
    "event_status": "编排",
    "state_node": "编排",
    "idempotent": "编排",
    "retry_count": "编排",
    "workorder_created": "Tool",
    "media_id_changed": "编排",
    "history_count": "Tool",
    "history_source": "Tool",
    "triage_level": "编排",
    "pending_site": "编排",
    "notified_before_confirm": "编排",
}


def _check(expect: dict, got: dict) -> list[str]:
    failed = []
    for key, want in expect.items():
        if key == "reply_contains":
            wants = want if isinstance(want, list) else [want]
            if not all(w in (got.get("reply") or "") for w in wants):
                failed.append(key)
        elif key == "recall_top_contains":  # top-1 内容包含关键词即命中
            if want not in (got.get("recall_top_contains") or ""):
                failed.append(key)
        elif key == "tool_calls_max":
            if got.get("tool_calls", 0) > want:
                failed.append(key)
        elif key == "tool_calls_min":
            if got.get("tool_calls", 0) < want:
                failed.append(key)
        elif key == "recall_min":
            if got.get("recall_count", 0) < want:
                failed.append(key)
        elif got.get(key) != want:
            failed.append(key)
    return failed


def pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def _route_ok(row: dict) -> bool:
    """路由正确性（分层命中率口径）：层命中 + 意图命中（无意图断言的只看层）。"""
    if row["got_layer"] != row["expect"].get("route_layer"):
        return False
    want_intent = row["expect"].get("intent")
    return want_intent is None or row["got_intent"] == want_intent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-embedding", action="store_true",
                        help="用真实 embedding（需 .env 配置，缺失即退出）")
    args = parser.parse_args()
    if args.real_embedding:
        _install_real_embedding_counter()
    else:
        _install_embedding_stub()
    _install_conflict_llm_stub()

    scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
    _upgrade_head()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.router.cascade import CascadeOutcome, CascadeRouter, _register_default_handlers
    from app.tools import medication_confirm

    engine = create_async_engine(_test_db_url(), poolclass=NullPool)
    results: list[dict] = []
    try:
        for sc in scenarios:
            async with engine.connect() as conn:
                trans = await conn.begin()
                factory = async_sessionmaker(
                    bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
                )
                async with factory() as session:
                    self_id, other_id = await _seed_elders(session)
                    from app.models import Elder

                    elder_obj = await session.get(Elder, self_id)
                    ctx = {
                        "elder_obj": elder_obj,
                        "elder_self": self_id,
                        "elder_other": other_id,
                        "bridge": _make_bridge,
                        "bridge_reset": None,  # 下面赋值（避免定义前引用）
                    }
                    from app.router.cascade import RuntimeBridge

                    ctx["bridge_reset"] = RuntimeBridge.reset

                    router = CascadeRouter()
                    _register_default_handlers(router)
                    router.register_handler(
                        "weather_fast_path",
                        CascadeOutcome(reply_text="(bench stub)", ws_events=[], tool_calls=1),
                    )
                    router.attach_system_one(_StubS1())

                    holder = {"session": session}
                    saved_factory = medication_confirm.get_session_factory
                    medication_confirm.get_session_factory = lambda: _FakeFactory(holder)  # noqa: B023

                    t0 = time.perf_counter()
                    ev: dict = {}
                    failed: list[str] = []
                    outcome = None
                    try:
                        outcome, ev = await EXECUTORS[sc["category"]](session, router, sc, ctx)
                        if outcome is not None:  # 路由层/意图统一从 outcome 取（ev 只管业务证据）
                            ev.setdefault("route_layer", outcome.route_layer)
                            ev.setdefault("intent", outcome.intent)
                        failed = _check(sc["expect"], ev)
                    except Exception as exc:  # noqa: BLE001 —— 场景异常即失败并归因编排
                        failed = ["exception"]
                        ev = {"reply": f"{type(exc).__name__}: {exc}"}
                    finally:
                        medication_confirm.get_session_factory = saved_factory
                    wall_ms = (time.perf_counter() - t0) * 1000
                await trans.rollback()

            got_layer = ev.get("route_layer") or (outcome.route_layer if outcome else None)
            got_intent = ev.get("intent") or (outcome.intent if outcome else None)
            results.append({
                **sc,
                "ok": not failed,
                "failed_asserts": failed,
                "wall_ms": round(wall_ms, 1),
                "got_layer": got_layer,
                "got_intent": got_intent,
                "evidence": {
                    k: (str(v) if isinstance(v, UUID) else v)
                    for k, v in ev.items() if not k.startswith("_")
                },
            })
            flag = "OK " if not failed else "BAD"
            print(f"[{flag}] {sc['id']} {sc['title'][:22]:<22} {wall_ms:6.0f}ms"
                  + (f" 失败断言: {failed}" if failed else ""))
    finally:
        await engine.dispose()

    _write_report(results)


def _make_bridge(elder_id, session, events):
    from app.router.cascade import RuntimeBridge

    async def send_event(payload):
        events.append(payload)

    return RuntimeBridge.set({
        "elder_id": str(elder_id),
        "db_factory": _FakeFactory({"session": session}),
        "send_event": send_event,
    })


def _write_report(results: list[dict]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    walls = [r["wall_ms"] for r in results]
    decide_rows = [r for r in results if r["got_layer"] is not None]
    closure = sum(1 for r in decide_rows if r["got_layer"] != "agent") / len(decide_rows)
    by_layer = {
        layer: [r for r in decide_rows if r["expect"].get("route_layer") == layer]
        for layer in ("safety", "rule", "system_one", "agent")
    }
    layer_breakdown = {
        layer: {"hit": sum(1 for r in rows if _route_ok(r)), "total": len(rows)}
        for layer, rows in by_layer.items() if rows
    }
    layer_hit_rate = (
        sum(v["hit"] for v in layer_breakdown.values())
        / sum(v["total"] for v in layer_breakdown.values())
    )
    sensitive = [r for r in results if r["category"] == "sensitive"]
    sensitive_recall = sum(1 for r in sensitive if r["ok"]) / len(sensitive)
    tool_rows = [r for r in results if any(k.startswith("tool_calls") for k in r["expect"])]
    tool_acc = sum(1 for r in tool_rows if r["ok"]) / len(tool_rows)
    takeover = sum(1 for r in decide_rows if r["got_layer"] == "agent") / len(decide_rows)
    bad = [r for r in results if not r["ok"]]

    metrics = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture": "eval/fixtures/scene20.json",
        "mode": "real_embedding" if _Counters.external_embedding else "stub_embedding",
        "task_success_rate": round(passed / total, 4),
        "p95_latency_ms": round(pctl(walls, 0.95), 1),
        "s1_closure_rate": round(closure, 4),
        "intent_accuracy": round(layer_hit_rate, 4),
        "layer_hit_rate": round(layer_hit_rate, 4),
        "layer_breakdown": layer_breakdown,
        "sensitive_recall": round(sensitive_recall, 4),
        "tool_accuracy": round(tool_acc, 4),
        "takeover_rate": round(takeover, 4),
        "token_cost": {
            "external_embedding_calls": _Counters.external_embedding,
            "external_llm_calls": 0,
            "stub_embedding_calls": _Counters.stub_embedding,
            "stub_llm_calls": _Counters.stub_llm,
            "note": "离线桩口径：外部 token 成本 0；--real-embedding 时按真实调用次数计入",
        },
        "passed": passed,
        "total": total,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "v1_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with open(RAW_DIR / "raw.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    lines = [
        "== Day7 评测基线 v1（29 场景=20+Day8 健康 9，离线可复算口径） ==",
        f"生成：{metrics['generated_at']}　embedding 模式：{metrics['mode']}",
        f"场景：{passed}/{total} 通过（任务成功率 {metrics['task_success_rate']:.0%}）",
        "",
        "## 8 项指标（阈值与判定见 eval/gate.py）",
        f"- 任务成功率：{passed}/{total} = {metrics['task_success_rate']:.0%}",
        f"- P95 延迟（场景级墙钟，含 DB）：{metrics['p95_latency_ms']}ms",
        f"- S1 闭环率（decide 驱动 {len(decide_rows)} 场景不经主控即完成）："
        f"{metrics['s1_closure_rate']:.0%}",
        f"- 分层命中率（=意图准确率口径）：{metrics['layer_hit_rate']:.0%}（"
        + "，".join(f"{k} {v['hit']}/{v['total']}" for k, v in layer_breakdown.items()) + "）",
        f"- 敏感召回：{sum(1 for r in sensitive if r['ok'])}/{len(sensitive)}"
        f" = {metrics['sensitive_recall']:.0%}（gate ≥95%）",
        f"- 工具调用准确率：{sum(1 for r in tool_rows if r['ok'])}/{len(tool_rows)}"
        f" = {metrics['tool_accuracy']:.0%}",
        f"- 人工接管率（agent 层占比）：{metrics['takeover_rate']:.0%}",
        f"- 单会话 token 成本：外部 embedding 调用 {_Counters.external_embedding} 次、"
        f"外部 LLM 调用 0 次（桩内 embedding {_Counters.stub_embedding} 次 / "
        f"桩 LLM {_Counters.stub_llm} 次）",
        "",
        "## 逐场景结果",
        *(
            f"- [{'OK' if r['ok'] else 'BAD'}] {r['id']} {r['title']}（{r['wall_ms']}ms"
            + (f"，失败：{r['failed_asserts']}" if not r["ok"] else "") + "）"
            for r in results
        ),
        "",
        "## 口径声明（归因前提）",
        "- S1 为确定性关键词桩：本报告意图/闭环指标测的是「路由编排与 handler 接线」；"
        "真实 laya 口径见 day6_router_baseline.md（50 条真实推理）。",
        "- embedding 缺省为词轴语义桩：记忆场景测的是「隔离过滤 + top-k + supersede/归并管线」；"
        "`--real-embedding` 可切真实向量复算。",
        "- ASR 层不在本轮范围（文本直入，语音链路归 Day3 实测）；token 成本为调用次数口径。",
        "",
        "## 失败分层归因表",
    ]
    if not bad:
        lines.append("（无 bad case）")
    for r in bad:
        layers = sorted({ATTRIBUTION.get(k, "编排") for k in r["failed_asserts"]})
        lines.append(
            f"- [{r['id']}] {r['title']}：断言失败 {r['failed_asserts']}"
            f" → 归因 {'/'.join(layers)}｜依据 evidence={r['evidence']}｜回流 fixture：是"
        )
    lines += ["", "## bad case 回流清单"]
    if not bad:
        lines.append("- 无；后续真实 embedding/laya 在线跑批新增 bad case 时回写并补 fixture。")
    else:
        lines += [
            f"- {r['id']}（{r['category']}）：见归因表；复算 scripts/bench_day7.py。"
            for r in bad
        ]

    (REPORT_DIR / "v1_baseline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n报告 → eval/reports/v1_baseline.md；指标 → v1_metrics.json；"
          "逐条 → .cache/bench_day7/raw.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
