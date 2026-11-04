"""设备路由：绑定、一次性注册凭证签发、设备自查。"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_device, require_admin
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import Device
from app.schemas.device import (
    BindingUpdate,
    DeviceResponse,
    DeviceSelfResponse,
    RegistrationTokenResponse,
)
from app.services.device import (
    DeviceConflict,
    DeviceNotFound,
    DeviceValidationError,
    bind_device,
    device_self_view,
    issue_registration_token,
)

router = APIRouter(prefix="/devices", tags=["devices"])
# 管理员子路由：注册凭证签发挂在 /admin/devices 下（对齐 Day2 文档 6.2 接口清单）
admin_router = APIRouter(prefix="/admin/devices", tags=["admin"])
logger = get_logger(__name__)


@router.put("/{device_id}/binding", response_model=DeviceResponse)
async def bind_device_route(
    device_id: uuid.UUID,
    body: BindingUpdate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DeviceResponse:
    """绑定设备到房间/床位（仅管理员）；床位冲突 409、校验失败 422。"""
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    try:
        device = await bind_device(session, device, body, actor)
    except DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except DeviceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    if idempotency_key:
        logger.info(
            "binding idempotency key",
            extra={
                "event": "idempotency_key",
                "object_type": "device",
                "object_id": str(device_id),
                "idempotency_key": idempotency_key,
            },
        )
    return DeviceResponse.model_validate(device)


@admin_router.post("/{device_id}/registration-tokens", response_model=RegistrationTokenResponse)
async def issue_registration_token_route(
    device_id: uuid.UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RegistrationTokenResponse:
    """签发一次性注册凭证（默认 24 小时有效），明文仅此一次返回。"""
    try:
        issued = await issue_registration_token(session, device_id, actor)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await session.commit()
    return RegistrationTokenResponse(
        device_id=device_id,
        registration_token=issued.plaintext,
        expires_at=issued.token.expires_at,
    )


@router.get("/me", response_model=DeviceSelfResponse)
async def device_me(
    device_id: uuid.UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> DeviceSelfResponse:
    """设备自查：单人间锁定在住长者，双人间返回候选列表。"""
    try:
        view = await device_self_view(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DeviceSelfResponse(**view)
