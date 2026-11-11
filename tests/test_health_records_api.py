"""Day8 实时数据批次：健康数值登记端点 + 14 天总览端点测试。

走 kangban_test 真实库（事务回滚隔离）；写操作回查 DB（登记记录带分级落库）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import BuildingAssignment, HealthRecord
from app.models.enums import AssignmentStatus


async def _grant_nurse_building(db_session, seed, building_code: str = "A") -> None:
    """护工可见范围=楼栋→房间→长者：给 nurse_a 授权楼栋。"""
    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id,
            building_code=building_code,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


# ---- POST /health-records：登记同步入趋势 ----


async def _payload(**overrides) -> dict:
    base = {"bp_systolic": 130, "bp_diastolic": 85}
    base.update(overrides)
    return base


async def test_register_creates_record_with_triage(api, seed, auth_headers_factory, db_session):
    """护士登记数值 → 201 + 记录落库带分级（回查 DB）。"""
    headers = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await _grant_nurse_building(db_session, seed)  # A 楼（长者房间所在楼栋）
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json=await _payload(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["triage_level"] in ("green", "yellow", "red")
    record = (
        await db_session.execute(
            select(HealthRecord).where(HealthRecord.record_id == uuid.UUID(body["record_id"]))
        )
    ).scalar_one()
    assert record.source == "manual_register"  # 趋势数据源已同步
    assert record.triage_level == body["triage_level"]
    assert record.triage_reason


async def test_register_red_complaint_direct(api, seed, auth_headers_factory, db_session):
    """主诉含急症词 → red 直达（登记即分级）。"""
    headers = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await _grant_nurse_building(db_session, seed)
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json=await _payload(bp_systolic=120, chief_complaint="胸口疼"),
    )
    assert resp.status_code == 201
    assert resp.json()["triage_level"] == "red"


async def test_register_out_of_range_422(api, seed, auth_headers_factory, db_session):
    """数值越界 → 422（与 ORM CHECK 边界一致）。"""
    headers = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await _grant_nurse_building(db_session, seed)
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json=await _payload(bp_systolic=300),
    )
    assert resp.status_code == 422


async def test_register_empty_payload_422(api, seed, auth_headers_factory, db_session):
    headers = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await _grant_nurse_building(db_session, seed)
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json={"chief_complaint": "  "},
    )
    assert resp.status_code == 422


async def test_register_family_403_nurse_out_of_scope_404(api, seed, auth_headers_factory):
    """角色门禁：家属 403；护工范围外 404 隐藏存在性。"""
    elder = seed["elders"]["single"]
    fam = await auth_headers_factory("family")
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}", headers=fam, json=await _payload()
    )
    assert resp.status_code == 403

    other = seed["elders"]["other"]  # 护工 A 的楼栋外长者（B 楼）
    resp = await api.post(
        f"/api/v1/health-records?elder_id={other.elder_id}",
        headers=await auth_headers_factory("nurse_a"),
        json=await _payload(),
    )
    assert resp.status_code == 404


async def test_register_mood_stored_as_string(api, seed, auth_headers_factory, db_session):
    """mood 1–5 整数入库存整数字符串（口径一致）。"""
    headers = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await _grant_nurse_building(db_session, seed)
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json=await _payload(mood=2),
    )
    assert resp.status_code == 201
    assert resp.json()["triage_level"] == "yellow"  # 情绪 ≤2 → yellow
    record = (
        await db_session.execute(
            select(HealthRecord).where(
                HealthRecord.elder_id == elder.elder_id,
                HealthRecord.source == "manual_register",
            )
        )
    ).scalar_one()
    assert record.mood == "2"


# ---- GET /doctor/health-overview：全部关联长者汇总 ----


async def test_overview_multi_elder_and_missing_days(api, seed, auth_headers_factory, db_session):
    """医生有 1 位关联 → 总览仅含该长者；缺失天数正确；admin 全量。"""
    from app.models import DoctorElderAssignment
    from app.models.enums import AssignmentStatus

    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=seed["elders"]["single"].elder_id,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()
    elder = seed["elders"]["single"]
    db_session.add(
        HealthRecord(
            elder_id=elder.elder_id,
            recorded_at=datetime.now(UTC),
            bp_systolic=185,
            bp_diastolic=95,
            sleep_minutes=420,
            sleep_awakenings=2,
            mood="3",
            source="checkin",
            triage_level="red",
            triage_reason="收缩压185≥180",
        )
    )
    await db_session.flush()

    headers = await auth_headers_factory("doctor")
    resp = await api.get("/api/v1/doctor/health-overview?days=14", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1  # 仅关联长者
    it = items[0]
    assert it["elder_id"] == str(elder.elder_id)
    assert it["avg_sbp"] == 185
    assert it["avg_sleep"] == 420
    assert it["avg_awakenings"] == 2
    assert it["red_count"] == 1 and it["yellow_count"] == 0
    assert it["missing_days"] == 13  # 14 天窗口仅 1 天有数据
    assert len(it["sbp_series"]) == 14

    admin = await auth_headers_factory("admin")
    resp = await api.get("/api/v1/doctor/health-overview?days=14", headers=admin)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) >= 2  # admin 全量在住长者


async def test_overview_nurse_403(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("nurse_a")
    resp = await api.get("/api/v1/doctor/health-overview", headers=headers)
    assert resp.status_code == 403
