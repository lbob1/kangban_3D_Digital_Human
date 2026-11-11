"""健康数值登记服务（Day8 实时数据批次）。

护士/医生登记的健康数值同步写入趋势数据源 health_records（用户 2026-11-11 裁决）：
- 走规则引擎算分级并落库 triage_level/triage_reason（与晚间外呼同口径）；
- 本服务**不建工单不发通知**（与种子同口径）；分级工单/健康图 interrupt
  接入留 Day8 步 5（端点接图改造），届时替换此处直接 triage 的实现；
- 基线口径与健康图 _baseline 一致：近 7 天均值（新记录尚未入库，天然排除自身）。
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import HealthRecord
from app.schemas.health_records import HealthRecordIn
from app.services.health_triage import triage

logger = get_logger(__name__)

_TZ_SH = ZoneInfo("Asia/Shanghai")


async def _baseline(session: AsyncSession, elder_id: uuid.UUID) -> dict[str, float | None]:
    """近 7 天基线均值（误报防护输入）：与 app.graphs.health._baseline 同口径。"""
    since = datetime.now(UTC) - timedelta(days=7)
    rows = (
        await session.execute(
            select(
                HealthRecord.bp_systolic,
                HealthRecord.bp_diastolic,
                HealthRecord.sleep_minutes,
            ).where(
                HealthRecord.elder_id == elder_id,
                HealthRecord.recorded_at >= since,
            )
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


def _hhmm_to_utc(hhmm: str, *, previous_day: bool) -> datetime | None:
    """HH:MM（北京时间）→ UTC：bed 视为昨夜、wake 视为今晨（登记时刻锚定今天）。"""
    if not hhmm:
        return None
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    day = (datetime.now(_TZ_SH) - timedelta(days=1 if previous_day else 0)).date()
    return datetime.combine(day, time(hour, minute), tzinfo=_TZ_SH).astimezone(UTC)


async def create_and_triage(
    session: AsyncSession, *, elder_id: uuid.UUID, payload: HealthRecordIn
) -> HealthRecord:
    """写入健康记录并回填规则引擎分级（不建单不通知——分级工单留步 5 接图）。"""
    result = triage(
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        sleep_minutes=payload.sleep_minutes,
        mood=str(payload.mood) if payload.mood is not None else None,
        chief_complaint=payload.chief_complaint,
        baseline=await _baseline(session, elder_id),
    )
    record = HealthRecord(
        elder_id=elder_id,
        recorded_at=datetime.now(UTC),
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        sleep_minutes=payload.sleep_minutes,
        mood=str(payload.mood) if payload.mood is not None else None,
        chief_complaint=(payload.chief_complaint or None),
        source="manual_register",
        triage_level=result.level,
        triage_reason=result.reason,
        sleep_bed_time=_hhmm_to_utc(payload.sleep_bed_time, previous_day=True),
        sleep_wake_time=_hhmm_to_utc(payload.sleep_wake_time, previous_day=False),
        sleep_awakenings=payload.sleep_awakenings,
    )
    session.add(record)
    await session.flush()
    logger.info(
        "health record registered",
        extra={
            "event": "health_record_registered",
            "record_id": str(record.record_id),
            "elder_id": str(elder_id),
            "triage_level": result.level,
        },
    )
    return record
