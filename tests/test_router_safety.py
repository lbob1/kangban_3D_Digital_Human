"""安全兜底层单测（Day6 P0-2，大纲 6.3 ①）。

口径：5 类词表逐类命中 + 一票否决语义 + 未命中返回 None + 常见非急症不误伤 +
延迟极小（确定性词表零模型）+ 表结构不变量（5 类封顶、安抚话术齐备）。
"""

from __future__ import annotations

import pytest

from app.router.safety import COMFORT_TEXT, SENSITIVE_WORDS, check_sensitive

# 每类一个代表词（表驱动：类别 → 触发例）
POSITIVE_CASES: dict[str, str] = {
    "acute_symptom": "我胸口疼得厉害",
    "fall": "我刚才在厕所摔倒了",
    "lost": "我出门散步走丢了",
    "emotional_crisis": "活着没意思，不想活了",
    "emergency_call": "救命啊，快来人",
    "abuse": "有人打我，我要告诉家里人",
}

# 负例：不误伤常见日常表达（含同形字干扰）
NEGATIVE_CASES = [
    "今天有点头晕，休息一下就好",  # 头晕≠晕倒，克制红线
    "昨天看了越剧真好看",
    "我想去公园遛弯",
    "医生说我恢复得不错",
]


@pytest.mark.parametrize("category", sorted(POSITIVE_CASES), ids=sorted(POSITIVE_CASES))
def test_sensitive_hit_per_category(category: str):
    hit = check_sensitive(POSITIVE_CASES[category])
    assert hit is not None, f"{category} 正例未命中"
    assert category in hit.types


def test_no_hit_returns_none():
    assert check_sensitive("今天天气怎么样") is None
    assert check_sensitive("我想听一段越剧") is None


def test_multi_category_hit_reports_all():
    hit = check_sensitive("我摔倒了，胸口疼，救命")
    assert hit is not None
    assert set(hit.types) == {"fall", "acute_symptom", "emergency_call"}


# ---- abuse 类目（Day6+ 第 6 类：虐待指控，仅家属+管理员可见）----


def test_abuse_category_hits():
    for text in ("护工虐待我", "有人打我", "邻居总是欺负我", "他昨天骂我"):
        hit = check_sensitive(text)
        assert hit is not None, f"abuse 漏检：{text}"
        assert "abuse" in hit.types


def test_abuse_comfort_does_not_mention_staff():
    """abuse 安抚话术绝不提「工作人员」（施害者可能是工作人员），只提家人。"""
    from app.router.safety import COMFORT_TEXT_ABUSE, comfort_text_for

    assert comfort_text_for(["abuse"]) == COMFORT_TEXT_ABUSE
    assert "工作人员" not in COMFORT_TEXT_ABUSE
    assert "家人" in COMFORT_TEXT_ABUSE
    # 非 abuse 类目仍走通用安抚
    assert comfort_text_for(["fall"]) != COMFORT_TEXT_ABUSE


def test_symptom_pattern_catches_spaced_words():
    """Day6+ 补充正则：隔字表达（「胸口有点疼」）必须命中——真实长者不会按词表说话。"""
    for text in ("我胸口有点疼", "胸口好疼啊", "胸口一阵一阵地疼"):
        hit = check_sensitive(text)
        assert hit is not None, f"隔字表达漏检：{text}"
        assert "acute_symptom" in hit.types


def test_symptom_pattern_window_not_overbroad():
    """隔字窗口 6 字封顶 + 标点阻断：长隔字/跨句不误伤。"""
    assert check_sensitive("胸口贴着的膏药，突然有点疼") is None  # 标点阻断
    assert check_sensitive("胸口挂着的相框早就该擦了，膝盖疼") is None  # 超窗不跨抓


def test_wordlist_shape_invariants():
    """6 类（Day6+ 增 abuse，用户裁决）；每类至少 3 词；安抚话术齐备（HITL 播报用）。"""
    assert len(SENSITIVE_WORDS) == 6
    assert set(SENSITIVE_WORDS) == set(POSITIVE_CASES)
    for words in SENSITIVE_WORDS.values():
        assert len(words) >= 3
    assert COMFORT_TEXT
    assert "工作人员" in COMFORT_TEXT


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_negative_no_false_hit(text: str):
    assert check_sensitive(text) is None, f"负例误伤：{text}"


def test_hit_latency_tiny():
    """确定性词表零模型：命中路径 <5ms（「零延迟兜底」证据）。"""
    hit = check_sensitive("我胸口疼")
    assert hit is not None
    assert hit.latency_ms < 5.0
