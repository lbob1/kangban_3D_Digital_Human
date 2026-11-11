"""晚间健康外呼 4 问采集（Day8 步4，主文档 §4.3）。

链路：scheduler 20:00 tick → WS 下行 health_checkin（互不干扰：type 独立于
medication_reminder）→ 数字人逐问（TTS）→ 老人 ASR 逐答 → voice.py 在级联前
拦截进本状态机 → 4 答结构化（HealthRecordIn 口径）→ 落 health_records
（source="checkin"）→ run_triage（三通道共用一图，大纲 8.1 红线）。

纪律与边界：
- 适老红线：同一问题至多追问 1 轮，再听不清就置 None 继续（绝不纠缠）；
- 超时/拒绝：该字段 None 入库，不造数；TTL 过期在下次交互或 tick 兜底 finalize；
- 设备离线：标记 + 次日外呼头插一问（补昨晚睡眠），不重建外呼调度
  （与大纲字面差异：晨间补问收敛为次日 20:00 头插一问，如实记入文档）；
- 结构化：确定性解析先行（可测、零 prompt 依赖）；LLM 已配置时对解析失败的
  原话做一次 chat_with_tools 兜底抽取（失败静默回退，绝不阻塞采集）；
- 阈值永不进 prompt（分级纪律）：本模块只采集不分级，分级一律走健康图规则引擎；
- 话术红线：收尾确认话术零诊断词、零分级词。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.db.session import get_session_factory

logger = get_logger("services.health_checkin")

_TZ_SH = ZoneInfo("Asia/Shanghai")


def _cst(day, hour: int, minute: int):
    """某日北京时间 hour:minute 的 aware datetime（补问记录锚点）。"""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_TZ_SH)

CHECKIN_TTL = timedelta(minutes=10)

# 4 问话术（适老：短句、单问、给示例）；(key, 首问, 追问一次的话术)
QUESTIONS: list[tuple[str, str, str]] = [
    (
        "bp",
        "咱们聊聊今天的健康情况。您今天量血压了吗？高压和低压分别是多少？",
        "不好意思，没听清楚。您再说一遍高压和低压，比如说一百三、八十五。",
    ),
    (
        "sleep",
        "昨晚睡了大概几个小时？",
        "没听清，您再说一遍昨晚睡了几个小时。",
    ),
    (
        "mood",
        "今天心情怎么样？给您的心情打个分，1 到 5 分，5 分最好。",
        "您再说一个 1 到 5 的分数就行。",
    ),
    (
        "complaint",
        "今天有没有哪里不舒服？",
        "您再说一遍，今天有没有哪里不舒服？",
    ),
]

MAKEUP_QUESTION = (
    "对了，昨天咱们没联系上，您前一个晚上睡了大概几个小时？",
    "没听清，您再说一遍那晚睡了几个小时。",
)

ACK_TEXT = "好，都帮您记下来啦。您早点休息，晚安。"

# 拒绝/无主诉口径（主诉置 None 入库）
_REFUSAL = re.compile(r"没啥事|没事|没有|不想说|不告诉你|没什么")


# ---------------- 确定性解析（可测、零 prompt 依赖） ----------------

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = re.compile(r"[零一二两三四五六七八九十百]+")


def _cn_to_arabic(text: str) -> str:
    """中文数字段 → 阿拉伯数字（支持到百位：一百三十 → 130）。"""

    def _seg(m: re.Match) -> str:
        s = m.group(0)
        if "百" in s:
            b, rest = s.split("百", 1)
            hundred = _CN_DIGIT.get(b, 1) if b else 1
            if not rest:  # 「两百」= 200
                return str(hundred * 100)
            if rest.startswith("零"):  # 「一百零五」= 105
                return str(hundred * 100 + _seg_tail(rest[1:]))
            if "十" not in rest and len(rest) == 1:
                # 口语省略「十」：「两百二」= 220、「一百一」= 110
                return str(hundred * 100 + _CN_DIGIT.get(rest, 0) * 10)
            return str(hundred * 100 + _seg_tail(rest))
        return str(_seg_tail(s))

    def _seg_tail(s: str) -> int:
        if not s:
            return 0
        if "十" in s:
            left, _, right = s.partition("十")
            tens = _CN_DIGIT.get(left, 1) if left else 1
            ones = _CN_DIGIT.get(right, 0) if right else 0
            return tens * 10 + ones
        return _CN_DIGIT.get(s, 0)

    return _CN_NUM.sub(_seg, text)


def _first_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def parse_bp(text: str) -> tuple[int, int] | None:
    """「130 85 / 130/85 / 一百三、八十五」→ (130, 85)；解析不出 → None。"""
    t = _cn_to_arabic(text)
    nums = [int(n) for n in re.findall(r"\d{2,3}", t)]
    for i in range(len(nums) - 1):
        a, b = nums[i], nums[i + 1]
        if 50 <= a <= 250 and 30 <= b <= 150:
            return a, b
    return None


def parse_sleep_minutes(text: str) -> int | None:
    """「六个半小时 / 睡了7个小时 / 五个钟头 / 三十分钟」→ 分钟。"""
    t = _cn_to_arabic(text)
    # 「X个半 + 小时/钟头」
    m = re.search(r"(\d+)\s*个\s*半\s*(?:小时|钟头|h)?", t)
    if m:
        return int((int(m.group(1)) + 0.5) * 60)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时|钟头|h)", t)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"(\d+)\s*(?:个)?\s*分?钟", t)
    if m:
        return int(m.group(1))
    # 无单位兜底：≤24 视为小时
    v = _first_int(t)
    if v is not None and 1 <= v <= 24:
        return v * 60
    return None


def parse_mood(text: str) -> int | None:
    """「4分 / 四分 / 挺好 / 还行 / 不太好 / 很差」→ 1–5。"""
    t = _cn_to_arabic(text)
    v = _first_int(t)
    if v is not None and 1 <= v <= 5:
        return v
    if "很好" in t or "很开心" in t or "非常好" in t:
        return 5
    if "挺好" in t or "不错" in t:
        return 4
    if "还行" in t or "还可以" in t or "一般" in t:
        return 3
    if "不太好" in t or "不太开心" in t:
        return 2
    if "很差" in t or "糟糕" in t or "很难受" in t:
        return 1
    # 中文单字分（如「五分」在无空格口语里）
    if "分" in t:
        for ch, num in _CN_DIGIT.items():
            if num and ch in t:
                return num
    return None


# ---------------- 采集会话状态机（进程内存，重启即弃——演示口径） ----------------


@dataclass
class _Collector:
    elder_id: UUID
    elder_name: str
    questions: list[tuple[str, str, str]]  # (key, 首问, 追问)
    idx: int = 0
    retries: int = 0
    raw: dict[str, str] = field(default_factory=dict)  # key → 老人原话
    parsed: dict[str, Any] = field(default_factory=dict)  # key → 结构化值（None=未采到）
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    makeup_date: object | None = None  # 补问记录锚定日（缺失外呼那天）


_collectors: dict[UUID, _Collector] = {}
# 设备离线标记：elder_id → 缺失外呼日期（次日 20:00 头插一问补昨晚睡眠）
_pending_makeup: dict[UUID, "datetime"] = {}


def _parse_answer(key: str, text: str) -> Any:
    if key == "bp":
        return parse_bp(text)
    if key in ("sleep", "makeup_sleep"):
        return parse_sleep_minutes(text)
    if key == "mood":
        return parse_mood(text)
    # complaint：拒绝口径 → None；其余原话存主诉
    cleaned = re.sub(r"[\x00-\x1f]+", " ", text).strip()[:200]
    return None if (not cleaned or _REFUSAL.search(cleaned)) else cleaned


async def start_checkin(
    elder_id: UUID, elder_name: str, session: Any, *, makeup_date: Any | None = None
) -> bool:
    """发起外呼：WS 下行 health_checkin 元数据 + TTS 首问。已在采集中的长者跳过。"""
    if elder_id in _collectors:
        return False
    questions = list(QUESTIONS)
    if makeup_date is not None:
        questions = [("makeup_sleep", MAKEUP_QUESTION[0], MAKEUP_QUESTION[1])] + questions
    collector = _Collector(
        elder_id=elder_id, elder_name=elder_name, questions=questions, makeup_date=makeup_date
    )
    _collectors[elder_id] = collector
    first = questions[0][1]
    await session.send_event(
        {
            "type": "health_checkin",
            "elder_name": elder_name,
            "questions": [q[1] for q in questions],
            "text": first,
        }
    )
    # noqa 话术：与 scheduler.ws_notifier 同口径（跨层私有调用已评审）
    await session._synthesize_and_send(first, capture_metrics=False)  # noqa: SLF001
    logger.info(
        "health checkin started",
        extra={"event": "health_checkin_start", "elder_id": str(elder_id),
               "makeup": makeup_date is not None},
    )
    return True


async def feed_or_none(elder_id: UUID | None, text: str) -> str | None:
    """外呼进行中 → 处理本句答案并返回下一问/收尾话术；无会话 → None（走级联）。"""
    collector = _collectors.get(elder_id) if elder_id else None
    if collector is None:
        return None
    if datetime.now(UTC) - collector.started_at > CHECKIN_TTL:
        # 超时兜底：已采到的照常入库（缺失字段 None），本句放行级联
        await _finalize(collector)
        return None

    key, _q, reprompt = collector.questions[collector.idx]
    value = _parse_answer(key, text)
    # complaint 的 None 是有效回答（拒绝口径 → 主诉置 None 入库）
    if value is not None or key == "complaint":
        collector.raw[key] = text
        collector.parsed[key] = value
        collector.idx += 1
        collector.retries = 0
    elif collector.retries < 1:
        # 适老红线：同一问题至多追问 1 轮
        collector.retries += 1
        return reprompt
    else:
        collector.parsed[key] = None
        collector.idx += 1
        collector.retries = 0

    if collector.idx < len(collector.questions):
        return collector.questions[collector.idx][1]
    await _finalize(collector)
    return ACK_TEXT


async def finalize_expired() -> int:
    """tick 兜底：TTL 过期的采集会话按已采内容入库。返回收尾条数。"""
    now = datetime.now(UTC)
    expired = [c for c in _collectors.values() if now - c.started_at > CHECKIN_TTL]
    for c in expired:
        await _finalize(c)
    return len(expired)


# ---------------- 结构化落库（Pydantic 口径 + LLM 兜底抽取） ----------------


def _deterministic_record(collector: _Collector) -> dict:
    """主记录结构化（补问睡眠不混入主记录，走 makeup 记录）。"""
    p = collector.parsed
    bp = p.get("bp")
    return {
        "bp_systolic": bp[0] if bp else None,
        "bp_diastolic": bp[1] if bp else None,
        "sleep_minutes": p.get("sleep"),
        "mood": p.get("mood"),
        "chief_complaint": p.get("complaint"),
    }


async def _llm_rescue(collector: _Collector, fields: dict) -> None:
    """LLM 已配置时，对确定性解析为 None 的字段用原话做一次工具化抽取。

    阈值不进 prompt：LLM 只做格式归一，不做任何分级/判断；失败静默回退。
    """
    if not any(v is None for v in fields.values()):
        return
    try:
        from app.integrations.llm import chat_with_tools, is_configured

        if not is_configured():
            return
        missing = [k for k, v in fields.items() if v is None]
        raw_texts = {k: collector.raw.get(k, "") for k in missing if collector.raw.get(k)}
        if not raw_texts:
            return
        tool = {
            "type": "function",
            "function": {
                "name": "save_health_record",
                "description": "把长者的回答转成结构化健康数值；听不出的字段留空，禁止编造",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "bp_systolic": {"type": "integer"},
                        "bp_diastolic": {"type": "integer"},
                        "sleep_minutes": {"type": "integer"},
                        "mood": {"type": "integer"},
                        "chief_complaint": {"type": "string"},
                    },
                },
            },
        }
        messages = [
            {
                "role": "system",
                "content": "你是健康采集助手：只把原话中的数值转成参数，缺失字段不要编造。",
            },
            {"role": "user", "content": json.dumps(raw_texts, ensure_ascii=False)},
        ]
        decision = await chat_with_tools(messages, [tool])
        tcs = (decision or {}).get("tool_calls") or []
        if not tcs:
            return
        args = tcs[0].get("arguments") or {}
        from pydantic import ValidationError

        from app.schemas.health_records import HealthRecordIn

        try:
            fill = {k: v for k, v in args.items() if k in fields and fields[k] is None}
            payload = HealthRecordIn(**fill)
        except ValidationError:
            return
        for k, v in payload.model_dump(exclude_none=True).items():
            if fields.get(k) is None:
                fields[k] = v
    except Exception:  # 采集链路绝不因兜底抽取中断
        logger.warning("llm rescue failed", extra={"event": "health_checkin_llm_rescue_fail"})


def _noop_notifier(state: dict) -> Any:
    """外呼通道占位通知器：yellow/red 挂 interrupt，确认后由 resume 端点传真实
    notifier（步 6 接线）；初始调用前不会触发。"""
    logger.warning("health checkin default notifier fired",
                   extra={"event": "health_checkin_notifier", "record_id": state.get("record_id")})
    return False


async def _finalize(collector: _Collector) -> None:
    _collectors.pop(collector.elder_id, None)
    fields = _deterministic_record(collector)
    await _llm_rescue(collector, fields)

    from app.graphs.health import run_triage
    from app.models import HealthRecord

    async def _write(session, *, elder_id: UUID, anchor: datetime, data: dict, source: str):
        record = HealthRecord(
            elder_id=elder_id,
            recorded_at=anchor,
            bp_systolic=data.get("bp_systolic"),
            bp_diastolic=data.get("bp_diastolic"),
            sleep_minutes=data.get("sleep_minutes"),
            mood=str(data["mood"]) if data.get("mood") is not None else None,
            chief_complaint=data.get("chief_complaint"),
            source=source,
        )
        session.add(record)
        await session.flush()
        await run_triage(
            session, record=record, elder_name=collector.elder_name,
            notifier=_noop_notifier, checkpoint_path=None,
        )

    now = datetime.now(UTC)
    async with get_session_factory()() as db:
        if collector.makeup_date is not None:
            # 补问：只采睡眠时长，锚定缺失那天 20:00（北京时间）
            anchor = _cst(collector.makeup_date, 20, 0)
            makeup_data = {
                "bp_systolic": None, "bp_diastolic": None,
                "sleep_minutes": collector.parsed.get("makeup_sleep"),
                "mood": None, "chief_complaint": None,
            }
            await _write(db, elder_id=collector.elder_id, anchor=anchor,
                         data=makeup_data, source="checkin_makeup")
        anchor = now
        await _write(db, elder_id=collector.elder_id, anchor=anchor,
                     data=fields, source="checkin")
        await db.commit()
    _pending_makeup.pop(collector.elder_id, None)
    logger.info(
        "health checkin finalized",
        extra={"event": "health_checkin_done", "elder_id": str(collector.elder_id),
               "collected": sum(1 for v in fields.values() if v is not None)},
    )


def mark_offline(elder_id: UUID, *, day: Any | None = None) -> None:
    """设备离线标记（D1 口径）：缺失外呼那天记下，次日外呼头插一问。"""
    _pending_makeup.setdefault(elder_id, day or datetime.now(UTC))


def pending_makeup_date(elder_id: UUID) -> Any | None:
    return _pending_makeup.get(elder_id)


async def shutdown() -> None:
    _collectors.clear()
    _pending_makeup.clear()
