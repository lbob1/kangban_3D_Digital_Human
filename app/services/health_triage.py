"""健康分级规则引擎（Day8 步1，大纲 8.1 Vibe 原文：阈值放代码不放 prompt）。

- 阈值全部为模块级常量表（可单测可改）；LLM/laya 仅做主诉急症意图兜底（图层职责），
  本模块零模型调用、纯函数可测；
- 分级优先级：red > yellow > green（先命中先得，多命中合并进 reason 供审计）；
- mood 存储口径为 1–5 分整数字符串（2026-11-11 用户裁决）；解析失败视为未触发情绪规则；
- 误报防护（Day8 主文档 §4.1）：录入值与近 7 天基线偏差 >40% 时 reason 标注
  「偏差大，建议人工复核」（不改级别，只加标注——级别裁决权留给 HITL）。
"""

from dataclasses import dataclass

# ---- 分级阈值常量表 ----
LEVEL_GREEN = "green"
LEVEL_YELLOW = "yellow"
LEVEL_RED = "red"

RED_SBP_GTE = 180  # 收缩压 ≥180 → red（179 → yellow 边界）
RED_DBP_GTE = 110  # 舒张压 ≥110 → red（109 → yellow 边界）
YELLOW_SBP_GTE = 160  # 收缩压 160–179 → yellow
YELLOW_DBP_GTE = 100  # 舒张压 100–109 → yellow
YELLOW_SLEEP_LT = 300  # 睡眠 <300min → yellow
YELLOW_MOOD_LE = 2  # 情绪自评 ≤2（5 分制）→ yellow

# 主诉急症词（命中直达 red，大纲 8.1；与 safety.py 第一层词表保持同源，步3 扩表）
RED_COMPLAINT_WORDS = ("胸口疼", "胸痛", "胸闷", "喘不上气", "呼吸困难", "意识模糊", "晕厥", "晕倒")

# ---- 误报防护（偏差>40% 标注）----
DEVIATION_RATIO = 0.40
DEVIATION_NOTE = "偏差大，建议人工复核"

_BASELINE_KEYS = ("sbp", "dbp", "sleep")


@dataclass
class TriageResult:
    """分级结论（写入 health_records.triage_level / triage_reason 并进 graph state 审计）。"""

    level: str
    reason: str
    deviation_flag: bool = False


def parse_mood(mood) -> int | None:
    """mood 口径为 1–5 整数字符串（或 int）；解析失败/越界返回 None（不触发情绪规则）。"""
    if mood is None:
        return None
    try:
        value = int(str(mood).strip())
    except ValueError:
        return None
    return value if 1 <= value <= 5 else None


def triage(
    bp_systolic: int | None = None,
    bp_diastolic: int | None = None,
    sleep_minutes: int | None = None,
    mood=None,
    chief_complaint: str | None = None,
    baseline: dict[str, float | None] | None = None,
) -> TriageResult:
    """四级输入 → 分级结论。baseline 为近 7 天均值 {"sbp","dbp","sleep"}（缺省跳过偏差校验）。"""
    reasons: list[str] = []
    hit_red = False
    hit_yellow = False

    complaint = (chief_complaint or "").strip()
    red_words = [w for w in RED_COMPLAINT_WORDS if w in complaint]
    if red_words:
        hit_red = True
        reasons.append(f"主诉含急症词：{'、'.join(red_words)}")

    if bp_systolic is not None:
        if bp_systolic >= RED_SBP_GTE:
            hit_red = True
            reasons.append(f"收缩压{bp_systolic}≥{RED_SBP_GTE}")
        elif bp_systolic >= YELLOW_SBP_GTE:
            hit_yellow = True
            reasons.append(f"收缩压{bp_systolic}≥{YELLOW_SBP_GTE}")

    if bp_diastolic is not None:
        if bp_diastolic >= RED_DBP_GTE:
            hit_red = True
            reasons.append(f"舒张压{bp_diastolic}≥{RED_DBP_GTE}")
        elif bp_diastolic >= YELLOW_DBP_GTE:
            hit_yellow = True
            reasons.append(f"舒张压{bp_diastolic}≥{YELLOW_DBP_GTE}")

    if sleep_minutes is not None and sleep_minutes < YELLOW_SLEEP_LT:
        hit_yellow = True
        reasons.append(f"睡眠{sleep_minutes}min<{YELLOW_SLEEP_LT}")

    mood_score = parse_mood(mood)
    if mood_score is not None and mood_score <= YELLOW_MOOD_LE:
        hit_yellow = True
        reasons.append(f"情绪自评{mood_score}≤{YELLOW_MOOD_LE}")

    if not reasons:
        # 缺采集或全部正常 = green（缺采集口径见 Day8 主文档 §4.3，备注区分）
        if _all_none(bp_systolic, bp_diastolic, sleep_minutes, mood, chief_complaint):
            reason = "缺采集，按正常处理"
        else:
            reason = "各项指标未见异常"
        return _apply_deviation(
            TriageResult(level=LEVEL_GREEN, reason=reason),
            bp_systolic=bp_systolic,
            bp_diastolic=bp_diastolic,
            sleep_minutes=sleep_minutes,
            baseline=baseline,
        )

    level = LEVEL_RED if hit_red else (LEVEL_YELLOW if hit_yellow else LEVEL_GREEN)
    result = TriageResult(level=level, reason="；".join(reasons))
    return _apply_deviation(
        result,
        bp_systolic=bp_systolic,
        bp_diastolic=bp_diastolic,
        sleep_minutes=sleep_minutes,
        baseline=baseline,
    )


def _all_none(*values) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def _apply_deviation(
    result: TriageResult,
    *,
    bp_systolic: int | None,
    bp_diastolic: int | None,
    sleep_minutes: int | None,
    baseline: dict[str, float | None] | None,
) -> TriageResult:
    """与近 7 天基线偏差>40% 时标注「建议人工复核」（不改级别——HITL 才是裁决方）。"""
    if not baseline:
        return result
    pairs = (
        ("sbp", bp_systolic),
        ("dbp", bp_diastolic),
        ("sleep", sleep_minutes),
    )
    notes: list[str] = []
    for key, value in pairs:
        base = baseline.get(key)
        if value is None or not base:
            continue
        if abs(value - base) / base > DEVIATION_RATIO:
            notes.append(key)
    if notes:
        result.deviation_flag = True
        result.reason = f"{result.reason}（{'、'.join(notes)}{DEVIATION_NOTE}）"
    return result
