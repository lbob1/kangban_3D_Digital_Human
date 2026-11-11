"""健康趋势响应契约（Day8 睡眠数据批次：医生页 14 天多图可视化数据源）。"""

from datetime import date as date_t
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SleepDay(BaseModel):
    """单日睡眠细维度（与 dates 数组对齐；缺失日 minutes 为 null）。"""

    model_config = ConfigDict(from_attributes=True)

    date: date_t
    minutes: int | None = None
    deep: int | None = None
    light: int | None = None
    awakenings: int | None = None
    bed_time: str | None = None  # HH:MM（北京时间）
    wake_time: str | None = None  # HH:MM（北京时间）


class ComplaintDay(BaseModel):
    """有主诉的单日（仅列出非空主诉，按时间升序）。"""

    model_config = ConfigDict(from_attributes=True)

    date: date_t
    text: str


class ElderOverview(BaseModel):
    """单长者 14 天汇总行（总览表）+ 迷你趋势序列（sbp/sleep 与窗口对齐，缺失日 null）。"""

    model_config = ConfigDict(from_attributes=True)

    elder_id: UUID
    elder_name: str
    room_no: str | None = None
    avg_sbp: float | None = None
    avg_dbp: float | None = None
    avg_sleep: float | None = None
    avg_awakenings: float | None = None
    avg_mood: float | None = None
    yellow_count: int = 0
    red_count: int = 0
    missing_days: int = 0
    sbp_series: list[int | None]
    sleep_series: list[int | None]


class HealthOverviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    days: int = Field(ge=1, le=30)
    items: list[ElderOverview]


class HealthTrendsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    elder_id: UUID
    elder_name: str
    days: int = Field(ge=1, le=30)
    dates: list[date_t]
    bp_systolic: list[int | None]
    bp_diastolic: list[int | None]
    mood: list[int | None]  # 1–5 分（整数字符串口径解析，非法值归 null）
    sleep: list[SleepDay]
    complaints: list[ComplaintDay]
