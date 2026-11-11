"""Day8 睡眠数据批次：医生健康趋势接口测试（窗口对齐/同日取末条/权限门禁/字段口径）。

走 kangban_test 真实库（事务回滚隔离）；时间基准=北京时间口径（服务端转换）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import DoctorElderAssignment, HealthRecord
from app.models.enums import AssignmentStatus

TZ_SH = ZoneInfo("Asia/Shanghai")


async def _grant_doctor(db_session, seed, elder) -> None:
    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=elder.elder_id,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def _add_record(db_session, elder, *, days_ago: int, hour: int = 20, **fields) -> None:
    recorded_at = datetime.now(TZ_SH) - timedelta(days=days_ago)
    recorded_at = recorded_at.replace(hour=hour, minute=0, second=0, microsecond=0)
    db_session.add(
        HealthRecord(
            elder_id=elder.elder_id,
            recorded_at=recorded_at.astimezone(UTC),
            source=fields.pop("source", "checkin"),
            **fields,
        )
    )
    await db_session.flush()


# ---- 窗口与数据形状 ----


async def test_full_window_with_same_day_last_record(api, seed, auth_headers_factory, db_session):
    """完整窗口输出（含缺失日 null）；同日多条取最后一条（外呼+查房并存）。"""
    headers = await auth_headers_factory("admin")
    elder = seed["elders"]["single"]
    await _add_record(
        db_session, elder, days_ago=0, hour=8,
        bp_systolic=120, bp_diastolic=80, mood="4",
    )
    await _add_record(
        db_session, elder, days_ago=0, hour=20,  # 同日更晚 → 应覆盖 8 点那条
        bp_systolic=125, bp_diastolic=83, mood="3", sleep_minutes=420,
        sleep_deep_minutes=95, sleep_light_minutes=280, sleep_awakenings=1,
    )
    await _add_record(
        db_session, elder, days_ago=5,
        bp_systolic=166, bp_diastolic=95, chief_complaint="有点头晕",
    )
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}&days=14", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == 14
    assert len(body["dates"]) == 14  # 完整窗口，缺失日 null 断点
    assert len(body["bp_systolic"]) == 14
    assert len(body["sleep"]) == 14
    today_idx = 13
    assert body["bp_systolic"][today_idx] == 125  # 同日取最后一条
    assert body["bp_diastolic"][today_idx] == 83
    assert body["mood"][today_idx] == 3
    last = body["sleep"][today_idx]
    assert last["minutes"] == 420 and last["deep"] == 95 and last["light"] == 280
    assert last["awakenings"] == 1
    assert last["bed_time"] is None  # 未采集为 null，不编造
    assert body["bp_systolic"][today_idx - 5] == 166  # 5 天前命中
    assert any(c["text"] == "有点头晕" for c in body["complaints"])
    assert body["elder_name"] == elder.name


async def test_bed_wake_time_beijing_hhmm(api, seed, auth_headers_factory, db_session):
    """入睡/起床时刻输出北京时间 HH:MM（用户全局规则）。"""
    headers = await auth_headers_factory("admin")
    elder = seed["elders"]["single"]
    bed = (datetime.now(TZ_SH) - timedelta(days=1)).replace(
        hour=22, minute=40, second=0, microsecond=0
    )
    wake = bed + timedelta(hours=8, minutes=15)
    await _add_record(db_session, elder, days_ago=0)
    rec = (
        await db_session.execute(
            select(HealthRecord).where(HealthRecord.elder_id == elder.elder_id)
        )
    ).scalar_one()
    rec.sleep_bed_time = bed.astimezone(UTC)
    rec.sleep_wake_time = wake.astimezone(UTC)
    rec.sleep_minutes = 495
    await db_session.flush()

    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}&days=7", headers=headers
    )
    assert resp.status_code == 200
    today = resp.json()["sleep"][-1]
    assert today["bed_time"] == "22:40"
    assert today["wake_time"] == "06:55"


async def test_invalid_mood_parsed_to_null(api, seed, auth_headers_factory, db_session):
    """mood 非整数字符串 → null（与规则引擎同口径），不编造分值。"""
    headers = await auth_headers_factory("admin")
    elder = seed["elders"]["single"]
    await _add_record(db_session, elder, days_ago=0, mood="abc")
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}&days=7", headers=headers
    )
    assert resp.json()["mood"][-1] is None


async def test_days_param_clamped(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    elder = seed["elders"]["single"]
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}&days=5", headers=headers
    )
    assert resp.status_code == 422  # ge=7
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}&days=31", headers=headers
    )
    assert resp.status_code == 422  # le=30


# ---- 权限门禁（楼栋/关联/角色三层）----


async def test_doctor_without_assignment_404(api, seed, auth_headers_factory):
    """医生无关联 → 404 隐藏存在性（get_elder_for_user 语义）。"""
    headers = await auth_headers_factory("doctor")
    elder = seed["elders"]["single"]
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}", headers=headers
    )
    assert resp.status_code == 404


async def test_doctor_with_assignment_200(api, seed, auth_headers_factory, db_session):
    headers = await auth_headers_factory("doctor")
    elder = seed["elders"]["single"]
    await _grant_doctor(db_session, seed, elder)
    await _add_record(db_session, elder, days_ago=0, bp_systolic=130, bp_diastolic=85)
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["bp_systolic"][-1] == 130


async def test_nurse_and_family_403(api, seed, auth_headers_factory):
    """角色门禁：nurse/family 不在 trends 可见角色（403，无论有无关联）。"""
    elder = seed["elders"]["single"]
    for username in ("nurse_a", "family"):
        headers = await auth_headers_factory(username)
        resp = await api.get(
            f"/api/v1/doctor/health-trends?elder_id={elder.elder_id}", headers=headers
        )
        assert resp.status_code == 403, username


async def test_unknown_elder_404(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    resp = await api.get(
        f"/api/v1/doctor/health-trends?elder_id={uuid.uuid4()}", headers=headers
    )
    assert resp.status_code == 404
