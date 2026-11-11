"""健康数值登记服务（Day8 步 5 接图改造）。

护士/医生登记的健康数值同步写入趋势数据源 health_records（用户 2026-11-11 裁决）：
- **三通道共用一图**（大纲 8.1 红线）：写入即走 app.graphs.health.run_triage，
  与晚间外呼采集（步 4）/主控子图主诉（步 3）同一张健康图——yellow/red 建单
  挂 interrupt，green 零副作用，阈值永不进 prompt；
- 基线口径由图内 triage 节点统一计算（近 7 天均值、排除当前记录），
  本服务不再自行算基线。
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import HealthRecord
from app.schemas.health_records import HealthRecordIn

logger = get_logger(__name__)

_TZ_SH = ZoneInfo("Asia/Shanghai")


def _hhmm_to_utc(hhmm: str, *, previous_day: bool) -> datetime | None:
    """HH:MM（北京时间）→ UTC：bed 视为昨夜、wake 视为今晨（登记时刻锚定今天）。"""
    if not hhmm:
        return None
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    day = (datetime.now(_TZ_SH) - timedelta(days=1 if previous_day else 0)).date()
    return datetime.combine(day, time(hour, minute), tzinfo=_TZ_SH).astimezone(UTC)


async def create_and_triage(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    elder_name: str,
    payload: HealthRecordIn,
) -> HealthRecord:
    """写入健康记录并走健康图分级（yellow/red 建单挂 interrupt，green 零副作用）。"""
    record = HealthRecord(
        elder_id=elder_id,
        recorded_at=datetime.now(UTC),
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        sleep_minutes=payload.sleep_minutes,
        mood=str(payload.mood) if payload.mood is not None else None,
        chief_complaint=(payload.chief_complaint or None),
        source="manual_register",
        sleep_bed_time=_hhmm_to_utc(payload.sleep_bed_time, previous_day=True),
        sleep_wake_time=_hhmm_to_utc(payload.sleep_wake_time, previous_day=False),
        sleep_awakenings=payload.sleep_awakenings,
    )
    session.add(record)
    await session.flush()

    # 三通道汇合：登记通道与外呼/主诉共用同一张健康图（分级由图内规则引擎回填）
    from app.graphs.health import default_notifier, run_triage

    await run_triage(
        session,
        record=record,
        elder_name=elder_name,
        notifier=default_notifier,
        checkpoint_path=None,  # 生产走 Settings.health_checkpoint_db；测试经 Settings 打桩
    )
    logger.info(
        "health record registered",
        extra={
            "event": "health_record_registered",
            "record_id": str(record.record_id),
            "elder_id": str(elder_id),
            "triage_level": record.triage_level,
        },
    )
    return record
