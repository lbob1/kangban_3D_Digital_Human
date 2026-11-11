"""Day8 步1：分级规则引擎单测（阈值边界全测 / 主诉直达 red / 偏差标注 / 缺采集口径）。

纯函数无 DB；边界对照 Day8 主文档 §9 验收项 2：179→yellow、180→red、109→yellow、110→red。
"""

import pytest

from app.services.health_triage import (
    DEVIATION_NOTE,
    LEVEL_GREEN,
    LEVEL_RED,
    LEVEL_YELLOW,
    parse_mood,
    triage,
)

# ---- 血压边界（验收硬项：179/180、109/110）----


@pytest.mark.parametrize(
    ("sbp", "dbp", "expected"),
    [
        (159, 99, LEVEL_GREEN),
        (160, 99, LEVEL_YELLOW),  # 收缩压下边界
        (179, 99, LEVEL_YELLOW),  # 179 → yellow
        (180, 99, LEVEL_RED),  # 180 → red
        (200, 99, LEVEL_RED),
        (130, 100, LEVEL_YELLOW),  # 舒张压下边界
        (130, 109, LEVEL_YELLOW),  # 109 → yellow
        (130, 110, LEVEL_RED),  # 110 → red
        (130, 130, LEVEL_RED),
        (130, 99, LEVEL_GREEN),
    ],
)
def test_bp_boundaries(sbp, dbp, expected):
    assert triage(bp_systolic=sbp, bp_diastolic=dbp).level == expected


# ---- 睡眠 / 情绪 ----


def test_sleep_boundary():
    assert triage(sleep_minutes=299).level == LEVEL_YELLOW  # <300 触发
    assert triage(sleep_minutes=300).level == LEVEL_GREEN


def test_mood_boundary():
    assert triage(mood="2").level == LEVEL_YELLOW  # ≤2 触发
    assert triage(mood="3").level == LEVEL_GREEN
    assert triage(mood=1).level == LEVEL_YELLOW
    assert triage(mood=None).level == LEVEL_GREEN


def test_parse_mood_invalid():
    assert parse_mood("abc") is None  # 解析失败不触发
    assert parse_mood("") is None
    assert parse_mood("0") is None  # 越界视为无效
    assert parse_mood("6") is None
    assert parse_mood(" 4 ") == 4


# ---- 主诉直达 red ----


def test_complaint_direct_red():
    assert triage(chief_complaint="胸口疼").level == LEVEL_RED
    assert triage(bp_systolic=120, chief_complaint="有点胸闷").level == LEVEL_RED  # 数值正常也直达
    assert triage(chief_complaint="昨晚呼吸困难和头晕").level == LEVEL_RED
    # 头晕不在 red 词表（走 laya/LLM 兜底）
    assert triage(chief_complaint="我头晕").level == LEVEL_GREEN
    assert triage(chief_complaint="没啥事").level == LEVEL_GREEN


# ---- 多命中合并与优先级 ----


def test_multi_hit_red_priority():
    result = triage(bp_systolic=185, bp_diastolic=105, chief_complaint="胸痛")
    assert result.level == LEVEL_RED  # red 优先
    assert "收缩压185" in result.reason
    assert "舒张压105" in result.reason
    assert "胸痛" in result.reason


def test_multi_yellow_reason_joined():
    result = triage(bp_systolic=165, sleep_minutes=200, mood="1")
    assert result.level == LEVEL_YELLOW
    assert "收缩压165" in result.reason
    assert "睡眠200" in result.reason
    assert "情绪自评1" in result.reason


# ---- 缺采集口径（§4.3：缺采集=green+备注）----


def test_all_missing_green_with_note():
    result = triage()
    assert result.level == LEVEL_GREEN
    assert "缺采集" in result.reason


def test_partial_missing_still_rules_on_present():
    result = triage(bp_systolic=185)
    assert result.level == LEVEL_RED


# ---- 误报防护（偏差>40% 标注，不改级别）----


def test_deviation_flag_on_red():
    result = triage(bp_systolic=180, baseline={"sbp": 120.0, "dbp": None, "sleep": None})
    assert result.level == LEVEL_RED  # 级别不变，裁决权留给 HITL
    assert result.deviation_flag is True  # 50% 偏差 >40%
    assert DEVIATION_NOTE in result.reason
    assert "sbp" in result.reason


def test_no_deviation_within_40pct():
    result = triage(bp_systolic=180, baseline={"sbp": 160.0, "dbp": None, "sleep": None})
    assert result.deviation_flag is False  # 12.5% 偏差
    assert DEVIATION_NOTE not in result.reason


def test_deviation_on_green_sleep():
    result = triage(sleep_minutes=500, baseline={"sbp": None, "dbp": None, "sleep": 300.0})
    assert result.level == LEVEL_GREEN
    assert result.deviation_flag is True  # 66% 偏差标注


def test_deviation_skipped_without_baseline():
    result = triage(bp_systolic=180)
    assert result.deviation_flag is False
    result = triage(bp_systolic=180, baseline={"sbp": None, "dbp": None, "sleep": None})
    assert result.deviation_flag is False
