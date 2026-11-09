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


def test_wordlist_shape_invariants():
    """5 类封顶；每类至少 3 词；安抚话术齐备（HITL 播报用）。"""
    assert len(SENSITIVE_WORDS) == 5
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
