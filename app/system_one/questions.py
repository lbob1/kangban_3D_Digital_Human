"""System One 输出契约（Day6 P0-3，TDD §5.2）。

口径：
- 契约字段不得增删（TDD §5.2 原文 9 字段）；意图枚举 7 值封顶；
- 方案 A 裁决（2026-11-09）：laya 只做业务意图高置信快路由；is_sensitive
  不信任 laya（POC-03 实测召回 25%）——字段按契约保留，仅作信号，
  级联层敏感判定只认 safety.py 确定性词表 + 主控 Agent prompt 双保险；
- 派生字段口径（laya 原生不输出，由 provider 派生并留档）：
    needs_clarification = confidence < 置信阈值（默认 0.85）
    is_out_of_domain    = intent == other
    secondary_intents   = []（laya 单标签输出，多意图留主控 Agent 兜底）
    privacy_restricted  = False（D8：契约就绪，行为留 Day7+）
    sensitive_types     = []（laya 无类型输出）
- 置信度阈值 0.85 只有评测验证后才能调整（执行提示词 §技术规范 2）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# laya multilingual checkpoint（POC-03 spike 验证通过的 pinned revision）
LAYA_MODEL_ID = "convaiinnovations/laya-multilingual"
LAYA_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"


class IntentLabel(str, Enum):
    """意图枚举（TDD §5.2，7 值封顶）。"""

    CHAT = "chat"
    MEDICATION = "medication"
    OPERA = "opera"
    MOVIE = "movie"
    WEATHER = "weather"
    HEALTH = "health"
    OTHER = "other"


class SystemOneResult(BaseModel):
    """System One 输出契约：字段与 TDD §5.2 一一对应，不得增删。"""

    intent: IntentLabel
    is_sensitive: bool
    sensitive_types: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool
    privacy_restricted: bool
    is_out_of_domain: bool
    secondary_intents: list[IntentLabel] = Field(default_factory=list)
    model_version: str | None = None


def build_laya_questions() -> dict:
    """laya 原生 question schema（与 POC-03 spike 实证一致，勿随意改动措辞）。"""
    return {
        "intent": {
            "type": "choice",
            "instructions": "用户这句话的主要意图分类",
            "criteria": {
                "chat": "闲聊问候、陪伴交谈",
                "medication": "用药、吃药、服药相关",
                "opera": "想听戏曲（越剧/京剧等）",
                "movie": "想看电影、视频",
                "weather": "询问天气",
                "health": "询问健康状况、血压等健康数据",
                "other": "以上都不是",
            },
        },
        "is_sensitive": {
            "type": "noul",
            "instructions": (
                "这句话是否涉及急症、跌倒、走失、情绪危机等需要工作人员立即介入的敏感情况"
            ),
        },
    }
