"""家属每日日报契约（Day8 步8，大纲 8.3）：当日健康摘要 + 用药完成情况。

TTS 语音条不走 JSON：单独 GET /family/daily-report/audio 返回 audio/mpeg
（前端 fetch blob 后播放，Bearer 经 header 携带）；失败降级纯文字。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel


class MedicationSummary(BaseModel):
    """当日用药事件计数（按 scheduled_at 北京时间窗口统计）。"""

    total: int = 0
    confirmed: int = 0
    escalated: int = 0


class DailyReportResponse(BaseModel):
    """当日健康摘要：取当日最新一条健康记录（与医生趋势「当日最后一条」同口径）。"""

    elder_id: UUID
    elder_name: str
    date: date
    has_record: bool = False
    triage_level: str | None = None
    triage_reason: str | None = None
    bp_systolic: int | None = None
    bp_diastolic: int | None = None
    sleep_minutes: int | None = None
    sleep_awakenings: int | None = None
    # 口径 1–5 整数字符串；输出转 int
    mood: int | None = None
    complaint: str | None = None
    medication: MedicationSummary = MedicationSummary()
    # 日报正文（展示与 TTS 同源同文）
    summary_text: str
