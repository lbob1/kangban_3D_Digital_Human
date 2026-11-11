"""安全兜底层（Day6，大纲 6.3 ①）：确定性敏感词表，一票否决，零延迟。

口径（Day6 主文档 §4.1 / §4.6）：
- 先行于规则层与 System One；命中即 HITL（安抚话术 + work_orders 落库），
  绝不调主控 Agent、绝不让 LLM 生成回复（敏感红线）；
- is_sensitive 不信任 laya（POC-03 实测召回 25% 不可用，方案 A 裁决）：
  敏感判定 = 本词表 + 主控 Agent system prompt 敏感转人工双保险；
- System One 不可用/超时时本层仍在 ① 兜底——「无法确定敏感性的输入
  采取保守策略」靠词表扩充实现；
- 词表只增不改删；类别 5 类封顶（急症/跌倒/走失/情绪危机/紧急求救）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# 6 类敏感词表（命中任意一词即一票否决）；词形克制，避免「头晕」等常见非急症误伤。
# Day6+ 第 6 类 abuse（用户裁决）：虐待指控只推家属+管理员（排除护士/医生——
# 施害者可能是医护本身），其余类目全角色可见。
SENSITIVE_WORDS: dict[str, tuple[str, ...]] = {
    # Day8 步3 扩表：与分级引擎 RED_COMPLAINT_WORDS 对齐（意识模糊/晕厥补齐），
    # 急症词在级联 ① 层零延迟直达 HITL（胸痛/呼吸困难复用既有架构，大纲 §4.2）
    "acute_symptom": (
        "胸口疼", "胸痛", "胸闷", "喘不上气", "呼吸困难", "晕倒", "晕厥", "意识模糊",
    ),
    "fall": ("摔倒", "跌倒", "起不来", "爬不起来", "摔了一跤"),
    "lost": ("走失", "走丢", "迷路", "找不到家"),
    "emotional_crisis": ("不想活", "轻生", "自杀"),
    "emergency_call": ("救命", "快来人", "出事了"),
    "abuse": ("虐待", "打我", "骂我", "欺负我", "有人打"),
}

# 补充正则模式：词表子串抓不住的隔字表达（如「胸口有点疼」「胸口好疼」）。
# 隔字窗口收紧到 6 字：覆盖程度副词插入，又不至于把「胸口」当话题词误伤长句。
SENSITIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "acute_symptom": (r"胸口[^。！？，,]{0,6}疼",),
}

# 预编译：类别 → 任一词/模式命中
_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        category,
        re.compile("|".join(words + SENSITIVE_PATTERNS.get(category, ()))),
    )
    for category, words in SENSITIVE_WORDS.items()
)

# 安抚话术（大纲 6.3 口径：不评估不诊断，只安抚 + 人工介入）
COMFORT_TEXT = (
    "您别着急，我已经通知工作人员马上过来看您。"
    "先原地休息一下，保持电话畅通，好不好？"
)

# abuse 专属安抚：绝不提「通知工作人员」（施害者可能是工作人员本身），
# 只告知已联系家人——转达对象按可见性矩阵只有家属与管理员。
COMFORT_TEXT_ABUSE = (
    "您别害怕，我已经把情况转达给您的家人了，"
    "先安心待着，家人很快就会来看您。"
)


def comfort_text_for(types: list[str]) -> str:
    """按主类目（首个命中）选安抚话术：abuse 走专属版本。"""
    return COMFORT_TEXT_ABUSE if types and types[0] == "abuse" else COMFORT_TEXT


@dataclass(frozen=True)
class SensitiveHit:
    """敏感命中结果：cascade 据此走 HITL 短路并落 decision_logs。"""

    types: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


def check_sensitive(text: str) -> SensitiveHit | None:
    """确定性敏感判定：命中返回 SensitiveHit（含全部命中类别），未命中返回 None。"""
    start = time.perf_counter()
    hit_types = [category for category, pattern in _COMPILED if pattern.search(text)]
    if not hit_types:
        return None
    elapsed = (time.perf_counter() - start) * 1000
    return SensitiveHit(types=hit_types, latency_ms=elapsed)
