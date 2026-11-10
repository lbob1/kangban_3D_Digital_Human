"""规则层表驱动单测（Day6 P0-2，大纲 6.1）。

口径：10 条正例逐条命中（含变体话术）+ 负例不误伤 + 优先级 + 精确词口径 +
「停 <5ms 零模型」延迟证据 + 规则表结构不变量（10 条封顶 / id 唯一 / 预告话术齐备）。
"""

from __future__ import annotations

import pytest

from app.router.rules import RULES, match_rule

# 每条规则一个代表正例（表驱动：规则 id → 触发例）
POSITIVE_CASES: dict[str, str] = {
    "stop_playback": "停",
    "volume_up": "声音太大了，大声点",
    "volume_down": "小声点",
    "tell_time": "现在几点了",
    "weather_today": "今天天气怎么样",
    "farewell": "再见",
    "repeat_last": "你再说一遍",
    "media_next_preview": "换一段吧",
    "opera_preview": "我想听越剧",
    "medication_preview": "我要吃药了",
}

# 负例清单：不得命中任何规则（「明天下雨吗」等无「天气」字样变体走 System One，D9）
NEGATIVE_CASES = [
    "明天下雨吗",
    "周末冷不冷",
    "京剧是什么时候起源的",  # 无「听」引导，不误伤
    "我昨天吃了药",  # 过去陈述非点播/提醒意图 → 不命中（交 S1/主控兜底）
    "今天心情不错",
    "停下来聊聊",  # 「停」是精确词，短语不命中
]


@pytest.mark.parametrize("rule_id", sorted(POSITIVE_CASES), ids=sorted(POSITIVE_CASES))
def test_rule_positive_hit(rule_id: str):
    match = match_rule(POSITIVE_CASES[rule_id])
    assert match is not None, f"{rule_id} 正例未命中"
    assert match.rule_id == rule_id


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_negative_no_false_hit(text: str):
    assert match_rule(text) is None, f"负例误伤：{text}"


def test_rule_table_shape_invariants():
    """10 条封顶不追加；id/action 唯一；预告话术 + 收尾话术齐备（Day7 原位接管后口径）。"""
    assert len(RULES) == 10
    rule_ids = [r.rule_id for r in RULES]
    assert len(rule_ids) == len(set(rule_ids))
    actions = [r.action for r in RULES]
    assert len(actions) == len(set(actions))
    # Day7 步7 原位接管后仍保留确定性预告文本的规则（medication 已换动态 handler）
    preview_text_actions = {"opera_play", "media_next", "farewell"}
    for r in RULES:
        if r.action in preview_text_actions:
            assert r.reply_text, f"{r.rule_id} 缺确定性回复文本"
        else:
            assert r.reply_text is None, f"{r.rule_id} 不应有回复文本"


def test_exact_word_rules_strict():
    """「停」「再见」精确词：带标点/空白可命中，带前后缀不命中。"""
    assert match_rule("停！").rule_id == "stop_playback"
    assert match_rule("  再见。").rule_id == "farewell"
    assert match_rule("停一下") is None or match_rule("停一下").rule_id != "stop_playback"
    assert match_rule("跟您说再见啦") is None


def test_weather_fast_path_variant_boundary():
    """D9 边界：时间词+天气 命中快路径；无「天气」字样变体不命中。"""
    assert match_rule("今天天气").rule_id == "weather_today"
    assert match_rule("今天的天气怎么样啊").rule_id == "weather_today"
    assert match_rule("今日天气如何") is not None
    for variant in ("明天下雨吗", "天气怎么样"):
        assert match_rule(variant) is None


def test_weather_tomorrow_hits_rule_with_text():
    """Day6+：明天/后天 + 天气 命中同一条规则（条数封顶不追加），原句随 RuleMatch 下发。"""
    for text in ("明天天气怎么样", "明日天气如何", "后天天气好吗"):
        match = match_rule(text)
        assert match is not None
        assert match.rule_id == "weather_today"
        assert match.text == text  # handler 据此解析日期传参


def test_first_match_priority_order():
    """规则表顺序即优先级：同时含「大声」与「几点」时按表序取 volume_up。"""
    match = match_rule("大声点，现在几点了")
    assert match is not None
    assert match.rule_id == "volume_up"


def test_stop_rule_latency_under_5ms():
    """验收 2 证据链：说「停」route_layer=rule + latency<5ms（本层匹配耗时）。"""
    for _ in range(100):
        match = match_rule("停")
        assert match is not None
        assert match.latency_ms < 5.0, f"匹配耗时 {match.latency_ms:.3f}ms 超 5ms"
