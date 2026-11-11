"""健康数值登记契约（Day8 实时数据批次）：护士/医生登记 → 同步入趋势数据源。

- 数值边界与 ORM CHECK 一致（收缩 50–250 / 舒张 30–150 / 睡眠 0–1440），越界 422；
- mood 1–5 整数（存储层转整数字符串口径）；bed/wake 为 HH:MM（北京时间口径，用户全局规则）；
- 全字段可选：至少一项数值/主诉必填（全空 422）。
"""

from uuid import UUID

from pydantic import BaseModel, Field, model_validator

SleepMinutes = Field(default=None, ge=0, le=1440)


class HealthRecordIn(BaseModel):
    bp_systolic: int | None = Field(default=None, ge=50, le=250)
    bp_diastolic: int | None = Field(default=None, ge=30, le=150)
    sleep_minutes: int | None = Field(default=None, ge=0, le=1440)
    sleep_awakenings: int | None = Field(default=None, ge=0, le=20)
    sleep_bed_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")  # HH:MM
    sleep_wake_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    mood: int | None = Field(default=None, ge=1, le=5)
    chief_complaint: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _at_least_one(self) -> "HealthRecordIn":
        numeric = (
            self.bp_systolic,
            self.bp_diastolic,
            self.sleep_minutes,
            self.sleep_awakenings,
            self.sleep_bed_time,
            self.sleep_wake_time,
            self.mood,
        )
        if all(v is None for v in numeric) and not (self.chief_complaint or "").strip():
            raise ValueError("至少填写一项健康数值或主诉")
        return self


class HealthRecordOut(BaseModel):
    record_id: UUID
    elder_id: UUID
    triage_level: str | None
    triage_reason: str | None
