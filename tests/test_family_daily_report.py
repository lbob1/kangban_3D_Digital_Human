"""Day8 步8：家属每日日报端点测试（摘要/用药计数/权限/TTS 降级）。

走 kangban_test 真实库（事务回滚隔离）；TTS 合成不打网络——打桩 _synthesize_mp3。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import FamilyElderLink, HealthRecord, MedicationEvent
from app.models.enums import AssignmentStatus


@pytest.fixture(autouse=True)
def _clean_audio_cache():
    """音频缓存按测试隔离（内存 dict，模块级共享）。"""
    from app.api.routes import family as family_route

    family_route._AUDIO_CACHE.clear()
    yield
    family_route._AUDIO_CACHE.clear()


async def _link_family(db_session, seed, elder) -> None:
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=elder.elder_id,
            relation="child",
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def test_daily_report_family_linked(api, seed, auth_headers_factory, db_session):
    """已关联家属：当日最新一条记录的分级/血压/睡眠/情绪 + 用药计数 + 正文同源。"""
    await _link_family(db_session, seed, seed["elders"]["single"])
    elder = seed["elders"]["single"]
    now = datetime.now(UTC)
    from app.models import MedicationPlan

    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name="降压片",
        dose="1片",
        schedule={"slots": [{"time": "08:00"}]},
        start_date=now.date(),
        status="active",
    )
    db_session.add(plan)
    await db_session.flush()
    db_session.add_all(
        [
            HealthRecord(
                elder_id=elder.elder_id,
                recorded_at=now - timedelta(minutes=5),  # 较早一条，应被最新覆盖
                bp_systolic=185,
                bp_diastolic=95,
                sleep_minutes=420,
                sleep_awakenings=2,
                mood="3",
                source="checkin",
                triage_level="red",
                triage_reason="收缩压185≥180",
            ),
            # 当日多条：取最新一条（同医生趋势口径）
            HealthRecord(
                elder_id=elder.elder_id,
                recorded_at=now,
                bp_systolic=140,
                bp_diastolic=88,
                mood="2",
                source="manual_register",
                triage_level="yellow",
                triage_reason="情绪≤2",
            ),
            MedicationEvent(
                plan_id=plan.plan_id,
                elder_id=elder.elder_id,
                scheduled_at=now,
                status="confirmed",
                source="scheduler",
            ),
            MedicationEvent(
                plan_id=plan.plan_id,
                elder_id=elder.elder_id,
                scheduled_at=now,
                status="escalated",
                source="scheduler",
            ),
        ]
    )
    await db_session.flush()

    resp = await api.get(
        f"/api/v1/family/daily-report?elder_id={elder.elder_id}",
        headers=await auth_headers_factory("family"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_record"] is True
    assert body["triage_level"] == "yellow"  # 最新一条
    assert body["bp_systolic"] == 140 and body["bp_diastolic"] == 88
    assert body["mood"] == 2
    assert body["medication"]["total"] == 2
    assert body["medication"]["confirmed"] == 1
    assert body["medication"]["escalated"] == 1
    assert "今日日报" in body["summary_text"]
    assert "需要多加留意" in body["summary_text"]  # yellow 文案
    assert "已升级人工跟进" in body["summary_text"]


async def test_daily_report_no_record(api, seed, auth_headers_factory, db_session):
    """无记录当日：has_record=False，正文提示暂无记录（不造数）。"""
    await _link_family(db_session, seed, seed["elders"]["single"])
    elder = seed["elders"]["single"]
    resp = await api.get(
        f"/api/v1/family/daily-report?elder_id={elder.elder_id}",
        headers=await auth_headers_factory("family"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_record"] is False
    assert body["triage_level"] is None
    assert "还没有健康记录" in body["summary_text"]


async def test_daily_report_scope_404_role_403(api, seed, auth_headers_factory):
    """家属未关联 404（隐藏存在性）；nurse 403。"""
    elder = seed["elders"]["single"]
    fam = await auth_headers_factory("family")
    resp = await api.get(f"/api/v1/family/daily-report?elder_id={elder.elder_id}", headers=fam)
    assert resp.status_code == 404

    nurse = await auth_headers_factory("nurse_a")
    resp = await api.get(f"/api/v1/family/daily-report?elder_id={elder.elder_id}", headers=nurse)
    assert resp.status_code == 403


async def test_daily_report_audio_cached_and_failure_503(
    api, seed, auth_headers_factory, db_session, monkeypatch
):
    """语音条：合成成功缓存当日一份（二次命中不重合成）；TTS 失败 503 降级。"""
    from app.api.routes import family as family_route

    await _link_family(db_session, seed, seed["elders"]["single"])
    elder = seed["elders"]["single"]
    fam_headers = await auth_headers_factory("family")
    url = f"/api/v1/family/daily-report/audio?elder_id={elder.elder_id}"

    calls = {"n": 0}

    async def fake_synth(text: str) -> bytes:
        calls["n"] += 1
        return b"fake-mp3-" + str(calls["n"]).encode()

    monkeypatch.setattr(family_route, "_synthesize_mp3", fake_synth)
    resp = await api.get(url, headers=fam_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content.startswith(b"fake-mp3-1")
    resp = await api.get(url, headers=fam_headers)
    assert resp.content.startswith(b"fake-mp3-1")  # 缓存命中，未重合成
    assert calls["n"] == 1

    # TTS 失败 → 503（前端降级文字）
    async def boom(text: str) -> bytes:
        raise RuntimeError("network down")

    monkeypatch.setattr(family_route, "_synthesize_mp3", boom)
    elder2 = seed["elders"]["d1"]
    await _link_family(db_session, seed, elder2)
    resp = await api.get(
        f"/api/v1/family/daily-report/audio?elder_id={elder2.elder_id}", headers=fam_headers
    )
    assert resp.status_code == 503
