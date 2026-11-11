"""Day8 睡眠数据批次：健康多日演示数据种子（幂等，可重复执行）。

内容（全部在住长者 × 14 天，每日 20:00 北京时间一条 health_records 记录）：
1) 五类场景轮换编排（保证连续性与真实感；场景内数值逐日平滑漂移）：
   0 血压偏高型：基线 128–152 收缩压漂移，第 -4 天 168/96 → yellow；
   1 平稳型：全指标正常 → 全 green；
   2 睡眠差型：多数正常，近 3 天 240–280min（<300 → yellow）+ 夜醒 2–4 次；
   3 偶发急警型：多数正常，最后一天主诉「胸口疼」→ red（主诉直达，演示分级链）；
   4 情绪低落型：情绪多为 4 分，第 -6/-9 天 2 分 → yellow。
2) 睡眠细维度（0018 五列）：入睡 21:20–23:10、起床 06:00–07:40、夜醒按场景、
   deep+light ≤ minutes（写入方一致性红线）；
3) 分级回填：走 app.services.health_triage.triage 规则引擎（基线用此前生成日
   的均值，与图内 _baseline 同口径），只算分级**不建工单不发通知**（用户裁决）；
4) 医生关联兜底：无任何 active 关联的医生账号自动关联全部在住长者（已有关联的
   医生不动），保证医生页趋势区块开箱可看。

幂等策略：source="seed_health_days" 的 14 天窗口记录先删后插（重跑即刷新）。

用法：uv run python scripts/seed_health_days.py [--days 14]
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import (
    Account,
    DoctorElderAssignment,
    Elder,
    HealthRecord,
)
from app.models.enums import AccountRole, AssignmentStatus, ElderStatus
from app.services.health_triage import triage

TZ_SH = ZoneInfo("Asia/Shanghai")
SOURCE = "seed_health_days"

rng = random.Random(20261111)  # 固定种子：重跑数据完全一致（演示可复现）


def _sh_noon_utc(d) -> datetime:
    """北京时区某日 20:00 → UTC（采集时刻口径：晚间外呼）。"""
    return datetime.combine(d, time(20, 0), tzinfo=TZ_SH).astimezone(UTC)


def _minutes_between(bed_hour, bed_min, wake_hour, wake_min) -> int:
    bed = bed_hour * 60 + bed_min
    wake = wake_hour * 60 + wake_min
    if wake <= bed:
        wake += 24 * 60
    return wake - bed


def _gen_day(scenario: int, day_offset: int, days: int) -> dict:
    """单日单场景数值生成（day_offset: -13..0，0=今天）。"""
    jitter = rng.randint(-6, 6)
    rec: dict = {}

    # ---- 血压 ----
    if scenario == 0:  # 血压偏高型
        base_sbp = 132 + (day_offset % 5) * 4 + jitter
        base_dbp = 80 + (day_offset % 4) * 3 + jitter // 2
        if day_offset == -4:
            base_sbp, base_dbp = 168, 96  # → yellow（160–179）
        rec["bp_systolic"], rec["bp_diastolic"] = base_sbp, base_dbp
    elif scenario == 3:  # 偶发急警型（平时正常）
        rec["bp_systolic"] = 126 + (day_offset % 4) * 3 + jitter
        rec["bp_diastolic"] = 78 + (day_offset % 3) * 2 + jitter // 3
    else:
        rec["bp_systolic"] = 118 + (day_offset % 4) * 3 + jitter
        rec["bp_diastolic"] = 74 + (day_offset % 3) * 2 + jitter // 3

    # ---- 睡眠细维度 ----
    bed_h, bed_m = 21, rng.choice([25, 40, 50, 58])
    wake_h, wake_m = 6, rng.choice([10, 25, 35, 50])
    if scenario == 4:  # 情绪低落型入睡更晚
        bed_h, bed_m = 22, rng.choice([10, 30, 45])
    minutes = _minutes_between(bed_h, bed_m, wake_h, wake_m) + rng.randint(-12, 12)
    if scenario == 2 and day_offset >= -2:  # 近 3 天睡眠差（<300 → yellow）
        bed_h, bed_m = 23, rng.choice([5, 20, 35])
        wake_h, wake_m = 5, rng.choice([40, 50, 58])
        minutes = rng.randint(240, 280)  # 直接给值，勿用跨午夜重算（会回弹 ≥300）
    deep = int(minutes * rng.uniform(0.20, 0.26))
    light = int(minutes * rng.uniform(0.55, 0.65))
    if deep + light >= minutes:  # 一致性红线：deep+light < minutes
        light = minutes - deep - rng.randint(5, 15)
    awakenings = rng.randint(2, 4) if (scenario == 2) else rng.randint(0, 1)
    rec.update(
        sleep_bed_time=datetime.combine(
            _day_utc(day_offset) - timedelta(days=1), time(bed_h, bed_m), tzinfo=TZ_SH
        ).astimezone(UTC),  # 入睡在记录日前一晚
        sleep_wake_time=datetime.combine(
            _day_utc(day_offset), time(wake_h, wake_m), tzinfo=TZ_SH
        ).astimezone(UTC),
        sleep_minutes=minutes,
        sleep_deep_minutes=deep,
        sleep_light_minutes=light,
        sleep_awakenings=awakenings,
    )

    # ---- 情绪（1–5 整数字符串口径）----
    mood = rng.choice([3, 4, 4, 5])
    if scenario == 4 and day_offset in (-6, -9):
        mood = 2  # → yellow（≤2）
    rec["mood"] = str(mood)

    # ---- 主诉 ----
    if scenario == 3 and day_offset == 0:
        rec["chief_complaint"] = "昨晚胸口疼，有点闷"  # → red（主诉直达，含连续子串「胸口疼」）
    elif day_offset == -5 and scenario == 2:
        rec["chief_complaint"] = "有点头晕，睡得不太好"
    else:
        rec["chief_complaint"] = "没啥事"

    return rec


def _day_utc(day_offset: int) -> date:
    return (datetime.now(TZ_SH) + timedelta(days=day_offset)).date()


SCENARIO_NAMES = ["血压偏高", "平稳", "睡眠差", "偶发急警", "情绪低落"]


async def seed(days: int) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        elders = (
            (
                await session.execute(
                    select(Elder).where(Elder.status == ElderStatus.ACTIVE).order_by(Elder.name)
                )
            )
            .scalars()
            .all()
        )
        if not elders:
            raise SystemExit("[seed_health_days] 无在住长者，先跑基础种子")
        today = datetime.now(TZ_SH).date()
        window_start = _sh_noon_utc(today - timedelta(days=days - 1))

        # ---- 医生关联兜底（幂等：已有 active 关联的医生不动）----
        doctors = (
            (
                await session.execute(
                    select(Account).where(
                        Account.role == AccountRole.DOCTOR, Account.status == "active"
                    )
                )
            )
            .scalars()
            .all()
        )
        ensured = 0
        for doc in doctors:
            has_active = (
                await session.execute(
                    select(DoctorElderAssignment.assignment_id).where(
                        DoctorElderAssignment.account_id == doc.account_id,
                        DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
                    )
                )
            ).scalar_one_or_none()
            if has_active is None:
                for elder in elders:
                    session.add(
                        DoctorElderAssignment(
                            account_id=doc.account_id,
                            elder_id=elder.elder_id,
                            status=AssignmentStatus.ACTIVE,
                        )
                    )
                ensured += 1

        # ---- 14 天健康记录：先删窗口内旧种子，再按场景生成 ----
        await session.execute(
            delete(HealthRecord).where(
                HealthRecord.source == SOURCE,
                HealthRecord.recorded_at >= window_start,
                HealthRecord.elder_id.in_([e.elder_id for e in elders]),
            )
        )
        summary = []
        for index, elder in enumerate(elders):
            scenario = index % len(SCENARIO_NAMES)
            history: dict[str, list[int]] = {"sbp": [], "dbp": [], "sleep": []}
            levels = {"green": 0, "yellow": 0, "red": 0}
            for day_offset in range(-(days - 1), 1):
                rec = _gen_day(scenario, day_offset, days)

                def _avg(vals: list[int]) -> float | None:
                    return round(sum(vals) / len(vals), 1) if vals else None

                baseline = {
                    "sbp": _avg(history["sbp"]),
                    "dbp": _avg(history["dbp"]),
                    "sleep": _avg(history["sleep"]),
                }
                result = triage(
                    bp_systolic=rec.get("bp_systolic"),
                    bp_diastolic=rec.get("bp_diastolic"),
                    sleep_minutes=rec.get("sleep_minutes"),
                    mood=rec.get("mood"),
                    chief_complaint=rec.get("chief_complaint"),
                    baseline=baseline,
                )
                levels[result.level] += 1
                recorded_at = _sh_noon_utc(today + timedelta(days=day_offset))
                session.add(
                    HealthRecord(
                        elder_id=elder.elder_id,
                        recorded_at=recorded_at,
                        bp_systolic=rec.get("bp_systolic"),
                        bp_diastolic=rec.get("bp_diastolic"),
                        sleep_minutes=rec.get("sleep_minutes"),
                        mood=rec.get("mood"),
                        chief_complaint=rec.get("chief_complaint"),
                        source=SOURCE,
                        triage_level=result.level,
                        triage_reason=result.reason,
                        sleep_bed_time=rec.get("sleep_bed_time"),
                        sleep_wake_time=rec.get("sleep_wake_time"),
                        sleep_awakenings=rec.get("sleep_awakenings"),
                        sleep_deep_minutes=rec.get("sleep_deep_minutes"),
                        sleep_light_minutes=rec.get("sleep_light_minutes"),
                    )
                )
                history["sbp"].append(rec["bp_systolic"])
                history["dbp"].append(rec["bp_diastolic"])
                history["sleep"].append(rec["sleep_minutes"])
            summary.append(
                f"{elder.name}: 场景={SCENARIO_NAMES[scenario]} "
                f"green={levels['green']} yellow={levels['yellow']} red={levels['red']}"
            )
        await session.commit()
        print("[seed_health_days] 完成：")
        for line in summary:
            print("  " + line)
        print(f"  医生关联兜底：新建 {ensured} 位医生的关联")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Day8 健康 14 天种子")
    parser.add_argument("--days", type=int, default=14, choices=range(7, 31))
    asyncio.run(seed(parser.parse_args().days))


if __name__ == "__main__":
    main()
