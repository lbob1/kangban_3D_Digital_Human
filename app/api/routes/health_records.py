"""健康数值登记端点（Day8 实时数据批次）：POST /api/v1/health-records。

- 角色：nurse/doctor/admin（require_role 门禁）；长者范围判定与 health_profile 同语义
  （范围外 404 隐藏存在性、无权限角色 403）；
- 写入即走规则引擎分级（triage_level/triage_reason 落库），登记成功后前端立即
  刷新趋势图（30 秒轮询兜底跨端同步）；
- 分级工单/健康图 interrupt 接入留 Day8 步 5（本端点届时改造接图）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.schemas.health_records import HealthRecordIn, HealthRecordOut
from app.services.elder import ElderNotFound, ElderPermissionDenied, get_elder_for_user
from app.services.health_records import create_and_triage

router = APIRouter(prefix="/health-records", tags=["health-records"])


@router.post("", response_model=HealthRecordOut, status_code=status.HTTP_201_CREATED)
async def create_health_record(
    payload: HealthRecordIn,
    elder_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role("nurse", "doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthRecordOut:
    try:
        elder = await get_elder_for_user(session, actor, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    record = await create_and_triage(session, elder_id=elder.elder_id, payload=payload)
    return HealthRecordOut(
        record_id=record.record_id,
        elder_id=record.elder_id,
        triage_level=record.triage_level,
        triage_reason=record.triage_reason,
    )
