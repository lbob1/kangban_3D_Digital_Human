"""Day7 步8：娱乐点播全链路测试（导入幂等 / 偏好优先 / voice_rule 留痕 / 换一段）。"""

import uuid

from sqlalchemy import select

from app.models import MediaHistory, MediaLibrary, MedicationPlan, MemoryChunk
from app.models.enums import (
    MediaCopyrightStatus,
    MediaPlaySource,
    MediaStatus,
    MedicationPlanStatus,
    MemoryCategory,
)
from app.router.cascade import RuntimeBridge, get_cascade_router
from app.services.media import pick_for_elder
from scripts.import_media import SAMPLES, import_samples
from scripts.seed_day7 import GENRES, ensure_for_elder
from tests.test_medication_dialog import _FakeFactory

# ---- 样例音频 / 导入幂等 ----


def test_generate_samples_wav(tmp_path, monkeypatch):
    from scripts import import_media as im

    monkeypatch.setattr(im, "STATIC_MEDIA_DIR", tmp_path)
    im.generate_all_samples()
    im.generate_all_samples()  # 幂等：已存在跳过
    assert len(list(tmp_path.glob("*.wav"))) == len(SAMPLES)
    for s in SAMPLES:
        p = tmp_path / s["media_uri"].split("/")[-1]
        assert p.stat().st_size > 1000


async def test_import_samples_idempotent(db_session):
    first = await import_samples(db_session)
    second = await import_samples(db_session)

    assert first["inserted"] == len(SAMPLES)
    assert second["inserted"] == 0
    rows = ((await db_session.execute(select(MediaLibrary))).scalars().all())
    assert len(rows) == len(SAMPLES)


async def test_import_marks_copyright(db_session):
    await import_samples(db_session)
    rows = ((await db_session.execute(select(MediaLibrary))).scalars().all())
    statuses = {r.copyright_status for r in rows}
    assert statuses <= {"teaching", "public_domain"}  # 零 unknown 入库


# ---- seed_day7 幂等（Day7+：默认覆盖全部在住长者，单长者接口 ensure_for_elder） ----


async def test_seed_day7_idempotent(db_session, seed):
    elder = seed["elders"]["single"]
    pref_content = f"喜欢听{GENRES[0]}"
    first = await ensure_for_elder(db_session, elder, 0, [])
    second = await ensure_for_elder(db_session, elder, 0, [])

    assert first["preference"] == pref_content  # 新插入时返回偏好内容
    assert second["preference"] == "exists"
    assert first["plans"] == "+3"
    assert second["plans"] == "+0"
    assert first["history"] == "exists"  # 无曲库候选时不建播放留痕
    plans = (
        (
            await db_session.execute(
                select(MedicationPlan).where(
                    MedicationPlan.elder_id == elder.elder_id,
                    MedicationPlan.status == MedicationPlanStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(plans) == 3
    prefs = (
        (
            await db_session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.elder_id == elder.elder_id,
                    MemoryChunk.content == pref_content,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(prefs) == 1


# ---- 选曲：偏好优先 / 无候选 ----


def _add_media(db, **kw):
    defaults = {
        "type": "opera",
        "title": "样例曲目",
        "copyright_status": MediaCopyrightStatus.TEACHING,
        "status": MediaStatus.ACTIVE,
        "media_uri": "/static/media/sample.wav",
    }
    defaults.update(kw)
    media = MediaLibrary(**defaults)
    db.add(media)
    return media


async def test_pick_prefers_preference_keyword(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    jingju = _add_media(db_session, title="穆桂英挂帅", genre="京剧")
    yueju = _add_media(db_session, title="梁祝选段", genre="越剧")
    await db_session.flush()
    db_session.add(
        MemoryChunk(
            elder_id=elder_id,
            content="喜欢听越剧",
            embedding=[0.0] * 1024,
            category=MemoryCategory.PREFERENCE,
        )
    )
    await db_session.flush()

    picked = await pick_for_elder(db_session, elder_id=elder_id)
    assert picked.media_id == yueju.media_id
    assert picked.media_id != jingju.media_id


async def test_pick_no_candidate_returns_none(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    _add_media(
        db_session,
        title="未授权素材",
        copyright_status=MediaCopyrightStatus.UNKNOWN,
        status=MediaStatus.HIDDEN,
        media_uri=None,
    )
    await db_session.flush()
    picked = await pick_for_elder(db_session, elder_id=elder_id)
    assert picked is None


# ---- 对话点播：rule 层真播放 ----


def _bridge(elder_id, db_session, events):
    return RuntimeBridge.set(
        {
            "elder_id": str(elder_id),
            "db_factory": _FakeFactory(db_session),
            "send_event": _record_send(events),
        }
    )


def _record_send(events):
    async def send_event(payload):
        events.append(payload)

    return send_event


async def test_dialog_opera_play_voice_rule(db_session, seed, monkeypatch):
    """「听一段越剧」：偏好优先选曲 → media_play 下行 → media_history(voice_rule)。"""
    elder_id = seed["elders"]["single"].elder_id
    await import_samples(db_session)
    db_session.add(
        MemoryChunk(
            elder_id=elder_id,
            content="喜欢听越剧",
            embedding=[0.0] * 1024,
            category=MemoryCategory.PREFERENCE,
        )
    )
    await db_session.flush()
    events: list = []
    token = _bridge(elder_id, db_session, events)
    try:
        outcome = await get_cascade_router().decide("听一段越剧")
    finally:
        RuntimeBridge.reset(token)

    assert outcome.route_layer == "rule"
    assert "为您播放" in outcome.reply_text
    assert len(events) == 1
    assert events[0]["type"] == "media_play"
    assert events[0]["media_uri"].endswith(".wav")
    history = (
        (
            await db_session.execute(
                select(MediaHistory).where(MediaHistory.elder_id == elder_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(history) == 1
    assert history[0].source == MediaPlaySource.VOICE_RULE
    assert history[0].media_id == uuid.UUID(events[0]["media_id"])


async def test_dialog_media_next_excludes_current(db_session, seed, monkeypatch):
    """「换一段」：排除最近播放曲目，选中另一条。"""
    elder_id = seed["elders"]["single"].elder_id
    await import_samples(db_session)
    events: list = []
    token = _bridge(elder_id, db_session, events)
    try:
        await get_cascade_router().decide("听一段越剧")
        second = await get_cascade_router().decide("换一段")
    finally:
        RuntimeBridge.reset(token)

    assert "换一首" in second.reply_text
    first_id, second_id = events[0]["media_id"], events[1]["media_id"]
    assert first_id != second_id
    history = (
        (
            await db_session.execute(
                select(MediaHistory)
                .where(MediaHistory.elder_id == elder_id)
                .order_by(MediaHistory.played_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(history) == 2
    assert str(history[-1].media_id) == second_id


async def test_dialog_media_play_requires_verified_elder(db_session, seed, monkeypatch):
    await import_samples(db_session)
    await db_session.flush()
    outcome = await get_cascade_router().decide("听一段越剧")

    assert outcome.route_layer == "rule"
    assert "声纹" in outcome.reply_text
    assert "梁祝" not in outcome.reply_text


async def test_dialog_media_play_empty_library(db_session, seed, monkeypatch):
    elder_id = seed["elders"]["single"].elder_id
    token = _bridge(elder_id, db_session, [])
    try:
        outcome = await get_cascade_router().decide("听一段越剧")
    finally:
        RuntimeBridge.reset(token)

    assert "曲库正在整理" in outcome.reply_text
