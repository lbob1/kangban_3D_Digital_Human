"""设备业务：绑定、注册凭证签发/消费、devices/me 自查（单人间锁定 / 双人间候选）。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.auth.device_token import (
    DeviceCredentialError,
    IssuedAccessToken,
    IssuedRegisterToken,
    _hash_token,
    consume_register_token,
    issue_register_token,
)
from app.core.logging import get_logger
from app.models import Device, DeviceToken, Elder, Room
from app.models.enums import DeviceStatus, DeviceTokenKind, ElderStatus, RoomStatus
from app.schemas.device import BindingUpdate, ElderCandidate

logger = get_logger(__name__)


class DeviceValidationError(Exception):
    """绑定校验失败（房间不可用、床位超容量）；路由层映射 422。"""


class DeviceConflict(Exception):
    """床位已被其他设备绑定；路由层映射 409。"""


class DeviceNotFound(Exception):
    """设备不存在；路由层映射 404。"""


async def bind_device(
    session: AsyncSession, device: Device, data: BindingUpdate, actor: UserIdentity
) -> Device:
    """绑定设备到房间/床位；同床位其他活跃设备占用返回 409。"""
    room = (
        await session.execute(select(Room).where(Room.room_id == data.room_id))
    ).scalar_one_or_none()
    if room is None or room.status != RoomStatus.ACTIVE:
        raise DeviceValidationError("房间不存在或非可用状态")
    if data.bed_no is not None and data.bed_no > room.bed_count:
        raise DeviceValidationError("床位号超出房间床位数")

    if data.bed_no is not None:
        conflict = (
            await session.execute(
                select(Device).where(
                    Device.room_id == data.room_id,
                    Device.bed_no == data.bed_no,
                    Device.status == DeviceStatus.ACTIVE,
                    Device.device_id != device.device_id,
                )
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise DeviceConflict("该床位已绑定其他设备")

    device.room_id = data.room_id
    device.bed_no = data.bed_no
    await session.flush()
    logger.info(
        "device bound",
        extra={
            "event": "device_bound",
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "object_type": "device",
            "object_id": str(device.device_id),
            "result": "success",
        },
    )
    return device


async def issue_registration_token(
    session: AsyncSession, device_id: UUID, actor: UserIdentity
) -> IssuedRegisterToken:
    """为设备签发一次性注册凭证；设备不存在抛 DeviceNotFound。"""
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None:
        raise DeviceNotFound("设备不存在")
    issued = await issue_register_token(session, device_id)
    logger.info(
        "registration token issued",
        extra={
            "event": "device_register_token_issued",
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "object_type": "device",
            "object_id": str(device_id),
            "result": "success",
        },
    )
    return issued


async def register_device(
    session: AsyncSession, device_code: str, registration_token: str
) -> IssuedAccessToken:
    """平板注册：设备编码 + 注册凭证换发长期设备 Token（凭证一次性，即换即吊销）。"""
    device = (
        await session.execute(select(Device).where(Device.device_code == device_code))
    ).scalar_one_or_none()
    register_row = (
        await session.execute(
            select(DeviceToken).where(
                DeviceToken.token_hash == _hash_token(registration_token),
                DeviceToken.token_kind == DeviceTokenKind.REGISTER,
            )
        )
    ).scalar_one_or_none()

    if device is None or register_row is None or register_row.device_id != device.device_id:
        raise DeviceCredentialError("invalid registration token")

    issued = await consume_register_token(session, register_row, registration_token)
    await session.commit()
    logger.info(
        "device registered",
        extra={
            "event": "device_registered",
            "object_type": "device",
            "object_id": str(device.device_id),
            "result": "success",
        },
    )
    return issued


async def device_self_view(session: AsyncSession, device_id: UUID) -> dict:
    """devices/me：单人间（bed_count==1）锁定在住长者；双人间返回全部在住长者候选。"""
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None or device.room_id is None:
        raise DeviceNotFound("设备未绑定房间")

    room = (
        await session.execute(select(Room).where(Room.room_id == device.room_id))
    ).scalar_one_or_none()
    if room is None:
        raise DeviceNotFound("房间不存在")

    rows = await session.execute(
        select(Elder).where(
            Elder.room_id == device.room_id, Elder.status == ElderStatus.ACTIVE
        )
    )
    elders = [
        ElderCandidate(elder_id=e.elder_id, name=e.name, bed_no=e.bed_no)
        for e in rows.scalars().all()
    ]
    elders.sort(key=lambda e: (e.bed_no is None, e.bed_no or 0))

    if room.bed_count == 1:
        return {
            "device_id": device.device_id,
            "device_code": device.device_code,
            "room_id": room.room_id,
            "building_code": room.building_code,
            "room_number": room.room_number,
            "bed_count": room.bed_count,
            "mode": "single",
            "elder": elders[0] if elders else None,
            "candidates": [],
        }
    return {
        "device_id": device.device_id,
        "device_code": device.device_code,
        "room_id": room.room_id,
        "building_code": room.building_code,
        "room_number": room.room_number,
        "bed_count": room.bed_count,
        "mode": "double",
        "elder": None,
        "candidates": elders,
    }
