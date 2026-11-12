"""护工看板 workqueue（Day8 步6）：四类工单聚合 + HITL 确认/升级接健康图。

- GET /nurse/workqueue：type∈{medication_missed, sensitive_alert, health_alert,
  help_request} 聚合；nurse 按楼栋隔离（BuildingAssignment→Room→Elder）且排除
  abuse 类目（Day6+ 可见性矩阵）；admin 全量；health_alert 附图挂起站点；
- POST /nurse/workqueue/{id}/resolve：
  - health_alert → Command(resume) 续图（nurse_confirm → yellow_resolve），
    工单由图流转 acknowledged；图已走到医生确认站点时 409；
  - 其余三类 → 0015 审核链 resolve（RESOLVED + review pending（admin 免审
    approved）+ WorkorderAction 留痕），语义与敏感事件端点一致；
  - 幂等：非 open 一律 409；范围外/不可见一律 404 隐藏存在性；
- POST /nurse/workqueue/{id}/escalate：仅 health_alert 且图挂起在 nurse_confirm，
  resume escalate → 既有工单升 urgent 并进医生 interrupt（不重复建单）。

话术/副作用红线：确认（resolve）前零通知；doctor_resolve 节点确认后才放行
紧急通知与 mock 转诊——本路由只驱动图，不自行发通知。
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.graphs.health import (
    graph_pending_site,
    health_thread_id_for_workorder,
    resume_by_workorder,
)
from app.services.notifier import notify as notify_channel  # Day9 步7：双通道通知
from app.models import Elder, Workorder, WorkorderAction
from app.models.enums import WorkorderStatus
from app.schemas.workqueue import (
    QUEUE_TYPES,
    WorkqueueEscalate,
    WorkqueueItem,
    WorkqueueResolve,
)
from app.services.elder import list_elders_for_user

router = APIRouter(prefix="/nurse", tags=["workqueue"])

_ABORT_MARK = "[原因说明]"


def _role_str(actor: UserIdentity) -> str:
    return actor.role.value if hasattr(actor.role, "value") else str(actor.role)


async def _visible_elder_ids(session: AsyncSession, actor: UserIdentity) -> set[uuid.UUID] | None:
    """nurse 楼栋隔离集合；admin 返回 None（不过滤）。"""
    if _role_str(actor) == "admin":
        return None
    elders = await list_elders_for_user(session, actor)
    return {e.elder_id for e in elders}


async def _load_visible_workorder(
    session: AsyncSession, workorder_id: uuid.UUID, actor: UserIdentity
) -> Workorder:
    """取工单并做 workqueue 门禁：类型四类内 + 楼栋范围内 + nurse 排除 abuse。

    任一不满足一律 404（隐藏存在性，与楼栋隔离硬验收一致）。
    """
    row = await session.get(Workorder, workorder_id)
    if row is None or row.type not in QUEUE_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    if _role_str(actor) == "nurse" and (row.category or "general") == "abuse":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    visible = await _visible_elder_ids(session, actor)
    if visible is not None and row.elder_id not in visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    return row


def _require_open(row: Workorder) -> None:
    if row.status != WorkorderStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"工单已流转（status={row.status}），不可重复操作",
        )


async def _item(
    session: AsyncSession, row: Workorder, elder_name: str | None = None
) -> WorkqueueItem:
    """组装响应：health_alert 补图挂起站点（决定前端可执行的操作）。"""
    if elder_name is None:
        elder_name = (
            await session.execute(select(Elder.name).where(Elder.elder_id == row.elder_id))
        ).scalar_one()
    pending_site = None
    if row.type == "health_alert":
        pending_site = await graph_pending_site(health_thread_id_for_workorder(row))
    return WorkqueueItem(
        workorder_id=row.workorder_id,
        elder_id=row.elder_id,
        elder_name=elder_name,
        type=row.type,
        category=row.category or "general",
        status=row.status,
        priority=row.priority,
        title=row.title,
        description=row.description,
        solution=row.solution,
        pending_site=pending_site,
        created_at=row.created_at,
    )


@router.get("/workqueue", response_model=list[WorkqueueItem])
async def list_workqueue(
    event_status: str = Query(default="open", alias="status"),
    limit: int = Query(default=50, le=200),
    actor: UserIdentity = Depends(require_role("nurse", "admin")),
    session: AsyncSession = Depends(get_session),
) -> list[WorkqueueItem]:
    """四类工单聚合（默认 open）：楼栋隔离 + abuse 排除 + 健康图挂起站点。"""
    stmt = (
        select(Workorder, Elder.name)
        .join(Elder, Workorder.elder_id == Elder.elder_id)
        .where(Workorder.type.in_(QUEUE_TYPES))
        .order_by(Workorder.created_at.desc())
        .limit(limit)
    )
    if event_status != "all":
        stmt = stmt.where(Workorder.status == event_status)
    if _role_str(actor) == "nurse":
        stmt = stmt.where(Workorder.category != "abuse")
    visible = await _visible_elder_ids(session, actor)
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(Workorder.elder_id.in_(visible))
    rows = (await session.execute(stmt)).all()
    items: list[WorkqueueItem] = []
    for row, elder_name in rows:
        pending_site = None
        if row.type == "health_alert":
            pending_site = await graph_pending_site(health_thread_id_for_workorder(row))
        items.append(
            WorkqueueItem(
                workorder_id=row.workorder_id,
                elder_id=row.elder_id,
                elder_name=elder_name,
                type=row.type,
                category=row.category or "general",
                status=row.status,
                priority=row.priority,
                title=row.title,
                description=row.description,
                solution=row.solution,
                pending_site=pending_site,
                created_at=row.created_at,
            )
        )
    return items


@router.post("/workqueue/{workorder_id}/resolve", response_model=WorkqueueItem)
async def resolve_workqueue_item(
    workorder_id: uuid.UUID,
    body: WorkqueueResolve,
    actor: UserIdentity = Depends(require_role("nurse", "admin")),
    session: AsyncSession = Depends(get_session),
) -> WorkqueueItem:
    """确认处理：health_alert 续图（acknowledged），其余三类走 0015 审核链。"""
    row = await _load_visible_workorder(session, workorder_id, actor)
    _require_open(row)
    role = _role_str(actor)
    if row.type == "health_alert":
        site = await graph_pending_site(health_thread_id_for_workorder(row))
        if site == "doctor_confirm":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="该告警已升级，待医生确认"
            )
        if site != "nurse_confirm":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="工单未挂起等待护工确认"
            )
        await resume_by_workorder(
            session,
            row,
            action="resolve",
            operator=actor.display_name,
            note=body.note,
            notifier=notify_channel,
        )
        await session.commit()
        await session.refresh(row)
        return await _item(session, row, elder_name=None)

    # 其余三类：0015 审核链（与敏感事件 resolve 同语义）
    row.status = WorkorderStatus.RESOLVED
    row.resolved_at = datetime.now(UTC)
    row.assignee_ref = f"{actor.display_name}({str(actor.account_id)[:8]})"
    if role == "admin":
        row.review_status = "approved"
        row.reviewed_by = actor.display_name
        row.reviewed_at = row.resolved_at
    else:
        row.review_status = "pending"
    row.description = f"{row.description or ''}\n{_ABORT_MARK} {body.note}"[:2000]
    row.solution = body.solution[:2000]
    session.add(
        WorkorderAction(
            workorder_id=row.workorder_id,
            account_id=actor.account_id,
            role=role,
            actor_name=actor.display_name,
            note=body.note,
            solution=body.solution[:2000],
            action="resolve",
        )
    )
    await session.commit()
    await session.refresh(row)
    return await _item(session, row, elder_name=None)


@router.post("/workqueue/{workorder_id}/escalate", response_model=WorkqueueItem)
async def escalate_workqueue_item(
    workorder_id: uuid.UUID,
    body: WorkqueueEscalate,
    actor: UserIdentity = Depends(require_role("nurse", "admin")),
    session: AsyncSession = Depends(get_session),
) -> WorkqueueItem:
    """升级（仅 health_alert 且挂起在护工站点）：resume escalate → 升 urgent 进医生确认。"""
    row = await _load_visible_workorder(session, workorder_id, actor)
    _require_open(row)
    if row.type != "health_alert":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该类工单不支持升级"
        )
    site = await graph_pending_site(health_thread_id_for_workorder(row))
    if site == "doctor_confirm":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="已在医生确认环节，无需升级"
        )
    if site != "nurse_confirm":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="工单未挂起等待护工确认"
        )
    await resume_by_workorder(
        session,
        row,
        action="escalate",
        operator=actor.display_name,
        note=body.note,
        notifier=notify_channel,
    )
    await session.commit()
    await session.refresh(row)
    return await _item(session, row, elder_name=None)
