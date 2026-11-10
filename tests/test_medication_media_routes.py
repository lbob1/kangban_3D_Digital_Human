"""Day7 步5：medication/media 路由测试（kangban_test，事务回滚隔离）。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.auth.device_token import issue_access_token
from app.models import FamilyElderLink, MediaHistory, MediaLibrary, MedicationEvent
from app.models.enums import (
    MediaCopyrightStatus,
    MediaStatus,
    MedicationEventSource,
    MedicationEventStatus,
)
from tests.conftest import login

# ---- 用药计划 ----


def _plan_body(elder_id: uuid.UUID, **overrides) -> dict:
    body = {
        "elder_id": str(elder_id),
        "drug_name": "硝苯地平缓释片",
        "dose": "20mg",
        "schedule": {"slots": [{"time": "08:00", "label": "早"}]},
        "start_date": "2026-11-10",
    }
    body.update(overrides)
    return body


async def test_create_plan_admin_201(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "planned"
    assert data["schedule"]["slots"][0]["time"] == "08:00"


async def test_create_plan_nurse_403(api, seed):
    token = await login(api, "nurse_a", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_create_plan_elder_not_found_404(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(uuid.uuid4()),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_create_plan_bad_date_range_422(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id, end_date="2026-11-01"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_plan_empty_schedule_422(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id, schedule={}),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_list_plans_family_scoped(api, seed, db_session):
    token = await login(api, "admin", seed["password"])
    p1 = (
        await api.post(
            "/api/v1/medication/plans",
            json=_plan_body(seed["elders"]["single"].elder_id),
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["other"].elder_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    family = seed["accounts"]["family"]
    db_session.add(FamilyElderLink(account_id=family.account_id, elder_id=p1["elder_id"]))
    await db_session.flush()

    ftoken = await login(api, "family", seed["password"])
    resp = await api.get(
        "/api/v1/medication/plans", headers={"Authorization": f"Bearer {ftoken}"}
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["plan_id"] == p1["plan_id"]

    # 未关联长者按 id 过滤 → 空集
    resp = await api.get(
        f"/api/v1/medication/plans?elder_id={seed['elders']['other'].elder_id}",
        headers={"Authorization": f"Bearer {ftoken}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_events_family_403(api, seed):
    token = await login(api, "family", seed["password"])
    resp = await api.get(
        "/api/v1/medication/events", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_list_events_status_filter(api, seed, db_session):
    token = await login(api, "admin", seed["password"])
    plan = (
        await api.post(
            "/api/v1/medication/plans",
            json=_plan_body(seed["elders"]["single"].elder_id),
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    event = MedicationEvent(
        plan_id=uuid.UUID(plan["plan_id"]),
        elder_id=seed["elders"]["single"].elder_id,
        scheduled_at=datetime.now(UTC),
        status=MedicationEventStatus.ESCALATED,
        source=MedicationEventSource.SCHEDULER,
    )
    db_session.add(event)
    await db_session.flush()

    token = await login(api, "nurse_a", seed["password"])
    resp = await api.get(
        "/api/v1/medication/events?status=escalated",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["event_id"] == str(event.event_id)
    assert items[0]["status"] == "escalated"


# ---- 用药计划 CRUD（Day7+ 权限口径：doctor/admin 可写） ----


async def test_create_plan_doctor_201(api, seed):
    token = await login(api, "doctor", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "planned"


async def test_create_plan_family_403(api, seed):
    token = await login(api, "family", seed["password"])
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def _create_plan(api, seed, token, **overrides) -> dict:
    resp = await api.post(
        "/api/v1/medication/plans",
        json=_plan_body(seed["elders"]["single"].elder_id, **overrides),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_update_plan_doctor_200(api, seed):
    token = await login(api, "doctor", seed["password"])
    plan = await _create_plan(api, seed, token)
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"dose": "30mg", "instructions": "改为早餐后服用"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dose"] == "30mg"
    assert data["drug_name"] == plan["drug_name"]  # 未传字段不改动
    assert data["instructions"] == "改为早餐后服用"


async def test_update_plan_admin_200(api, seed):
    token = await login(api, "admin", seed["password"])
    plan = await _create_plan(api, seed, token)
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"status": "active"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


async def test_update_plan_nurse_403(api, seed):
    token = await login(api, "admin", seed["password"])
    plan = await _create_plan(api, seed, token)
    token = await login(api, "nurse_a", seed["password"])
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"dose": "30mg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_update_plan_family_403(api, seed):
    token = await login(api, "admin", seed["password"])
    plan = await _create_plan(api, seed, token)
    token = await login(api, "family", seed["password"])
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"dose": "30mg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_update_plan_not_found_404(api, seed):
    token = await login(api, "doctor", seed["password"])
    resp = await api.patch(
        f"/api/v1/medication/plans/{uuid.uuid4()}",
        json={"dose": "30mg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_update_plan_bad_status_422(api, seed):
    token = await login(api, "doctor", seed["password"])
    plan = await _create_plan(api, seed, token)
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"status": "deleted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_update_plan_bad_date_range_422(api, seed):
    token = await login(api, "doctor", seed["password"])
    plan = await _create_plan(api, seed, token)
    resp = await api.patch(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        json={"end_date": "2026-11-01"},  # 早于 start_date 2026-11-10
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_cancel_plan_soft_delete(api, seed, db_session):
    token = await login(api, "doctor", seed["password"])
    plan = await _create_plan(api, seed, token)
    resp = await api.delete(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    # 软删口径：行仍存在（证据链），仅 status 置 cancelled
    from app.models import MedicationPlan

    row = await db_session.get(MedicationPlan, uuid.UUID(plan["plan_id"]))
    assert row is not None
    assert row.status == "cancelled"


async def test_cancel_plan_nurse_403(api, seed):
    token = await login(api, "admin", seed["password"])
    plan = await _create_plan(api, seed, token)
    token = await login(api, "nurse_a", seed["password"])
    resp = await api.delete(
        f"/api/v1/medication/plans/{plan['plan_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_cancel_plan_not_found_404(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.delete(
        f"/api/v1/medication/plans/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---- 媒体库 ----


async def _device_headers(db_session, device_id) -> dict:
    issued = await issue_access_token(db_session, device_id)
    await db_session.commit()
    return {"Authorization": f"Bearer {issued.access_token}"}


async def _add_media(db_session, **overrides) -> MediaLibrary:
    defaults = {
        "type": "opera",
        "title": "梁山伯与祝英台（教学样例）",
        "copyright_status": MediaCopyrightStatus.TEACHING,
        "status": MediaStatus.ACTIVE,
        "media_uri": "/static/media/sample_opera.mp3",
    }
    defaults.update(overrides)
    media = MediaLibrary(**defaults)
    db_session.add(media)
    await db_session.flush()
    return media


async def test_media_library_requires_device_token(api, seed):
    token = await login(api, "admin", seed["password"])
    resp = await api.get(
        "/api/v1/media/library", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_media_library_active_only(api, seed, db_session):
    active = await _add_media(db_session)
    await _add_media(
        db_session,
        title="未授权素材",
        copyright_status=MediaCopyrightStatus.UNKNOWN,
        status=MediaStatus.HIDDEN,
        media_uri=None,
    )
    await _add_media(db_session, title="已授权但隐藏", status=MediaStatus.HIDDEN)

    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.get("/api/v1/media/library", headers=headers)
    assert resp.status_code == 200
    titles = [m["title"] for m in resp.json()]
    assert active.title in titles
    assert "未授权素材" not in titles
    assert "已授权但隐藏" not in titles


async def test_media_library_type_filter(api, seed, db_session):
    await _add_media(db_session, type="opera", title="越剧样例")
    await _add_media(db_session, type="movie", title="老电影样例")

    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.get("/api/v1/media/library?type=movie", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "老电影样例"


# ---- 播放上报 ----


async def test_report_play_resolves_elder_server_side(api, seed, db_session):
    media = await _add_media(db_session)
    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "duration_seconds": 30, "source": "ui"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # elder_id 来自服务端设备绑定解析（安全红线 8），非请求体
    assert data["elder_id"] == str(seed["elders"]["single"].elder_id)
    assert data["source"] == "ui"


async def test_report_play_ambiguous_room_409(api, seed, db_session):
    media = await _add_media(db_session)
    headers = await _device_headers(db_session, seed["devices"]["double"].device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "source": "voice_rule"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_report_play_inactive_media_422(api, seed, db_session):
    media = await _add_media(
        db_session,
        title="隐藏素材",
        copyright_status=MediaCopyrightStatus.UNKNOWN,
        status=MediaStatus.HIDDEN,
        media_uri=None,
    )
    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "source": "ui"},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_report_play_media_not_found_404(api, seed, db_session):
    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(uuid.uuid4()), "source": "ui"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_report_play_room_without_elder_404(api, seed, db_session):
    from app.models import Device, Room

    empty_room = Room(building_code="A", floor=9, room_number="901", bed_count=1)
    db_session.add(empty_room)
    await db_session.flush()
    lonely = Device(device_code="TAB-EMPTY", room_id=empty_room.room_id, status="active")
    db_session.add(lonely)
    await db_session.flush()
    media = await _add_media(db_session)
    headers = await _device_headers(db_session, lonely.device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "source": "ui"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_report_play_rejects_user_token(api, seed, db_session):
    media = await _add_media(db_session)
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "source": "ui"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_report_play_history_persisted(api, seed, db_session):
    media = await _add_media(db_session)
    headers = await _device_headers(db_session, seed["devices"]["single"].device_id)
    resp = await api.post(
        "/api/v1/media/history",
        json={"media_id": str(media.media_id), "duration_seconds": 0, "source": "voice_rule"},
        headers=headers,
    )
    assert resp.status_code == 201
    rows = (
        await db_session.execute(
            select(MediaHistory).where(
                MediaHistory.elder_id == seed["elders"]["single"].elder_id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].media_id == media.media_id
    assert rows[0].duration_seconds == 0
