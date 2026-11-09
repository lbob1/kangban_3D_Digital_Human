"""Day6 P1-5 级联路由单测：System One 用 Fake mock，安全/规则层走真实确定性实现。

覆盖口径（执行提示词 P1-5）：
- 顺序铁律：敏感一票否决最先；规则/S1 命中不升级；S1 不可用记 degraded 跳主控；
- 「停」零模型：route_layer=rule、model_version=null、纯 WS 事件无话术；
- 天气双路径预留：规则快路径与 S1 intent=weather 未实装 handler 时都落到主控（P1-6 换 MCP）；
- 低置信/域外澄清反问不升级；input_text 脱敏。
"""

import pytest

from app.router.cascade import (
    CLARIFY_TEXT,
    REPEAT_EMPTY_TEXT,
    CascadeRouter,
    _register_default_handlers,
    redact_input,
)
from app.router.safety import COMFORT_TEXT
from app.system_one.provider_laya import SystemOneUnavailable
from app.system_one.questions import IntentLabel, SystemOneResult


class FakeS1:
    """System One 替身：可配置输出或不可用；记录调用次数供顺序断言。"""

    def __init__(
        self,
        intent: str = "chat",
        confidence: float = 0.95,
        unavailable_reason: str | None = None,
    ) -> None:
        self.intent = intent
        self.confidence = confidence
        self.unavailable_reason = unavailable_reason
        self.calls = 0

    async def classify(self, text: str) -> SystemOneResult:
        self.calls += 1
        if self.unavailable_reason is not None:
            raise SystemOneUnavailable(self.unavailable_reason)
        intent = IntentLabel(self.intent)
        return SystemOneResult(
            intent=intent,
            is_sensitive=False,  # 方案 A：级联不信任该字段，本表验证词表优先
            sensitive_types=[],
            confidence=self.confidence,
            needs_clarification=self.confidence < 0.85,
            privacy_restricted=False,
            is_out_of_domain=intent == IntentLabel.OTHER,
            secondary_intents=[],
            model_version="laya:fake@test1234",
        )


def make_router(s1: FakeS1 | None = None) -> CascadeRouter:
    router = CascadeRouter(system_one=s1)
    _register_default_handlers(router)
    return router


# ---- ① 安全一票否决 ----


async def test_sensitive_hit_short_circuits_with_comfort_and_workorder():
    router = make_router()
    outcome = await router.decide("我胸口疼得厉害")
    assert outcome.route_layer == "safety"
    assert outcome.sensitive_hit == "acute_symptom"
    assert outcome.reply_text == COMFORT_TEXT
    assert outcome.create_workorder is True
    assert outcome.escalated_to_agent is False


async def test_sensitive_beats_rule_and_system_one():
    s1 = FakeS1()
    router = make_router(s1)
    # 同时满足规则（重复）与敏感（救命）→ 安全局最先且 S1 零调用
    outcome = await router.decide("救命，再说一遍")
    assert outcome.route_layer == "safety"
    assert s1.calls == 0


async def test_sensitive_multi_type_aggregated():
    router = make_router()
    outcome = await router.decide("我摔倒了，不想活了")
    assert outcome.route_layer == "safety"
    assert set(outcome.sensitive_hit.split(",")) == {"fall", "emotional_crisis"}


# ---- ② 规则层 ----


async def test_stop_is_zero_model_pure_event():
    router = make_router(FakeS1())
    outcome = await router.decide("停")
    assert outcome.route_layer == "rule"
    assert outcome.intent == "stop_playback"
    assert outcome.model_version is None
    assert outcome.reply_text is None
    assert outcome.ws_events == [{"type": "stop_playback"}]
    assert outcome.escalated_to_agent is False
    assert outcome.latency_ms < 5  # 「停 <5ms」验收口径


async def test_volume_events():
    router = make_router()
    up = await router.decide("大声一点")
    assert up.ws_events == [{"type": "volume", "direction": "up"}]
    down = await router.decide("小声点")
    assert down.ws_events == [{"type": "volume", "direction": "down"}]


async def test_rule_reply_text_handlers():
    router = make_router()
    farewell = await router.decide("再见")
    assert farewell.route_layer == "rule"
    assert "您先歇着" in (farewell.reply_text or "")
    opera = await router.decide("我想听一段越剧")
    assert "越剧" in (opera.reply_text or "")


async def test_tell_time_local_reply():
    router = make_router()
    outcome = await router.decide("现在几点了")
    assert outcome.route_layer == "rule"
    assert outcome.reply_text is not None
    assert "点" in outcome.reply_text


async def test_repeat_last_repeats_history_or_fallback():
    router = make_router()
    again = await router.decide("再说一遍", last_reply="今天天气不错。")
    assert again.reply_text == "今天天气不错。"
    empty = await router.decide("再说一遍")
    assert empty.reply_text == REPEAT_EMPTY_TEXT


async def test_weather_rule_without_handler_falls_through_to_agent():
    router = make_router()  # P1-5：天气 handler 未实装 → 落主控（P1-6 换 MCP）
    outcome = await router.decide("今天天气怎么样")
    assert outcome.route_layer == "agent"
    assert outcome.escalated_to_agent is True
    assert outcome.degraded is False


# ---- ③ System One ----


async def test_s1_high_conf_weather_falls_to_agent_without_handler():
    s1 = FakeS1(intent="weather", confidence=0.92)
    router = make_router(s1)
    outcome = await router.decide("明天下雨吗")  # 规则不命中 → S1
    assert s1.calls == 1
    assert outcome.route_layer == "agent"
    assert outcome.intent == "weather"
    assert outcome.confidence == pytest.approx(0.92)
    assert outcome.model_version == "laya:fake@test1234"
    assert outcome.escalated_to_agent is True


async def test_s1_weather_with_handler_converges_same_pipeline():
    s1 = FakeS1(intent="weather", confidence=0.92)
    router = make_router(s1)
    # D9 双路径汇合：S1 路由到与规则快路径同一个 handler（P1-6 注册 MCP 管线）
    router.register_handler("weather_fast_path", lambda rule, last: None)  # 未实装仍落主控
    outcome = await router.decide("明天下雨吗")
    assert outcome.route_layer == "agent"
    assert outcome.intent == "weather"


async def test_s1_high_conf_medication_routes_preview_reply():
    s1 = FakeS1(intent="medication", confidence=0.9)
    router = make_router(s1)
    outcome = await router.decide("降压药什么时候吃合适")
    assert outcome.route_layer == "system_one"
    assert outcome.intent == "medication"
    assert "按时吃药" in (outcome.reply_text or "")
    assert outcome.escalated_to_agent is False


async def test_s1_low_confidence_clarifies_not_escalates():
    s1 = FakeS1(intent="chat", confidence=0.4)
    router = make_router(s1)
    outcome = await router.decide("那个啥来着")
    assert outcome.route_layer == "system_one"
    assert outcome.reply_text == CLARIFY_TEXT
    assert outcome.escalated_to_agent is False


async def test_s1_out_of_domain_clarifies():
    s1 = FakeS1(intent="other", confidence=0.99)
    router = make_router(s1)
    outcome = await router.decide("量子力学是什么")
    assert outcome.route_layer == "system_one"
    assert outcome.reply_text == CLARIFY_TEXT


async def test_s1_chat_high_conf_escalates_to_agent():
    s1 = FakeS1(intent="chat", confidence=0.95)
    router = make_router(s1)
    outcome = await router.decide("陪我聊聊天")
    assert outcome.route_layer == "agent"
    assert outcome.intent == "chat"
    assert outcome.escalated_to_agent is True


async def test_s1_unavailable_degrades_to_agent():
    s1 = FakeS1(unavailable_reason="timeout")
    router = make_router(s1)
    outcome = await router.decide("随便聊聊")
    assert outcome.route_layer == "agent"
    assert outcome.degraded is True
    assert outcome.escalated_to_agent is True


async def test_s1_none_skips_layer_without_degraded_flag():
    router = make_router(None)  # 未启用 System One（配置未开启）
    outcome = await router.decide("随便聊聊")
    assert outcome.route_layer == "agent"
    assert outcome.degraded is False


# ---- 脱敏 ----


def test_redact_id_card():
    text = "我身份证是110101199003077758请帮我登记"
    redacted = redact_input(text)
    assert "110101199003077758" not in redacted
    assert "11*************8" in redacted  # 保留前 2 后 1


def test_redact_dose_numbers():
    redacted = redact_input("每天吃25mg的药，一次2片")
    assert "25" not in redacted and "2片" not in redacted
    assert "mg" in redacted and "片" in redacted


def test_redact_keeps_normal_text():
    assert redact_input("今天天气怎么样") == "今天天气怎么样"


# ---- 落库辅助（fake session，不连数据库）----


class _FakeSession:
    def __init__(self):
        self.objects: list = []

    def add(self, obj):
        self.objects.append(obj)

    async def commit(self):
        pass


class _FakeFactory:
    def __init__(self):
        self.session = _FakeSession()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


async def test_persist_decision_log_adds_row_with_redacted_input():
    from app.models.decision_log import DecisionLog
    from app.router.cascade import persist_decision_log

    factory = _FakeFactory()
    outcome = await make_router().decide("我胸口疼")
    await persist_decision_log(
        factory, outcome, text="身份证110101199003077758", device_id=None, elder_id=None
    )
    assert len(factory.session.objects) == 1
    row = factory.session.objects[0]
    assert isinstance(row, DecisionLog)
    assert row.route_layer == "safety"
    assert row.sensitive_hit == "acute_symptom"
    assert "110101199003077758" not in (row.input_text or "")


async def test_persist_decision_log_noop_without_factory():
    from app.router.cascade import persist_decision_log

    outcome = await make_router().decide("停")
    await persist_decision_log(None, outcome, text="停", device_id=None, elder_id=None)  # 不抛异常


async def test_sensitive_workorder_skipped_without_elder():
    from app.router.cascade import persist_sensitive_workorder

    factory = _FakeFactory()
    outcome = await make_router().decide("我胸口疼")
    await persist_sensitive_workorder(
        factory, outcome, elder_id=None, text="我胸口疼"
    )  # 未声纹验证：不落工单（workorders.elder_id NOT NULL）
    assert factory.session.objects == []
