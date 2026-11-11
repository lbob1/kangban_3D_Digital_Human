"""医生端路由（Day8 睡眠数据批次）：健康趋势多图数据源。

GET /api/v1/doctor/health-trends?elder_id=&days=14
- 角色：doctor/admin（require_role 门禁）；doctor 按有效关联过滤（get_elder_for_user：
  范围外 404 隐藏存在性、无权限角色 403）；admin 不过滤；
- 同日多条记录取当日最后一条（外呼采集 + 查房等并存时以最新为准）；
- 输出与 dates 数组对齐的完整窗口（缺失日为 null），前端画折线保持连续性；
- 所有时刻展示口径=北京时间（用户全局规则），HH:MM 字符串由服务端转换。
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.models import HealthRecord, Room
from app.schemas.health_trends import (
    ComplaintDay,
    ElderOverview,
    HealthOverviewResponse,
    HealthTrendsResponse,
    SleepDay,
)
from app.services.elder import (
    ElderNotFound,
    ElderPermissionDenied,
    get_elder_for_user,
    list_elders_for_user,
)

router = APIRouter(prefix="/doctor", tags=["health-trends"])

_TZ_SH = ZoneInfo("Asia/Shanghai")


def _shanghai_date(dt: datetime) -> date:
    """UTC → 北京时间日期（全局规则：时间展示统一北京时间）。"""
    return dt.astimezone(_TZ_SH).date()


def _shanghai_hhmm(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    local = dt.astimezone(_TZ_SH)
    return f"{local.hour:02d}:{local.minute:02d}"


def _sh_start_of_day_utc(d: date) -> datetime:
    """北京时区某日 00:00 → UTC aware（供 DB 比对）。"""
    return datetime.combine(d, time.min, tzinfo=_TZ_SH).astimezone(UTC)


def _parse_mood(mood) -> int | None:
    """mood 存储口径 1–5 整数字符串；解析失败/越界归 None（与规则引擎同口径）。"""
    if mood is None:
        return None
    try:
        value = int(str(mood).strip())
    except ValueError:
        return None
    return value if 1 <= value <= 5 else None


@router.get("/health-trends", response_model=HealthTrendsResponse)
async def health_trends(
    elder_id: uuid.UUID,
    days: int = Query(default=14, ge=7, le=30),
    actor: UserIdentity = Depends(require_role("doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthTrendsResponse:
    # 范围判定：范围外 404 隐藏存在性、无权限角色 403（与 health_profile 同语义）
    try:
        elder = await get_elder_for_user(session, actor, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # 窗口：北京时间今天-(days-1) 起（含今天），换 UTC 比对 recorded_at
    today_sh = _shanghai_date(datetime.now(UTC))
    start_date = today_sh - timedelta(days=days - 1)
    start_utc = _sh_start_of_day_utc(start_date)

    rows = (
        (
            await session.execute(
                select(HealthRecord)
                .where(
                    HealthRecord.elder_id == elder.elder_id,
                    HealthRecord.recorded_at >= start_utc,
                )
                .order_by(HealthRecord.recorded_at.asc())
            )
        )
        .scalars()
        .all()
    )
    by_date: dict = {}
    for row in rows:  # 升序遍历：同日后者覆盖前者 = 取当日最后一条
        by_date[_shanghai_date(row.recorded_at)] = row

    dates = [start_date + timedelta(days=i) for i in range(days)]
    bp_systolic: list[int | None] = []
    bp_diastolic: list[int | None] = []
    mood: list[int | None] = []
    sleep_days: list[SleepDay] = []
    complaints: list[ComplaintDay] = []
    for d in dates:
        row = by_date.get(d)
        bp_systolic.append(row.bp_systolic if row else None)
        bp_diastolic.append(row.bp_diastolic if row else None)
        mood.append(_parse_mood(row.mood) if row else None)
        sleep_days.append(
            SleepDay(
                date=d,
                minutes=row.sleep_minutes if row else None,
                deep=row.sleep_deep_minutes if row else None,
                light=row.sleep_light_minutes if row else None,
                awakenings=row.sleep_awakenings if row else None,
                bed_time=_shanghai_hhmm(row.sleep_bed_time) if row else None,
                wake_time=_shanghai_hhmm(row.sleep_wake_time) if row else None,
            )
        )
        if row and row.chief_complaint and row.chief_complaint.strip():
            complaints.append(ComplaintDay(date=d, text=row.chief_complaint.strip()))

    return HealthTrendsResponse(
        elder_id=elder.elder_id,
        elder_name=elder.name,
        days=days,
        dates=dates,
        bp_systolic=bp_systolic,
        bp_diastolic=bp_diastolic,
        mood=mood,
        sleep=sleep_days,
        complaints=complaints,
    )


# ---- 14 天总览（全部可见长者 × 指标汇总 + 迷你趋势）----


def _avg(values: list) -> float | None:
    nums = [float(v) for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None


@router.get("/health-overview", response_model=HealthOverviewResponse)
async def health_overview(
    days: int = Query(default=14, ge=7, le=30),
    actor: UserIdentity = Depends(require_role("doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthOverviewResponse:
    """全部可见长者 × 指标汇总：均值血压/睡眠/夜醒/情绪 + yellow/red 次数 + 迷你趋势。"""
    elders = await list_elders_for_user(session, actor)
    today_sh = _shanghai_date(datetime.now(UTC))
    start_date = today_sh - timedelta(days=days - 1)
    start_utc = _sh_start_of_day_utc(start_date)
    elder_ids = [e.elder_id for e in elders]
    rows = (
        (
            await session.execute(
                select(HealthRecord)
                .where(
                    HealthRecord.elder_id.in_(elder_ids),
                    HealthRecord.recorded_at >= start_utc,
                )
                .order_by(HealthRecord.recorded_at.asc())
            )
        )
        .scalars()
        .all()
        if elder_ids
        else []
    )
    by_elder: dict = {}
    for row in rows:
        by_elder.setdefault(row.elder_id, {}).setdefault(_shanghai_date(row.recorded_at), row)

    items: list[ElderOverview] = []
    room_rows = (
        (
            await session.execute(
                select(Room.room_id, Room.room_number).where(
                    Room.room_id.in_([e.room_id for e in elders if e.room_id])
                )
            )
        ).all()
        if elder_ids
        else []
    )
    room_no_map = {room_id: str(room_number) for room_id, room_number in room_rows}
    for elder in elders:
        per_date = by_elder.get(elder.elder_id, {})
        dates = [start_date + timedelta(days=i) for i in range(days)]
        recs = [per_date.get(d) for d in dates]
        sbp_series = [r.bp_systolic if r else None for r in recs]
        sleep_series = [r.sleep_minutes if r else None for r in recs]
        mood_vals = [_parse_mood(r.mood) if r else None for r in recs]
        levels = [r.triage_level if r else None for r in recs]
        missing = sum(1 for r in recs if r is None)
        items.append(
            ElderOverview(
                elder_id=elder.elder_id,
                elder_name=elder.name,
                room_no=room_no_map.get(elder.room_id),
                avg_sbp=_avg(sbp_series),
                avg_dbp=_avg([r.bp_diastolic if r else None for r in recs]),
                avg_sleep=_avg(sleep_series),
                avg_awakenings=_avg([r.sleep_awakenings if r else None for r in recs]),
                avg_mood=_avg(mood_vals),
                yellow_count=sum(1 for lv in levels if lv == "yellow"),
                red_count=sum(1 for lv in levels if lv == "red"),
                missing_days=missing,
                sbp_series=sbp_series,
                sleep_series=sleep_series,
            )
        )
    return HealthOverviewResponse(days=days, items=items)
