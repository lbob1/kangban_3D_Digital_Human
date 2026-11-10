"""用药确认图（大纲 7.2 / US-01/02 / POC-06，Day7 步6，LangGraph 第一次落地）。

状态机（Day_plan/Day7_3D康伴数字人.md §4.2，节点/边逐字对齐）：

    [定时] APScheduler ──┐
    [对话] S1/主控 Tool ──┴→ calling（发起外呼：WS 下行 medication_reminder；离线→标记）
                                │
                          waiting_confirm（等待长者答复）
                                │
                「我吃过药了」 │        │「没吃」/ 超时 / 离线
                              ▼        ▼
                          confirmed   retry（循环边 ≤3 次）→ 回 calling
                              │        │ 3 次仍无应答
                        medication_events▼
                        status=confirmed   escalated → 创建漏服工单
                              │            （workorders category=medication）→ END
                            END

- thread_id = med-{plan_id}-{date}-{slot}：同一计划同一时段幂等复入（start_reminder 查重）；
- 定时与对话共用一图（大纲红线）：scheduler 触发 start/recall，对话确认走 reply，
  主控 Tool medication_confirm 亦复入本图，不另写一套；
- 外呼口径（D1）：calling 节点经 notifier 回调发 WS 下行事件 medication_reminder
  （与大纲「反向 WebRTC」的字面差异及理由在步 10 文档回填写实标注）；
  设备离线（notifier 返回 False）→ retry_count+1 → END（由调度器退避后 recall 再入图）；
- 失败四分类（大纲 Vibe 点）：离线=临时错→退避重试（retry 循环）；重试耗尽=不可达
  → escalated 落工单（人工兜底）；本图不处理参数错/越权（入口已在服务层校验）。

依赖注入约定：DB session / notifier 回调经 RunnableConfig.configurable 传入节点
（不进图状态——state 会被 msgpack 序列化进 checkpoint，回调对象不可序列化）。
DB 同步约定：medication_events.status 随图节点流转在节点内同步（flush 不 commit），
调用方（scheduler / 路由 / Tool）负责最终 commit。
"""

import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.graphs.checkpointer import close_checkpointer, open_checkpointer
from app.models import MedicationEvent, MedicationPlan, Workorder
from app.models.enums import (
    MedicationEventSource,
    MedicationEventStatus,
    WorkorderPriority,
    WorkorderStatus,
)

logger = get_logger(__name__)

MAX_RETRY = 3  # 事件 CHECK retry_count BETWEEN 0 AND 3；1 次首呼 + 至多 3 次重试

# 长者回复归一化判定词（确定性红线：零模型零 token）
_CONFIRM_WORDS = ("吃了", "吃过了", "确认", "吃过", "好的已吃")
_DECLINE_WORDS = ("没吃", "还没", "不吃", "不想吃")


class MedicationGraphState(TypedDict, total=False):
    """图状态：retry_count / current_node / reply 全部进 state（§4.2 硬要求）。"""

    event_id: str
    plan_id: str
    elder_id: str
    elder_name: str
    drug_name: str
    dose: str
    slot: str
    retry_count: int
    current_node: str
    reply: str | None  # 长者原话；分类结果由 classify_reply 确定性判定
    notified: bool
    workorder_id: str | None


def thread_id_for(plan_id: uuid.UUID, scheduled_at: datetime, slot: str) -> str:
    """图 checkpointer thread（med-{plan_id}-{date}-{slot}）。"""
    day = scheduled_at.astimezone(UTC).date() if scheduled_at.tzinfo else scheduled_at.date()
    return f"med-{plan_id}-{day.isoformat()}-{slot}"


def classify_reply(reply: str) -> str:
    """长者答复三分类（确定性规则，零模型零 token）。"""
    text = (reply or "").strip()
    if any(w in text for w in _CONFIRM_WORDS):
        return "confirm"
    if any(w in text for w in _DECLINE_WORDS):
        return "decline"
    return "timeout"  # 听不懂/答非所问/静默/离线 → 视同未确认


def _deps(config: RunnableConfig) -> tuple[AsyncSession, Any]:
    configurable = config.get("configurable") or {}
    return configurable["session"], configurable["notifier"]


async def _get_event(session: AsyncSession, event_id: str) -> MedicationEvent:
    event = (
        await session.execute(
            select(MedicationEvent).where(MedicationEvent.event_id == uuid.UUID(event_id))
        )
    ).scalar_one()
    return event


# ---- 节点 ----


async def calling_node(state: MedicationGraphState, config: RunnableConfig) -> dict[str, Any]:
    """发起外呼：notifier 下行播报；成功→等待确认，失败→retry 节点标记。"""
    session, notifier = _deps(config)
    event = await _get_event(session, state["event_id"])
    event.status = MedicationEventStatus.CALLING
    await session.flush()

    ok = bool(await notifier(state))
    return {
        "current_node": "calling",
        "notified": ok,
        "reply": None if ok else "__offline__",
    }


async def waiting_confirm_node(
    state: MedicationGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """等待长者答复：落库后本轮调用结束（下一次 resume 由对话/超时驱动）。"""
    session, _ = _deps(config)
    event = await _get_event(session, state["event_id"])
    event.status = MedicationEventStatus.WAITING_CONFIRM
    await session.flush()
    return {"current_node": "waiting_confirm", "status": MedicationEventStatus.WAITING_CONFIRM}


async def evaluate_node(state: MedicationGraphState, config: RunnableConfig) -> dict[str, Any]:
    """答复评估：确定性分类，不改状态（分支交给条件边）。"""
    return {"current_node": "evaluate", "reply": state.get("reply")}


async def retry_node(state: MedicationGraphState, config: RunnableConfig) -> dict[str, Any]:
    """重试计数 +1 并同步事件行；后续走向（循环回 calling / 耗尽升级）由条件边决定。"""
    session, _ = _deps(config)
    event = await _get_event(session, state["event_id"])
    event.retry_count = min(event.retry_count + 1, MAX_RETRY)
    await session.flush()
    return {
        "retry_count": event.retry_count,
        "current_node": "retry",
        "reply": state.get("reply"),
    }


async def confirmed_node(state: MedicationGraphState, config: RunnableConfig) -> dict[str, Any]:
    session, _ = _deps(config)
    event = await _get_event(session, state["event_id"])
    event.status = MedicationEventStatus.CONFIRMED
    event.confirmed_at = datetime.now(UTC)
    await session.flush()
    logger.info(
        "medication confirmed",
        extra={"event": "medication_confirmed", "event_id": state["event_id"]},
    )
    return {"current_node": "confirmed", "status": MedicationEventStatus.CONFIRMED}


async def escalated_node(state: MedicationGraphState, config: RunnableConfig) -> dict[str, Any]:
    """3 次仍无应答/离线 → 漏服工单（category=medication，复用批 5/6 弹窗流转，D6）。"""
    session, _ = _deps(config)
    event = await _get_event(session, state["event_id"])
    workorder = Workorder(
        elder_id=event.elder_id,
        type="medication_missed",
        category="medication",
        status=WorkorderStatus.OPEN,
        priority=WorkorderPriority.HIGH,
        title=f"漏服提醒：{state['elder_name']} {state['drug_name']}（{state['slot']}）",
        description=(
            f"用药确认 3 次无应答（thread={event.thread_id}，plan={event.plan_id}），"
            "请人工上门核实服药情况。"
        ),
    )
    session.add(workorder)
    await session.flush()
    event.status = MedicationEventStatus.ESCALATED
    event.workorder_id = workorder.workorder_id
    await session.flush()
    logger.warning(
        "medication escalated to workorder",
        extra={
            "event": "medication_escalated",
            "event_id": state["event_id"],
            "workorder_id": str(workorder.workorder_id),
        },
    )
    return {
        "current_node": "escalated",
        "status": MedicationEventStatus.ESCALATED,
        "workorder_id": str(workorder.workorder_id),
    }


# ---- 条件边 ----


def _route_entry(state: MedicationGraphState) -> str:
    """入口路由：等待确认中的答复复入→evaluate；start/recall（外呼类）→calling。"""
    if state.get("reply") is not None and state.get("current_node") == "waiting_confirm":
        return "evaluate"
    return "calling"


def _route_after_calling(state: MedicationGraphState) -> str:
    return "waiting_confirm" if state.get("notified") else "retry"


def _route_after_evaluate(state: MedicationGraphState) -> str:
    reply_kind = classify_reply(state.get("reply") or "")
    if reply_kind == "confirm":
        return "confirmed"
    if state.get("retry_count", 0) >= MAX_RETRY:
        return "escalated"
    return "retry"


def _route_after_retry(state: MedicationGraphState) -> str:
    if state.get("retry_count", 0) >= MAX_RETRY:
        return "escalated"
    if classify_reply(state.get("reply") or "") == "decline":
        # 对话内「没吃」→ 立即循环回 calling 再次提醒（循环边 retry→calling）
        return "calling"
    # 超时/离线 → 本轮 END，由调度器退避后 recall 再入图（退避在图外，§4.2）
    return END


def _build_workflow() -> StateGraph:
    """构建状态图（节点/边显式化；session/notifier 经 configurable 依赖注入）。"""
    workflow = StateGraph(MedicationGraphState)
    workflow.add_node("calling", calling_node)
    workflow.add_node("waiting_confirm", waiting_confirm_node)
    workflow.add_node("evaluate", evaluate_node)
    workflow.add_node("retry", retry_node)
    workflow.add_node("confirmed", confirmed_node)
    workflow.add_node("escalated", escalated_node)

    workflow.add_conditional_edges(
        START, _route_entry, {"calling": "calling", "evaluate": "evaluate"}
    )
    workflow.add_conditional_edges(
        "calling", _route_after_calling, {"waiting_confirm": "waiting_confirm", "retry": "retry"}
    )
    workflow.add_edge("waiting_confirm", END)
    workflow.add_conditional_edges(
        "evaluate",
        _route_after_evaluate,
        {"confirmed": "confirmed", "escalated": "escalated", "retry": "retry"},
    )
    workflow.add_conditional_edges(
        "retry", _route_after_retry, {"escalated": "escalated", "calling": "calling", END: END}
    )
    workflow.add_edge("confirmed", END)
    workflow.add_edge("escalated", END)
    return workflow


# ---- 服务入口（定时与对话共用）----


async def _invoke_graph(
    session: AsyncSession,
    notifier: Any,
    thread_id: str,
    input_state: dict[str, Any],
    *,
    checkpoint_path: str | None = None,
) -> None:
    """一次图调用：新开 checkpointer 连接（文件持久化保证跨调用/重启恢复）。"""
    saver = await open_checkpointer(checkpoint_path)
    try:
        graph = _build_workflow().compile(checkpointer=saver)
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id, "session": session, "notifier": notifier}
        }
        await graph.ainvoke(input_state, config)
    finally:
        await close_checkpointer(saver)


async def start_reminder(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    elder_name: str,
    slot: str,
    scheduled_at: datetime,
    source: MedicationEventSource,
    notifier: Any,
    checkpoint_path: str | None = None,
) -> MedicationEvent:
    """触发一次用药提醒（scheduler / 对话汇合共用）：幂等——同 thread 已有事件直接复用。"""
    thread_id = thread_id_for(plan.plan_id, scheduled_at, slot)
    existing = (
        await session.execute(
            select(MedicationEvent).where(MedicationEvent.thread_id == thread_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    event = MedicationEvent(
        plan_id=plan.plan_id,
        elder_id=plan.elder_id,
        scheduled_at=scheduled_at,
        status=MedicationEventStatus.PENDING,
        source=source,
        thread_id=thread_id,
    )
    session.add(event)
    await session.flush()

    await _invoke_graph(
        session,
        notifier,
        thread_id,
        {
            "event_id": str(event.event_id),
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "elder_name": elder_name,
            "drug_name": plan.drug_name,
            "dose": plan.dose,
            "slot": slot,
            "retry_count": 0,
        },
        checkpoint_path=checkpoint_path,
    )
    await session.refresh(event)
    return event


async def resume_with_reply(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    reply: str,
    elder_name: str,
    drug_name: str,
    dose: str,
    slot: str,
    notifier: Any,
    checkpoint_path: str | None = None,
) -> MedicationEvent:
    """复入图（对话答复 / 调度超时）：S1「我吃过药了」与主控 Tool medication_confirm 共用。"""
    if event.thread_id is None:
        raise ValueError("event 缺少 thread_id，无法复入图")
    await _invoke_graph(
        session,
        notifier,
        event.thread_id,
        {
            "event_id": str(event.event_id),
            "plan_id": str(event.plan_id),
            "elder_id": str(event.elder_id),
            "elder_name": elder_name,
            "drug_name": drug_name,
            "dose": dose,
            "slot": slot,
            "reply": reply,
            "current_node": "waiting_confirm",
        },
        checkpoint_path=checkpoint_path,
    )
    await session.refresh(event)
    return event


async def recall(
    session: AsyncSession,
    *,
    event: MedicationEvent,
    elder_name: str,
    drug_name: str,
    dose: str,
    slot: str,
    notifier: Any,
    checkpoint_path: str | None = None,
) -> MedicationEvent:
    """调度器退避后重新外呼（离线/超时重试，无 reply 复入 → calling 节点）。"""
    if event.thread_id is None:
        raise ValueError("event 缺少 thread_id，无法复入图")
    await _invoke_graph(
        session,
        notifier,
        event.thread_id,
        {
            "event_id": str(event.event_id),
            "plan_id": str(event.plan_id),
            "elder_id": str(event.elder_id),
            "elder_name": elder_name,
            "drug_name": drug_name,
            "dose": dose,
            "slot": slot,
            "current_node": "retry",
        },
        checkpoint_path=checkpoint_path,
    )
    await session.refresh(event)
    return event


async def get_graph_state(
    thread_id: str, *, checkpoint_path: str | None = None
) -> dict[str, Any] | None:
    """graph.get_state 逐档可查（验收硬项）：服务重启后恢复断点视图。"""
    saver = await open_checkpointer(checkpoint_path)
    try:
        graph = _build_workflow().compile(checkpointer=saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if snapshot is None or not snapshot.values:
            return None
        values = dict(snapshot.values)
        values["__next__"] = tuple(snapshot.next)
        return values
    finally:
        await close_checkpointer(saver)
