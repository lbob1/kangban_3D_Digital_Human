"""家属端路由（Day8 步8，大纲 8.3）：每日日报（健康摘要 + 用药完成 + TTS 语音条）。

- GET /family/daily-report?elder_id=：当日健康摘要（分级结论/血压/睡眠/情绪/主诉，
  取当日最新一条，与医生趋势同口径）+ 用药事件计数（scheduled_at 北京时间窗口）
  + summary_text 正文（展示与 TTS 同源同文）；
- GET /family/daily-report/audio?elder_id=：EdgeTTS 合成 summary_text 为 MP3，
  内存缓存当日一份（进程重启重合成，属演示缓存非数据资产）；TTS 失败 503，
  前端降级纯文字（不白屏）。

权限（矩阵逐条对齐）：family 按有效关联过滤（get_elder_for_user 复用，
范围外 404 隐藏存在性）；admin 可代查；nurse/doctor 403。
"""

import hashlib
import io
from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import HealthRecord, MedicationEvent
from app.schemas.family_report import DailyReportResponse, MedicationSummary
from app.services.elder import ElderNotFound, ElderPermissionDenied, get_elder_for_user

router = APIRouter(prefix="/family", tags=["family-report"])

logger = get_logger(__name__)

_TZ_SH = ZoneInfo("Asia/Shanghai")

_LEVEL_TEXT = {
    "green": "整体状况平稳",
    "yellow": "今日需要多加留意，工作人员已跟进",
    "red": "今日出现需要关注的情况，医护人员已确认处理",
}

# MP3 内存缓存：{(elder_id, date, text_hash): bytes}；当日一份，换日/换文自然失效
_AUDIO_CACHE: dict[tuple[str, str, str], bytes] = {}


def _shanghai_date(dt: datetime) -> date:
    return dt.astimezone(_TZ_SH).date()


def _start_of_day_utc(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=_TZ_SH).astimezone(UTC)


def _parse_mood(mood) -> int | None:
    if mood is None:
        return None
    try:
        value = int(str(mood).strip())
    except ValueError:
        return None
    return value if 1 <= value <= 5 else None


async def _build_report(
    session: AsyncSession, actor: UserIdentity, elder_id: UUID
) -> DailyReportResponse:
    elder = None
    try:
        elder = await get_elder_for_user(session, actor, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    today = _shanghai_date(datetime.now(UTC))
    start_utc = _start_of_day_utc(today)

    record = (
        await session.execute(
            select(HealthRecord)
            .where(
                HealthRecord.elder_id == elder.elder_id,
                HealthRecord.recorded_at >= start_utc,
            )
            .order_by(HealthRecord.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    events = (
        await session.execute(
            select(MedicationEvent.status).where(
                MedicationEvent.elder_id == elder.elder_id,
                MedicationEvent.scheduled_at >= start_utc,
            )
        )
    ).scalars().all()
    medication = MedicationSummary(
        total=len(events),
        confirmed=sum(1 for s in events if s == "confirmed"),
        escalated=sum(1 for s in events if s == "escalated"),
    )

    parts: list[str] = [f"您好，{elder.name}今日日报。"]
    if record is None:
        parts.append("今天还没有健康记录。")
    else:
        parts.append(_LEVEL_TEXT.get(record.triage_level or "", "整体状况平稳") + "。")
        if record.bp_systolic is not None and record.bp_diastolic is not None:
            parts.append(f"血压 {record.bp_systolic} 除 {record.bp_diastolic}。")
        if record.sleep_minutes is not None:
            hours, minutes = divmod(int(record.sleep_minutes), 60)
            sleep_text = f"睡眠 {hours} 小时" + (f" {minutes} 分钟" if minutes else "")
            if record.sleep_awakenings is not None:
                sleep_text += f"，夜间醒来 {record.sleep_awakenings} 次"
            parts.append(sleep_text + "。")
        mood = _parse_mood(record.mood)
        if mood is not None:
            parts.append(f"心情 {mood} 分（5 分制）。")
        if record.chief_complaint and record.chief_complaint.strip():
            parts.append(f"主诉：{record.chief_complaint.strip()}。")
    if medication.total:
        med_text = f"今日用药计划 {medication.total} 次，已确认 {medication.confirmed} 次"
        if medication.escalated:
            med_text += f"，{medication.escalated} 次已升级人工跟进"
        parts.append(med_text + "。")

    return DailyReportResponse(
        elder_id=elder.elder_id,
        elder_name=elder.name,
        date=today,
        has_record=record is not None,
        triage_level=record.triage_level if record else None,
        triage_reason=record.triage_reason if record else None,
        bp_systolic=record.bp_systolic if record else None,
        bp_diastolic=record.bp_diastolic if record else None,
        sleep_minutes=record.sleep_minutes if record else None,
        sleep_awakenings=record.sleep_awakenings if record else None,
        mood=_parse_mood(record.mood) if record else None,
        complaint=record.chief_complaint if record else None,
        medication=medication,
        summary_text="".join(parts),
    )


@router.get("/daily-report", response_model=DailyReportResponse)
async def daily_report(
    elder_id: UUID,
    actor: UserIdentity = Depends(require_role("family", "admin")),
    session: AsyncSession = Depends(get_session),
) -> DailyReportResponse:
    return await _build_report(session, actor, elder_id)


async def _synthesize_mp3(text: str) -> bytes:
    """EdgeTTS 直接产 MP3（HTTP 语音条不需要 PCM 解码；失败由调用方转 503）。"""
    import edge_tts  # 延迟导入：与其他 Provider 同口径

    mp3 = io.BytesIO()
    communicate = edge_tts.Communicate(text, get_settings().edge_tts_voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3.write(chunk["data"])
    return mp3.getvalue()


@router.get("/daily-report/audio")
async def daily_report_audio(
    elder_id: UUID,
    actor: UserIdentity = Depends(require_role("family", "admin")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """语音条：summary_text 合成 MP3（缓存当日一份）；TTS 失败 503 降级文字。"""
    report = await _build_report(session, actor, elder_id)
    key = (
        str(report.elder_id),
        report.date.isoformat(),
        hashlib.sha1(report.summary_text.encode("utf-8")).hexdigest()[:16],
    )
    cached = _AUDIO_CACHE.get(key)
    if cached is None:
        try:
            cached = await _synthesize_mp3(report.summary_text)
        except Exception as exc:  # noqa: BLE001 —— 网络/依赖失败统一降级
            logger.warning(
                "daily report tts failed",
                extra={"event": "daily_report_tts_failed", "reason": str(exc)[:120]},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="语音合成暂不可用，请查看文字版日报",
            ) from exc
        # 换日自然淘汰：只保留当前日期缓存，防长驻进程膨胀
        today = report.date.isoformat()
        for k in [k for k in _AUDIO_CACHE if k[1] != today]:
            _AUDIO_CACHE.pop(k, None)
        _AUDIO_CACHE[key] = cached
    return Response(content=cached, media_type="audio/mpeg")
