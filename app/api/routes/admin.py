"""管理接口：三张授权关联表 + 房间/人设/账号管理（全部仅 admin）。"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_admin, require_role
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import (
    Account,
    BuildingAssignment,
    Conversation,
    ConversationMessage,
    DecisionLog,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    Persona,
    Room,
    Workorder,
)
from app.models.enums import AssignmentStatus, WorkorderStatus
from app.schemas.account_admin import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    ResetPasswordResponse,
)
from app.schemas.assignment import (
    BuildingAssignmentCreate,
    BuildingAssignmentResponse,
    DoctorAssignmentCreate,
    DoctorAssignmentResponse,
    FamilyLinkCreate,
    FamilyLinkResponse,
)
from app.schemas.persona import PersonaCreate, PersonaResponse, PersonaUpdate
from app.schemas.room import RoomCreate, RoomResponse, RoomUpdate
from app.schemas.sensitive_event import SensitiveEventResolve, SensitiveEventResponse
from app.services import account as account_service
from app.services import persona as persona_service
from app.services import room as room_service
from app.services.account import (
    AccountConflict,
    AccountNotFound,
    AccountStateConflict,
)
from app.services.authorization import (
    AssignmentConflict,
    AssignmentValidationError,
    ObjectInactive,
    ObjectNotFound,
    create_building_assignment,
    create_doctor_assignment,
    create_family_link,
    suspend_assignment,
)
from app.services.persona import PersonaConflict, PersonaNotFound
from app.services.room import RoomConflict, RoomNotFound, RoomOccupied, RoomValidationError

router = APIRouter(prefix="/admin/assignments", tags=["admin"])
logger = get_logger(__name__)


def _log_idempotency_key(kind: str, object_key: str, idempotency_key: str | None) -> None:
    if idempotency_key:
        logger.info(
            "assignment idempotency key",
            extra={
                "event": "idempotency_key",
                "object_type": kind,
                "object_id": object_key,
                "idempotency_key": idempotency_key,
            },
        )


def _map_service_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ObjectNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (AssignmentValidationError, ObjectInactive)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    if isinstance(exc, AssignmentConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="内部错误")


# ---- 房间管理 ----

rooms_router = APIRouter(prefix="/admin/rooms", tags=["admin"])


@rooms_router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
async def create_room_route(
    body: RoomCreate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RoomResponse:
    """创建房间（仅管理员）：楼栋+楼层+房号唯一 409。"""
    try:
        room = await room_service.create_room(session, body, actor)
    except RoomConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return RoomResponse.model_validate(room)


@rooms_router.get("", response_model=list[RoomResponse])
async def list_rooms_route(
    _admin=Depends(require_admin),
    building_code: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[RoomResponse]:
    stmt = select(Room).order_by(Room.building_code, Room.floor, Room.room_number)
    if building_code is not None:
        stmt = stmt.where(Room.building_code == building_code)
    rows = (await session.execute(stmt)).scalars().all()
    return [RoomResponse.model_validate(r) for r in rows]


@rooms_router.get("/{room_id}", response_model=RoomResponse)
async def get_room_route(
    room_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RoomResponse:
    try:
        room = await room_service.get_room_or_404(session, room_id)
    except RoomNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return RoomResponse.model_validate(room)


@rooms_router.put("/{room_id}", response_model=RoomResponse)
async def update_room_route(
    room_id: uuid.UUID,
    body: RoomUpdate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RoomResponse:
    """更新房间（仅管理员）：唯一冲突 409、缩容校验 422、占用 409。"""
    try:
        room = await room_service.get_room_or_404(session, room_id)
    except RoomNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        room = await room_service.update_room(session, room, body, actor)
    except RoomValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except RoomConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return RoomResponse.model_validate(room)


@rooms_router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room_route(
    room_id: uuid.UUID,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删房间（仅管理员）：置 inactive；有在住长者/活跃设备 409；幂等。"""
    try:
        room = await room_service.get_room_or_404(session, room_id)
    except RoomNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        await room_service.deactivate_room(session, room, actor)
    except RoomOccupied as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()


# ---- 人设管理 ----

personas_router = APIRouter(prefix="/admin/personas", tags=["admin"])


@personas_router.post("", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED)
async def create_persona_route(
    body: PersonaCreate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PersonaResponse:
    """创建人设（仅管理员）：code 唯一 409。"""
    try:
        persona = await persona_service.create_persona(session, body, actor)
    except PersonaConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return PersonaResponse.model_validate(persona)


@personas_router.get("", response_model=list[PersonaResponse])
async def list_personas_route(
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PersonaResponse]:
    rows = (
        (await session.execute(select(Persona).order_by(Persona.created_at))).scalars().all()
    )
    return [PersonaResponse.model_validate(p) for p in rows]


@personas_router.get("/{persona_id}", response_model=PersonaResponse)
async def get_persona_route(
    persona_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PersonaResponse:
    try:
        persona = await persona_service.get_persona_or_404(session, persona_id)
    except PersonaNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PersonaResponse.model_validate(persona)


@personas_router.put("/{persona_id}", response_model=PersonaResponse)
async def update_persona_route(
    persona_id: uuid.UUID,
    body: PersonaUpdate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PersonaResponse:
    """更新人设（仅管理员）；is_active=false 即软删。"""
    try:
        persona = await persona_service.get_persona_or_404(session, persona_id)
    except PersonaNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    persona = await persona_service.update_persona(session, persona, body, actor)
    await session.commit()
    return PersonaResponse.model_validate(persona)


@personas_router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona_route(
    persona_id: uuid.UUID,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删人设（仅管理员）：is_active=false；幂等。"""
    try:
        persona = await persona_service.get_persona_or_404(session, persona_id)
    except PersonaNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await persona_service.deactivate_persona(session, persona, actor)
    await session.commit()


# ---- 账号管理 ----

accounts_router = APIRouter(prefix="/admin/accounts", tags=["admin"])


@accounts_router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account_route(
    body: AccountCreate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    """建号（仅管理员）：密码经 bcrypt 哈希入库，明文不落日志。"""
    try:
        account = await account_service.create_account(session, body, actor)
    except AccountConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return AccountResponse.model_validate(account)


@accounts_router.get("", response_model=list[AccountResponse])
async def list_accounts_route(
    _admin=Depends(require_admin),
    role: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[AccountResponse]:
    stmt = select(Account).order_by(Account.created_at)
    if role is not None:
        stmt = stmt.where(Account.role == role)
    rows = (await session.execute(stmt)).scalars().all()
    return [AccountResponse.model_validate(a) for a in rows]


@accounts_router.get("/{account_id}", response_model=AccountResponse)
async def get_account_route(
    account_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    try:
        account = await account_service.get_account_or_404(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AccountResponse.model_validate(account)


@accounts_router.put("/{account_id}", response_model=AccountResponse)
async def update_account_route(
    account_id: uuid.UUID,
    body: AccountUpdate,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    """更新账号资料（仅管理员）：display_name/phone/role。"""
    try:
        account = await account_service.get_account_or_404(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        account = await account_service.update_account(session, account, body, actor)
    except AccountConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return AccountResponse.model_validate(account)


@accounts_router.post(
    "/{account_id}/reset-password", response_model=ResetPasswordResponse
)
async def reset_password_route(
    account_id: uuid.UUID,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ResetPasswordResponse:
    """重置密码（仅管理员）：后端生成随机密码，明文仅此一次返回。"""
    try:
        account = await account_service.get_account_or_404(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    new_password = await account_service.reset_password(session, account, actor)
    await session.commit()
    return ResetPasswordResponse(account_id=account.account_id, new_password=new_password)


@accounts_router.post("/{account_id}/disable", response_model=AccountResponse)
async def disable_account_route(
    account_id: uuid.UUID,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    """停用账号（仅管理员）；停用最后一个可用管理员 409。停用后旧 JWT 立即失效。"""
    try:
        account = await account_service.get_account_or_404(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        await account_service.disable_account(session, account, actor)
    except AccountStateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(account)
    return AccountResponse.model_validate(account)


@accounts_router.post("/{account_id}/enable", response_model=AccountResponse)
async def enable_account_route(
    account_id: uuid.UUID,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    """启用账号（仅管理员）。"""
    try:
        account = await account_service.get_account_or_404(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await account_service.enable_account(session, account, actor)
    await session.commit()
    await session.refresh(account)
    return AccountResponse.model_validate(account)


# ---- 三张授权关联表（原有） ----


# ---- 护工-楼栋 ----
@router.post("/buildings", response_model=BuildingAssignmentResponse, status_code=201)
async def create_building(
    body: BuildingAssignmentCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BuildingAssignmentResponse:
    _log_idempotency_key("building_assignment", str(body.account_id), idempotency_key)
    try:
        row = await create_building_assignment(session, body)
    except (ObjectNotFound, AssignmentValidationError, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return BuildingAssignmentResponse.model_validate(row)


@router.get("/buildings", response_model=list[BuildingAssignmentResponse])
async def list_buildings(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    building_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[BuildingAssignmentResponse]:
    stmt = select(BuildingAssignment)
    if account_id is not None:
        stmt = stmt.where(BuildingAssignment.account_id == account_id)
    if building_status is not None:
        stmt = stmt.where(BuildingAssignment.status == building_status)
    rows = (await session.execute(stmt.order_by(BuildingAssignment.created_at))).scalars().all()
    return [BuildingAssignmentResponse.model_validate(r) for r in rows]


@router.delete("/buildings/{assignment_id}", status_code=204)
async def delete_building(
    assignment_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "building", assignment_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()


# ---- 负责医生-长者 ----


@router.post("/doctors", response_model=DoctorAssignmentResponse, status_code=201)
async def create_doctor(
    body: DoctorAssignmentCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DoctorAssignmentResponse:
    _log_idempotency_key(
        "doctor_elder_assignment", f"{body.account_id}:{body.elder_id}", idempotency_key
    )
    try:
        row = await create_doctor_assignment(session, body)
    except (ObjectNotFound, AssignmentValidationError, ObjectInactive, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return DoctorAssignmentResponse.model_validate(row)


@router.get("/doctors", response_model=list[DoctorAssignmentResponse])
async def list_doctors(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    elder_id: uuid.UUID | None = None,
    assignment_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[DoctorAssignmentResponse]:
    stmt = select(DoctorElderAssignment)
    if account_id is not None:
        stmt = stmt.where(DoctorElderAssignment.account_id == account_id)
    if elder_id is not None:
        stmt = stmt.where(DoctorElderAssignment.elder_id == elder_id)
    if assignment_status is not None:
        stmt = stmt.where(DoctorElderAssignment.status == assignment_status)
    rows = (await session.execute(stmt.order_by(DoctorElderAssignment.created_at))).scalars().all()
    return [DoctorAssignmentResponse.model_validate(r) for r in rows]


@router.delete("/doctors/{assignment_id}", status_code=204)
async def delete_doctor(
    assignment_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "doctor", assignment_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()


# ---- 家属-长者 ----


@router.post("/families", response_model=FamilyLinkResponse, status_code=201)
async def create_family(
    body: FamilyLinkCreate,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> FamilyLinkResponse:
    _log_idempotency_key(
        "family_elder_link", f"{body.account_id}:{body.elder_id}", idempotency_key
    )
    try:
        row = await create_family_link(session, body)
    except (ObjectNotFound, AssignmentValidationError, ObjectInactive, AssignmentConflict) as exc:
        raise _map_service_errors(exc) from exc
    await session.commit()
    return FamilyLinkResponse.model_validate(row)


@router.get("/families", response_model=list[FamilyLinkResponse])
async def list_families(
    _admin=Depends(require_admin),
    account_id: uuid.UUID | None = None,
    elder_id: uuid.UUID | None = None,
    link_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[FamilyLinkResponse]:
    stmt = select(FamilyElderLink)
    if account_id is not None:
        stmt = stmt.where(FamilyElderLink.account_id == account_id)
    if elder_id is not None:
        stmt = stmt.where(FamilyElderLink.elder_id == elder_id)
    if link_status is not None:
        stmt = stmt.where(FamilyElderLink.status == link_status)
    rows = (await session.execute(stmt.order_by(FamilyElderLink.created_at))).scalars().all()
    return [FamilyLinkResponse.model_validate(r) for r in rows]


@router.delete("/families/{link_id}", status_code=204)
async def delete_family(
    link_id: uuid.UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    found = await suspend_assignment(session, "family", link_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联不存在")
    await session.commit()


# ---- 敏感事件 HITL（Day6 P1-8：复用 workorders 表 type=sensitive_alert）----
# 两层合流（方案 A 裁决）：敏感只由词表层（cascade safety）产生工单，本路由只读与流转，
# 绝不让 LLM 参与判定或回复（一票否决口径）。
# Day6+ 可见性矩阵（用户裁决）：
# - abuse（虐待指控）仅 admin+family 可见——排除 nurse/doctor（施害者可能是工作人员）；
# - 其余类目全角色可见；family 全程只读（无 resolve）且仅见自己关联长者（family_elder_links）。

sensitive_router = APIRouter(prefix="/admin/sensitive-events", tags=["admin"])

_ABUSE_CATEGORY = "abuse"


def _category_visible(category: str, role: str) -> bool:
    """abuse 对 nurse/doctor 隐藏（施害者可能是工作人员）；admin/family 全见。"""
    if role in ("admin", "family"):
        return True
    if role in ("nurse", "doctor"):
        return category != _ABUSE_CATEGORY
    return False


async def _family_linked_elder_ids(
    session: AsyncSession, account_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await session.execute(
        select(FamilyElderLink.elder_id).where(FamilyElderLink.account_id == account_id)
    )
    return [r[0] for r in rows.all()]


@sensitive_router.get("", response_model=list[SensitiveEventResponse])
async def list_sensitive_events_route(
    actor=Depends(require_role("admin", "nurse", "doctor", "family")),
    event_status: str = Query(default="open", alias="status"),
    limit: int = Query(default=50, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[SensitiveEventResponse]:
    """敏感事件列表：按角色过滤可见性（abuse 排除矩阵 + family 关联长者）。"""
    role = actor.role.value if hasattr(actor.role, "value") else str(actor.role)
    stmt = (
        select(Workorder)
        .where(Workorder.type == "sensitive_alert")
        .order_by(Workorder.created_at.desc())
        .limit(limit)
    )
    if event_status != "all":
        stmt = stmt.where(Workorder.status == event_status)
    if role == "family":
        elder_ids = await _family_linked_elder_ids(session, actor.account_id)
        stmt = stmt.where(Workorder.elder_id.in_(elder_ids or [uuid.uuid4()]))  # 无关联→空集
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SensitiveEventResponse.model_validate(r)
        for r in rows
        if _category_visible(r.category or "general", role)
    ]


@sensitive_router.post("/{workorder_id}/resolve", response_model=SensitiveEventResponse)
async def resolve_sensitive_event_route(
    workorder_id: uuid.UUID,
    body: SensitiveEventResolve,
    actor=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SensitiveEventResponse:
    """标记已处理（仅管理员）：open→resolved，审计字段 resolved_at/assignee_ref；已处理 409。"""
    row = await session.get(Workorder, workorder_id)
    if row is None or row.type != "sensitive_alert":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="敏感事件不存在")
    if row.status != WorkorderStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该事件已处理（status={row.status}）",
        )
    row.status = WorkorderStatus.RESOLVED
    row.resolved_at = datetime.now(UTC)
    # 外部引用暂存处理人（无 staff 表，Day8 护工看板再归位）
    row.assignee_ref = f"{actor.display_name}({str(actor.account_id)[:8]})"
    if body.note:
        row.description = f"{row.description or ''}\n[处理备注] {body.note}"[:2000]
    await session.commit()
    await session.refresh(row)
    return SensitiveEventResponse.model_validate(row)


# ---------------------------------------------------------------------------
# 对话记录（Day6+ 批3）：按长者看全历史 + 每轮路由动作叠加（数据源 decision_logs）
# ---------------------------------------------------------------------------

conversations_router = APIRouter(prefix="/admin/conversations", tags=["admin"])


@conversations_router.get("/elders")
async def conversation_elders_route(
    _actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """有对话记录的长者列表（会话数 + 最近活跃；匿名会话 elder 为空不展示）。"""
    rows = (
        await session.execute(
            select(
                Conversation.elder_id,
                func.count(Conversation.thread_id),
                func.max(Conversation.started_at),
                Elder.name,
            )
            .join(Elder, Conversation.elder_id == Elder.elder_id)
            .where(Conversation.elder_id.is_not(None))
            .group_by(Conversation.elder_id, Elder.name)
            .order_by(func.max(Conversation.started_at).desc())
        )
    ).all()
    return {
        "items": [
            {
                "elder_id": str(r[0]),
                "elder_name": r[3],
                "conversation_count": r[1],
                "last_active_at": r[2].isoformat(),
            }
            for r in rows
        ]
    }


def _match_decision_log(round_: dict, logs: list[DecisionLog]) -> DecisionLog | None:
    """行数不一致时的兜底：按脱敏后问题原文匹配（redact 只改身份证/剂量数字）。"""
    for lg in logs:
        if lg.input_text == round_["question"]:
            return lg
    return None


@conversations_router.get("")
async def elder_conversations_route(
    elder_id: uuid.UUID = Query(...),
    limit: int = Query(default=200, ge=1, le=500),
    _actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """指定长者的全部对话轮次：问题/回答/时间，叠加每轮路由动作（decision_logs）。

    decision_logs 每轮 utterance 1 行；同会话行数与轮数一致则按顺序对齐，
    否则按 input_text==问题 兜底匹配（对不上则该轮无路由标注，不虚构）。
    """
    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="长者不存在")

    msgs = (
        await session.execute(
            select(ConversationMessage)
            .join(Conversation, ConversationMessage.thread_id == Conversation.thread_id)
            .where(Conversation.elder_id == elder_id)
            .order_by(ConversationMessage.seq.asc())
            .limit(limit)
        )
    ).scalars().all()

    logs = (
        await session.execute(
            select(DecisionLog)
            .where(DecisionLog.elder_id == elder_id)
            .order_by(DecisionLog.created_at.asc())
        )
    ).scalars().all()
    logs_by_thread: dict[uuid.UUID, list[DecisionLog]] = {}
    for lg in logs:
        if lg.thread_id is not None:
            logs_by_thread.setdefault(lg.thread_id, []).append(lg)

    threads: dict[uuid.UUID, dict] = {}
    current: dict | None = None
    for m in msgs:
        t = threads.setdefault(m.thread_id, {"thread_id": str(m.thread_id), "rounds": []})
        if m.role == "user":
            current = {
                "asked_at": m.created_at.isoformat(),
                "question": m.content,
                "answer": None,
                "answered_at": None,
                "route": None,
            }
            t["rounds"].append(current)
        elif m.role == "assistant" and current is not None and current["answer"] is None:
            current["answer"] = m.content
            current["answered_at"] = m.created_at.isoformat()

    for thread_id, t in threads.items():
        bucket = t["rounds"]
        thread_logs = logs_by_thread.get(thread_id, [])
        if len(thread_logs) == len(bucket):
            pairs = list(zip(bucket, thread_logs))  # 每轮 1 行：顺序天然对齐
        else:
            pairs = [(r, _match_decision_log(r, thread_logs)) for r in bucket]
        for rnd, lg in pairs:
            if lg is not None:
                rnd["route"] = {
                    "route_layer": lg.route_layer,
                    "intent": lg.intent,
                    "tool_calls": lg.tool_calls,
                    "degraded": lg.degraded,
                    "sensitive_hit": lg.sensitive_hit,
                }

    return {
        "elder_id": str(elder_id),
        "elder_name": elder.name,
        "threads": list(threads.values()),
    }
