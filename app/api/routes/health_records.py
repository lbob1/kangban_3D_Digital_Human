"""健康数值登记端点（Day8 步 5 接图改造）：POST /api/v1/health-records。

- 角色：nurse/doctor/admin（require_role 门禁）；doctor 按有效关联、范围外 404；
  **nurse 2026-11-11 用户裁决：仅健康登记通道放开全部在住长者**（楼栋隔离红线
  仍保留在 workqueue 等其余接口，不受影响）；
- 写入即走健康图分级（三通道共用一图：yellow/red 建单挂 interrupt 等确认，
  green 零副作用），登记成功后前端立即刷新趋势图（30 秒轮询兜底跨端同步）；
- GET /register-elders：登记下拉名册，与 POST 同口径（nurse 全量在住 / doctor 关联）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.models import Elder
from app.models.enums import AccountRole, ElderStatus
from app.schemas.elder import ElderResponse
from app.schemas.health_records import HealthRecordIn, HealthRecordOut
from app.services.elder import (
    ElderNotFound,
    ElderPermissionDenied,
    get_elder_for_user,
    list_elders_for_user,
)
from app.services.health_records import create_and_triage

router = APIRouter(prefix="/health-records", tags=["health-records"])


@router.get("/register-elders", response_model=list[ElderResponse])
async def list_register_elders(
    actor: UserIdentity = Depends(require_role("nurse", "doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> list[ElderResponse]:
    """登记名册（在住）：nurse/admin 全量、doctor 按有效关联——与 POST 同口径。"""
    if actor.role == AccountRole.NURSE:
        rows = await session.execute(
            select(Elder)
            .where(Elder.status == ElderStatus.ACTIVE)
            .order_by(Elder.created_at)
        )
        elders = list(rows.scalars().all())
    else:
        elders = [
            e for e in await list_elders_for_user(session, actor)
            if e.status == ElderStatus.ACTIVE
        ]
    return [ElderResponse.model_validate(e) for e in elders]


@router.post("", response_model=HealthRecordOut, status_code=status.HTTP_201_CREATED)
async def create_health_record(
    payload: HealthRecordIn,
    elder_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role("nurse", "doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthRecordOut:
    if actor.role == AccountRole.NURSE:
        # 2026-11-11 用户裁决：健康登记对护士放开全部长者（仅此通道；不存在仍 404）
        elder = await session.get(Elder, elder_id)
        if elder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="长者不存在"
            )
    else:
        try:
            elder = await get_elder_for_user(session, actor, elder_id)
        except ElderPermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        except ElderNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
    record = await create_and_triage(
        session, elder_id=elder.elder_id, elder_name=elder.name, payload=payload
    )
    # get_session 依赖不自动提交；漏 commit 则请求结束即回滚（2026-11-11 联调修复）
    await session.commit()
    return HealthRecordOut(
        record_id=record.record_id,
        elder_id=record.elder_id,
        triage_level=record.triage_level,
        triage_reason=record.triage_reason,
    )
