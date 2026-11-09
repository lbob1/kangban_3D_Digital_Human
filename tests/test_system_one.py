"""System One 契约 + laya 适配器单测（Day6 P0-3，TDD §5.2）。

口径：契约 9 字段不得增删 / 意图枚举 7 值封顶 / 派生字段规则 /
provider 契约映射（mock agent，不装 laya）/ 未就绪与超时降级路径。
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from app.system_one.provider_laya import LayaProvider, SystemOneUnavailable
from app.system_one.questions import (
    LAYA_MODEL_ID,
    LAYA_REVISION,
    IntentLabel,
    SystemOneResult,
    build_laya_questions,
)

CONTRACT_FIELDS = {
    "intent",
    "is_sensitive",
    "sensitive_types",
    "confidence",
    "needs_clarification",
    "privacy_restricted",
    "is_out_of_domain",
    "secondary_intents",
    "model_version",
}


# ---- 契约（TDD §5.2）----


def test_contract_fields_exact_nine():
    """字段不得增删：模型字段集与 TDD §5.2 完全一致。"""
    assert set(SystemOneResult.model_fields) == CONTRACT_FIELDS
    assert len(CONTRACT_FIELDS) == 9


def test_contract_intent_enum_seven_values():
    assert {m.value for m in IntentLabel} == {
        "chat", "medication", "opera", "movie", "weather", "health", "other",
    }


def test_contract_rejects_unknown_intent():
    with pytest.raises(ValidationError):
        SystemOneResult(
            intent="music",  # 非法枚举
            is_sensitive=False,
            confidence=0.9,
            needs_clarification=False,
            privacy_restricted=False,
            is_out_of_domain=False,
        )


def test_contract_confidence_bounds():
    base = dict(
        is_sensitive=False,
        needs_clarification=False,
        privacy_restricted=False,
        is_out_of_domain=False,
    )
    with pytest.raises(ValidationError):
        SystemOneResult(intent="chat", confidence=1.5, **base)
    with pytest.raises(ValidationError):
        SystemOneResult(intent="chat", confidence=-0.1, **base)


def test_contract_json_roundtrip():
    r = SystemOneResult(
        intent=IntentLabel.WEATHER,
        is_sensitive=False,
        confidence=0.93,
        needs_clarification=False,
        privacy_restricted=False,
        is_out_of_domain=False,
        model_version="laya:x@abc1234",
    )
    data = r.model_dump(mode="json")
    assert data["intent"] == "weather"
    assert SystemOneResult.model_validate(data) == r


def test_laya_questions_shape():
    """question schema 与 POC-03 spike 实证一致：choice + noul 两问。"""
    q = build_laya_questions()
    assert q["intent"]["type"] == "choice"
    assert set(q["intent"]["criteria"]) == {m.value for m in IntentLabel}
    assert q["is_sensitive"]["type"] == "noul"


# ---- provider 契约映射（mock agent，不依赖 laya 安装）----


class FakeAgent:
    """按预设返回 spike 实证格式的 laya predict 输出。"""

    def __init__(self, answers: dict, sleep_s: float = 0.0):
        self.answers = answers
        self.sleep_s = sleep_s
        self.calls: list[str] = []

    def predict(self, text: str, questions: dict, lang: str = "zh") -> dict:
        self.calls.append(text)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        return {"answers": self.answers}


def _make_provider(agent, **kw) -> LayaProvider:
    p = LayaProvider(**kw)
    p._agent = agent  # 测试注入，跳过真实懒加载
    return p


@pytest.mark.asyncio
async def test_classify_maps_high_confidence_weather():
    agent = FakeAgent({
        "intent": {"choice": "weather", "confidence": 0.93, "probabilities": {"weather": 0.93}},
        "is_sensitive": {"noul": 0.02},
    })
    p = _make_provider(agent, confidence_threshold=0.85)
    r = await p.classify("明天下雨吗")
    assert r.intent == IntentLabel.WEATHER
    assert r.confidence == pytest.approx(0.93)
    assert r.needs_clarification is False
    assert r.is_out_of_domain is False
    assert r.is_sensitive is False
    assert r.privacy_restricted is False
    assert r.secondary_intents == []
    assert r.sensitive_types == []
    assert r.model_version == f"laya:{LAYA_MODEL_ID}@{LAYA_REVISION[:7]}"


@pytest.mark.asyncio
async def test_classify_low_confidence_flags_clarification():
    agent = FakeAgent({
        "intent": {"choice": "chat", "confidence": 0.42},
        "is_sensitive": {"noul": 0.01},
    })
    p = _make_provider(agent)
    r = await p.classify("那个那个啥")
    assert r.needs_clarification is True
    assert r.is_out_of_domain is False  # chat 是合法域内意图


@pytest.mark.asyncio
async def test_classify_other_maps_out_of_domain():
    agent = FakeAgent({
        "intent": {"choice": "other", "confidence": 0.88},
        "is_sensitive": {"noul": 0.01},
    })
    p = _make_provider(agent)
    r = await p.classify("外星人什么时候来")
    assert r.is_out_of_domain is True


@pytest.mark.asyncio
async def test_classify_noul_signal_only():
    """方案 A：noul>0.5 填 is_sensitive 契约字段，仅信号；sensitive_types 恒空。"""
    agent = FakeAgent({
        "intent": {"choice": "health", "confidence": 0.9},
        "is_sensitive": {"noul": 0.7},
    })
    p = _make_provider(agent)
    r = await p.classify("我胸口疼")
    assert r.is_sensitive is True
    assert r.sensitive_types == []


@pytest.mark.asyncio
async def test_classify_unknown_choice_falls_back_other():
    agent = FakeAgent({
        "intent": {"choice": "unknown_label", "confidence": 0.9},
        "is_sensitive": {"noul": 0.0},
    })
    p = _make_provider(agent)
    r = await p.classify("乱七八糟输入")
    assert r.intent == IntentLabel.OTHER


@pytest.mark.asyncio
async def test_classify_confidence_fallback_to_probabilities():
    """confidence 缺失时取 probabilities 最大值兜底。"""
    agent = FakeAgent({
        "intent": {"choice": "movie", "probabilities": {"movie": 0.91, "opera": 0.05}},
        "is_sensitive": {"noul": 0.0},
    })
    p = _make_provider(agent)
    r = await p.classify("放个电影")
    assert r.confidence == pytest.approx(0.91)


# ---- 降级路径（可见降级，不静默）----


@pytest.mark.asyncio
async def test_not_ready_raises_unavailable():
    p = LayaProvider()
    with pytest.raises(SystemOneUnavailable) as ei:
        await p.classify("今天天气怎么样")
    assert ei.value.reason == "model_not_loaded"


@pytest.mark.asyncio
async def test_inference_timeout_raises_unavailable():
    """200ms 超时降级：慢推理 → SystemOneUnavailable('timeout')，cascade 跳 ④。"""
    agent = FakeAgent(
        {"intent": {"choice": "chat", "confidence": 0.9}, "is_sensitive": {"noul": 0.0}},
        sleep_s=0.5,
    )
    p = _make_provider(agent, timeout_ms=50)
    with pytest.raises(SystemOneUnavailable) as ei:
        await p.classify("慢慢想")
    assert ei.value.reason == "timeout"


def test_warmup_failure_is_visible_not_silent(monkeypatch):
    """加载失败记录 load_error（cascade 据此记降级事件），不抛不静默。"""
    p = LayaProvider()

    def _boom():
        raise RuntimeError("model files missing")

    monkeypatch.setattr(p, "_do_load", _boom)
    p.warmup()  # 后台线程吞异常并记录
    deadline = time.monotonic() + 5.0
    while p.load_error is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert p.load_error is not None
    assert "RuntimeError" in p.load_error
    assert p.is_ready is False
