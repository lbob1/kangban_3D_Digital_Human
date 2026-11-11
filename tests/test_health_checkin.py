"""Day8 步4：晚间健康外呼 4 问采集测试。

- 4 问状态机：逐问→逐答→结构化（HealthRecordIn 口径）→ run_triage（共用一图）；
- 适老红线：同一问题至多追问 1 轮；超时已采内容照常入库；
- 离线标记：D1 口径次日头插一问补昨晚睡眠（checkin_makeup 记录）；
- 下行互不干扰：health_checkin 事件 type 独立于 medication_reminder。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.scheduler as app_scheduler
from app.models import HealthRecord
from app.services import health_checkin
from app.services.health_checkin import (
    ACK_TEXT,
    MAKEUP_QUESTION,
    parse_bp,
    parse_mood,
    parse_sleep_minutes,
)


class _FakeFactory:
    """对应生产 `get_session_factory()()`：工厂调用后返回异步上下文会话。"""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *args):
        return False


class _FakeVoiceSession:
    def __init__(self):
        self.events: list[dict] = []
        self.spoken: list[str] = []

    async def send_event(self, ev):
        self.events.append(ev)

    async def _synthesize_and_send(self, text, capture_metrics=True):
        self.spoken.append(text)


@pytest.fixture(autouse=True)
def _clean_collectors():
    health_checkin._collectors.clear()
    health_checkin._pending_makeup.clear()
    yield
    health_checkin._collectors.clear()
    health_checkin._pending_makeup.clear()


@pytest.fixture
def _ck_path(monkeypatch, tmp_path):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "health_checkpoint_db", str(tmp_path / "ck.sqlite")
    )


async def _records(db_session, elder_id, source: str) -> list[HealthRecord]:
    rows = await db_session.execute(
        select(HealthRecord)
        .where(HealthRecord.elder_id == elder_id, HealthRecord.source == source)
    )
    return list(rows.scalars().all())


# ---- 确定性解析 ----


def test_parse_bp():
    assert parse_bp("130 85") == (130, 85)
    assert parse_bp("高压130，低压85") == (130, 85)
    assert parse_bp("一百三十、八十五") == (130, 85)
    assert parse_bp("两百二、一百一") == (220, 110)
    assert parse_bp("今天天气不错") is None


def test_parse_sleep():
    assert parse_sleep_minutes("六个半小时") == 390
    assert parse_sleep_minutes("睡了7个小时") == 420
    assert parse_sleep_minutes("五个钟头") == 300
    assert parse_sleep_minutes("三十分钟") == 30
    assert parse_sleep_minutes("没睡") is None


def test_parse_mood():
    assert parse_mood("4分") == 4
    assert parse_mood("五分") == 5
    assert parse_mood("挺好") == 4
    assert parse_mood("还行") == 3
    assert parse_mood("不太好") == 2
    assert parse_mood("很差") == 1
    assert parse_mood("嗯嗯") is None


# ---- 4 问全流程（green 零副作用） ----


async def test_full_checkin_flow(db_session, seed, monkeypatch, _ck_path):
    monkeypatch.setattr(
        health_checkin, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    elder = seed["elders"]["single"]
    fake = _FakeVoiceSession()

    assert await health_checkin.start_checkin(elder.elder_id, elder.name, fake)
    # 下行事件独立 type + 首问 TTS
    assert fake.events[0]["type"] == "health_checkin"
    assert fake.events[0]["text"] == fake.spoken[0]
    assert len(fake.events[0]["questions"]) == 4

    # 逐问逐答
    assert await health_checkin.feed_or_none(elder.elder_id, "130 85") == (
        health_checkin.QUESTIONS[1][1]
    )
    assert await health_checkin.feed_or_none(elder.elder_id, "六个半小时") == (
        health_checkin.QUESTIONS[2][1]
    )
    assert await health_checkin.feed_or_none(elder.elder_id, "4分") == (
        health_checkin.QUESTIONS[3][1]
    )
    assert await health_checkin.feed_or_none(elder.elder_id, "没啥事") == ACK_TEXT

    records = await _records(db_session, elder.elder_id, "checkin")
    assert len(records) == 1
    r = records[0]
    assert (r.bp_systolic, r.bp_diastolic) == (130, 85)
    assert r.sleep_minutes == 390
    assert r.mood == "4"
    assert r.chief_complaint is None  # 拒绝口径 → None
    assert r.triage_level == "green"  # 共用一图：规则引擎落级
    assert elder.elder_id not in health_checkin._collectors


# ---- 适老红线：同一问题至多追问 1 轮 ----


async def test_reprompt_at_most_once(db_session, seed, monkeypatch, _ck_path):
    monkeypatch.setattr(
        health_checkin, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    elder = seed["elders"]["single"]
    fake = _FakeVoiceSession()
    await health_checkin.start_checkin(elder.elder_id, elder.name, fake)

    # 第一轮没听清 → 追问一次
    assert await health_checkin.feed_or_none(elder.elder_id, "今天天气不错") == (
        health_checkin.QUESTIONS[0][2]
    )
    # 第二轮仍没听清 → 不再纠缠，置 None 进下一问
    assert await health_checkin.feed_or_none(elder.elder_id, "还是不想说") == (
        health_checkin.QUESTIONS[1][1]
    )
    # 快速走完
    await health_checkin.feed_or_none(elder.elder_id, "五个钟头")
    await health_checkin.feed_or_none(elder.elder_id, "还行")
    assert await health_checkin.feed_or_none(elder.elder_id, "没有") == ACK_TEXT

    records = await _records(db_session, elder.elder_id, "checkin")
    assert len(records) == 1
    r = records[0]
    assert r.bp_systolic is None and r.bp_diastolic is None  # 未采到 → None，不造数
    assert r.sleep_minutes == 300
    assert r.mood == "3"


# ---- 超时兜底：已采内容照常入库 ----


async def test_expired_finalizes_partial(db_session, seed, monkeypatch, _ck_path):
    monkeypatch.setattr(
        health_checkin, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    elder = seed["elders"]["single"]
    fake = _FakeVoiceSession()
    await health_checkin.start_checkin(elder.elder_id, elder.name, fake)
    await health_checkin.feed_or_none(elder.elder_id, "130 85")

    # 人为把会话拨到 TTL 之外
    health_checkin._collectors[elder.elder_id].started_at = datetime.now(UTC) - timedelta(
        minutes=11
    )
    # 下一句不再进采集（放行级联），过期内容先 finalize
    assert await health_checkin.feed_or_none(elder.elder_id, "昨晚睡了八小时") is None
    assert elder.elder_id not in health_checkin._collectors

    records = await _records(db_session, elder.elder_id, "checkin")
    assert len(records) == 1
    assert (records[0].bp_systolic, records[0].bp_diastolic) == (130, 85)
    assert records[0].sleep_minutes is None  # 超时未采 → None


# ---- tick：在线外呼 / 离线标记 / 次日头插一问 ----


async def test_tick_online_offline_and_makeup(db_session, seed, monkeypatch, _ck_path):
    elder = seed["elders"]["single"]
    sessions = {str(seed["devices"]["single"].device_id): _FakeVoiceSession()}
    monkeypatch.setattr(
        app_scheduler, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    monkeypatch.setattr(
        app_scheduler.voice_session_manager, "get", lambda did: sessions.get(str(did))
    )

    await app_scheduler.health_checkin_tick()
    # 在线长者开始采集；离线长者标记
    assert elder.elder_id in health_checkin._collectors
    assert len(health_checkin._pending_makeup) == 3

    # 清掉本次采集，模拟「次日」：把 d1 的离线标记拨到昨天，并让 d1 上线
    health_checkin._collectors.clear()
    elder_d1 = seed["elders"]["d1"]
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    health_checkin._pending_makeup = {elder_d1.elder_id: yesterday}
    sessions[str(seed["devices"]["double"].device_id)] = _FakeVoiceSession()
    await app_scheduler.health_checkin_tick()

    c = health_checkin._collectors[elder_d1.elder_id]
    assert c.questions[0][1] == MAKEUP_QUESTION[0]  # 头插一问
    assert len(c.questions) == 5

    # 走完 5 问 → 主记录 + 补问记录（锚定昨日 20:00 北京时间）
    monkeypatch.setattr(
        health_checkin, "get_session_factory", lambda: _FakeFactory(db_session)
    )
    await health_checkin.feed_or_none(elder_d1.elder_id, "五个小时")
    await health_checkin.feed_or_none(elder_d1.elder_id, "120 80")
    await health_checkin.feed_or_none(elder_d1.elder_id, "六个半小时")
    await health_checkin.feed_or_none(elder_d1.elder_id, "3分")
    assert await health_checkin.feed_or_none(elder_d1.elder_id, "膝盖有点酸") == ACK_TEXT

    makeup = await _records(db_session, elder_d1.elder_id, "checkin_makeup")
    assert len(makeup) == 1 and makeup[0].sleep_minutes == 300
    assert makeup[0].bp_systolic is None
    assert makeup[0].recorded_at.hour == 12  # 昨日 20:00 北京时间 == UTC 12 点
    main = await _records(db_session, elder_d1.elder_id, "checkin")
    assert len(main) == 1
    assert main[0].chief_complaint == "膝盖有点酸"
    assert main[0].sleep_minutes == 390  # 补问睡眠不混入主记录
    assert elder_d1.elder_id not in health_checkin._pending_makeup


# ---- 已在采集中的长者不重复外呼 ----


async def test_no_duplicate_call(db_session, seed, monkeypatch, _ck_path):
    elder = seed["elders"]["single"]
    fake = _FakeVoiceSession()
    assert await health_checkin.start_checkin(elder.elder_id, elder.name, fake)
    assert await health_checkin.start_checkin(elder.elder_id, elder.name, fake) is False
    assert len(fake.events) == 1
