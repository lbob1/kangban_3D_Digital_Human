"""健康决策图（Day8 步2，大纲 8.1）：LangGraph interrupt HITL 正统落地（D3）。

图结构（三分支 + 两处 interrupt）：

    START → triage（分级规则引擎，阈值放代码不放 prompt）
      ├─ green → END（零副作用：无工单、无通知）
      ├─ yellow → yellow_workorder（建单 health_alert/high）
      │             → nurse_confirm（interrupt ①）
      │                 ├─ resolve → yellow_resolve（工单→acknowledged）→ END
      │                 └─ escalate → red_escalate（工单升 urgent）→ doctor_confirm（interrupt ②）
      └─ red → red_workorder（建单 health_alert/urgent）
                    → doctor_confirm（interrupt ②）
                        └─ resolve → doctor_resolve（放行副作用：紧急通知 + 医院 mock 转诊
                                      + 工单流转 acknowledged）→ END

副作用红线（一票否决，Day8 执行提示词铁律 4）：
- interrupt 未恢复前**零通知**——工单先建（status=open，护工看板可见）但不出任何
  WS 下行；确认后 doctor_resolve 才经 notifier 放行（大纲 §4.1 ASCII 图把
  urgent_notify 画在 interrupt 前，与铁律冲突，以铁律为准）；
- 绿色分级零副作用（无工单无通知）——专项断言。

两种 HITL 模式教学对照（D3，面试考点）：
- 本图 interrupt/Command=编排引擎原生挂起恢复（断点存 checkpoint，重启 get_state 可续跑）；
- 用药图（Day7）状态机+HTTP 确认=应用层确认端点驱动状态流转。

依赖注入约定（同 medication.py）：session/notifier 经 RunnableConfig.configurable
传入节点（state 会被 msgpack 序列化进 checkpoint，回调对象不可序列化）。
DB 同步约定：节点内 flush 不 commit，调用方（端点/scheduler/Tool）负责最终 commit。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.graphs.checkpointer import close_checkpointer, open_checkpointer
from app.integrations.hospital import create_referral
from app.models import HealthRecord, Workorder
from app.models.enums import WorkorderPriority, WorkorderStatus
from app.services.health_triage import triage

logger = get_logger(__name__)


class HealthGraphState(TypedDict, total=False):
    """图状态：分级依据/工单号/转诊单号/决策人全部进 state（get_state 可审计）。"""

    record_id: str
    elder_id: str
    elder_name: str
    level: str
    reason: str
    deviation_flag: bool
    workorder_id: str | None
    referral_no: str | None
    notified: bool
    current_node: str
    decision_action: str | None  # resolve / escalate（interrupt 恢复值）
    decision_operator: str | None
    decision_note: str | None


def thread_id_for(elder_id: uuid.UUID, record_id: uuid.UUID) -> str:
    """图 checkpointer thread（health-{elder_id}-{record_id}，同记录幂等复入）。"""
    return f"health-{elder_id}-{record_id}"


def _deps(config: RunnableConfig) -> tuple[AsyncSession, Any]:
    configurable = config.get("configurable") or {}
    return configurable["session"], configurable["notifier"]


async def _get_record(session: AsyncSession, record_id: str) -> HealthRecord:
    return (
        await session.execute(
            select(HealthRecord).where(HealthRecord.record_id == uuid.UUID(record_id))
        )
    ).scalar_one()


async def _get_workorder(session: AsyncSession, workorder_id: str) -> Workorder:
    return (
        await session.execute(
            select(Workorder).where(Workorder.workorder_id == uuid.UUID(workorder_id))
        )
    ).scalar_one()


async def _baseline(
    session: AsyncSession, elder_id: uuid.UUID, *, exclude_record_id: uuid.UUID | None = None
) -> dict[str, float | None]:
    """近 7 天基线均值（误报防护输入）：排除当前记录自身；无历史数据返回全 None。"""
    since = datetime.now(UTC) - timedelta(days=7)
    conditions = [
        HealthRecord.elder_id == elder_id,
        HealthRecord.recorded_at >= since,
    ]
    if exclude_record_id is not None:
        conditions.append(HealthRecord.record_id != exclude_record_id)
    rows = (
        await session.execute(
            select(
                HealthRecord.bp_systolic,
                HealthRecord.bp_diastolic,
                HealthRecord.sleep_minutes,
            ).where(*conditions)
        )
    ).all()

    def _avg(values) -> float | None:
        nums = [float(v) for v in values if v is not None]
        return round(sum(nums) / len(nums), 1) if nums else None

    return {
        "sbp": _avg([r.bp_systolic for r in rows]),
        "dbp": _avg([r.bp_diastolic for r in rows]),
        "sleep": _avg([r.sleep_minutes for r in rows]),
    }


# ---- 节点 ----


async def triage_node(state: HealthGraphState, config: RunnableConfig) -> dict[str, Any]:
    """分级：规则引擎先行（阈值永不进 prompt），结论落库 triage_level/triage_reason。"""
    session, _ = _deps(config)
    record = await _get_record(session, state["record_id"])
    result = triage(
        bp_systolic=record.bp_systolic,
        bp_diastolic=record.bp_diastolic,
        sleep_minutes=record.sleep_minutes,
        mood=record.mood,
        chief_complaint=record.chief_complaint,
        baseline=await _baseline(
            session, record.elder_id, exclude_record_id=record.record_id
        ),
    )
    record.triage_level = result.level
    record.triage_reason = result.reason
    await session.flush()
    logger.info(
        "health triaged",
        extra={
            "event": "health_triaged",
            "record_id": state["record_id"],
            "level": result.level,
            "reason": result.reason,
        },
    )
    return {
        "level": result.level,
        "reason": result.reason,
        "deviation_flag": result.deviation_flag,
        "current_node": "triage",
    }


async def _create_workorder(
    session: AsyncSession,
    state: HealthGraphState,
    *,
    priority: WorkorderPriority,
    title: str,
) -> Workorder:
    """建健康告警工单（status=open：看板可见；确认前零通知，确认后流转 acknowledged）。"""
    workorder = Workorder(
        elder_id=uuid.UUID(state["elder_id"]),
        type="health_alert",
        category="health_alert",
        status=WorkorderStatus.OPEN,
        priority=priority,
        title=title,
        description=f"健康决策图分级 {state['level']}（{state['reason']}），等待 HITL 确认。",
    )
    session.add(workorder)
    await session.flush()
    return workorder


async def yellow_workorder_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    session, _ = _deps(config)
    workorder = await _create_workorder(
        session,
        state,
        priority=WorkorderPriority.HIGH,
        title=f"健康告警待复核：{state['elder_name']}（yellow）",
    )
    return {"workorder_id": str(workorder.workorder_id), "current_node": "yellow_workorder"}


async def red_workorder_node(state: HealthGraphState, config: RunnableConfig) -> dict[str, Any]:
    session, _ = _deps(config)
    workorder = await _create_workorder(
        session,
        state,
        priority=WorkorderPriority.URGENT,
        title=f"健康急警待医生确认：{state['elder_name']}（red）",
    )
    return {"workorder_id": str(workorder.workorder_id), "current_node": "red_workorder"}


async def nurse_confirm_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """interrupt ①：暂停等护工在看板确认；恢复值 {"action": "resolve"|"escalate", ...}。"""
    decision = interrupt(
        {
            "site": "nurse_confirm",
            "elder_name": state["elder_name"],
            "level": state["level"],
            "reason": state["reason"],
            "workorder_id": state.get("workorder_id"),
        }
    )
    return {
        "decision_action": decision.get("action"),
        "decision_operator": decision.get("operator"),
        "decision_note": decision.get("note"),
        "current_node": "nurse_confirm",
    }


async def yellow_resolve_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """护工确认处理：工单流转 acknowledged（后续 resolve 走 0015 审核链，端点层职责）。"""
    session, _ = _deps(config)
    workorder = await _get_workorder(session, state["workorder_id"])
    workorder.status = WorkorderStatus.ACKNOWLEDGED
    await session.flush()
    logger.info(
        "health yellow confirmed",
        extra={
            "event": "health_confirmed",
            "record_id": state["record_id"],
            "action": "resolve",
            "operator": state.get("decision_operator"),
        },
    )
    return {"current_node": "yellow_resolve", "notified": False}


async def red_escalate_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """护工确认升级：既有工单升 urgent（不重复建单），进医生 interrupt。"""
    session, _ = _deps(config)
    workorder = await _get_workorder(session, state["workorder_id"])
    workorder.priority = WorkorderPriority.URGENT
    workorder.title = f"健康急警待医生确认：{state['elder_name']}（yellow 升级）"
    await session.flush()
    return {"current_node": "red_escalate"}


async def doctor_confirm_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """interrupt ②：暂停等医生确认（red 直达 / yellow 升级共用）。"""
    decision = interrupt(
        {
            "site": "doctor_confirm",
            "elder_name": state["elder_name"],
            "level": state["level"],
            "reason": state["reason"],
            "workorder_id": state.get("workorder_id"),
        }
    )
    return {
        "decision_action": decision.get("action"),
        "decision_operator": decision.get("operator"),
        "decision_note": decision.get("note"),
        "current_node": "doctor_confirm",
    }


async def doctor_resolve_node(
    state: HealthGraphState, config: RunnableConfig
) -> dict[str, Any]:
    """医生确认后放行副作用（红线：确认前零通知）：紧急通知 + 医院 mock 转诊 + 流转。"""
    session, notifier = _deps(config)
    notified = bool(await notifier({**state, "notify_kind": "health_urgent"}))
    referral_no = await create_referral(state["elder_name"], state["reason"])
    workorder = await _get_workorder(session, state["workorder_id"])
    workorder.status = WorkorderStatus.ACKNOWLEDGED
    workorder.description = f"{workorder.description}；已确认转诊，转诊单号 {referral_no}"
    await session.flush()
    logger.warning(
        "health red confirmed",
        extra={
            "event": "health_confirmed",
            "record_id": state["record_id"],
            "action": "resolve",
            "operator": state.get("decision_operator"),
            "referral_no": referral_no,
            "notified": notified,
        },
    )
    return {
        "notified": notified,
        "referral_no": referral_no,
        "current_node": "doctor_resolve",
    }


# ---- 条件边 ----


def _route_after_triage(state: HealthGraphState) -> str:
    level = state.get("level")
    if level == "red":
        return "red_workorder"
    if level == "yellow":
        return "yellow_workorder"
    return END  # green：零副作用


def _route_after_nurse(state: HealthGraphState) -> str:
    return "red_escalate" if state.get("decision_action") == "escalate" else "yellow_resolve"


def _build_workflow() -> StateGraph:
    workflow = StateGraph(HealthGraphState)
    workflow.add_node("triage", triage_node)
    workflow.add_node("yellow_workorder", yellow_workorder_node)
    workflow.add_node("red_workorder", red_workorder_node)
    workflow.add_node("nurse_confirm", nurse_confirm_node)
    workflow.add_node("yellow_resolve", yellow_resolve_node)
    workflow.add_node("red_escalate", red_escalate_node)
    workflow.add_node("doctor_confirm", doctor_confirm_node)
    workflow.add_node("doctor_resolve", doctor_resolve_node)

    workflow.add_edge(START, "triage")
    workflow.add_conditional_edges(
        "triage",
        _route_after_triage,
        {"yellow_workorder": "yellow_workorder", "red_workorder": "red_workorder", END: END},
    )
    workflow.add_edge("yellow_workorder", "nurse_confirm")
    workflow.add_conditional_edges(
        "nurse_confirm",
        _route_after_nurse,
        {"yellow_resolve": "yellow_resolve", "red_escalate": "red_escalate"},
    )
    workflow.add_edge("yellow_resolve", END)
    workflow.add_edge("red_escalate", "doctor_confirm")
    workflow.add_edge("red_workorder", "doctor_confirm")
    workflow.add_edge("doctor_confirm", "doctor_resolve")  # 医生 interrupt 仅 resolve 单选
    workflow.add_edge("doctor_resolve", END)
    return workflow


# ---- 服务入口（端点 / scheduler 外呼 / 主控子图三通道汇合共用）----


def _checkpoint_path(checkpoint_path: str | None) -> str:
    """健康图 checkpoint 独立文件（Settings.health_checkpoint_db），不复用用药图。"""
    return checkpoint_path or get_settings().health_checkpoint_db


async def _invoke_graph(
    session: AsyncSession,
    notifier: Any,
    thread_id: str,
    input_state: dict[str, Any] | Command,
    *,
    checkpoint_path: str | None = None,
) -> None:
    saver = await open_checkpointer(_checkpoint_path(checkpoint_path))
    try:
        graph = _build_workflow().compile(checkpointer=saver)
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id, "session": session, "notifier": notifier}
        }
        await graph.ainvoke(input_state, config)
    finally:
        await close_checkpointer(saver)


async def run_triage(
    session: AsyncSession,
    *,
    record: HealthRecord,
    elder_name: str,
    notifier: Any,
    checkpoint_path: str | None = None,
) -> HealthRecord:
    """触发分级（三通道入口：端点写入 / 定时外呼采集 / 主控子图主诉触发）。"""
    thread_id = thread_id_for(record.elder_id, record.record_id)
    await _invoke_graph(
        session,
        notifier,
        thread_id,
        {
            "record_id": str(record.record_id),
            "elder_id": str(record.elder_id),
            "elder_name": elder_name,
        },
        checkpoint_path=checkpoint_path,
    )
    await session.refresh(record)
    return record


async def resume_confirm(
    session: AsyncSession,
    *,
    record: HealthRecord,
    action: str,
    operator: str,
    note: str | None = None,
    notifier: Any,
    checkpoint_path: str | None = None,
) -> HealthRecord:
    """Command(resume) 复入：图从当前 interrupt 断点续跑（nurse_confirm/doctor_confirm 通用）。"""
    if record.elder_id is None or record.record_id is None:
        raise ValueError("record 缺少主键，无法复入健康图")
    thread_id = thread_id_for(record.elder_id, record.record_id)
    await _invoke_graph(
        session,
        notifier,
        thread_id,
        Command(resume={"action": action, "operator": operator, "note": note}),
        checkpoint_path=checkpoint_path,
    )
    await session.refresh(record)
    return record


async def get_graph_state(
    thread_id: str, *, checkpoint_path: str | None = None
) -> dict[str, Any] | None:
    """get_state 逐档可查（验收硬项）：分级依据/工单号/转诊单号/决策人可追溯。"""
    saver = await open_checkpointer(_checkpoint_path(checkpoint_path))
    try:
        graph = _build_workflow().compile(checkpointer=saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if snapshot is None or not snapshot.values:
            return None
        values = dict(snapshot.values)
        values["__next__"] = tuple(snapshot.next)  # 非空=interrupt 挂起中
        return values
    finally:
        await close_checkpointer(saver)
