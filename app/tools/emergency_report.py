"""report_emergency Tool（Day6+ 批 2，用户裁决「宁多勿漏」第四道防线）。

定位：
- ①词表（确定性主防线）之外，主控 Agent 对话中由 LLM 识别隐晦紧急语义
  （「喘不动气感觉熬不过去」「有人对我动手」等词表抓不住的表达）时调用本工具；
- 宁多勿漏：LLM 判断疑似即发工单（偶发误报可接受，漏报不可接受——康养安全定位）；
- 工单 description 前缀「[LLM观察]」标注来源，处理人可据此区分词表命中/LLM 观察；
- 不替代安抚：LLM 调用本工具后仍须自行生成安抚回复（工具只落工单不返回话术）；
- elder_id 只来自 runtime_ctx（服务端注入，AGENTS 禁区 8）；未验证长者时拒绝
  （工单必须可归户，无法归户的紧急陈述由词表层兜底流程处理）。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import get_logger
from app.db.session import get_session_factory

logger = get_logger("tools.emergency")

# LLM 可选类目：词表六类 + other（LLM 判断模糊但担心时用，宁多勿漏）
VALID_CATEGORIES = (
    "acute_symptom",
    "fall",
    "lost",
    "emotional_crisis",
    "emergency_call",
    "abuse",
    "other",
)

_DESC_SANITIZE = re.compile(r"[\x00-\x1f]+")  # 控制字符清洗（LLM 输出不信任）


async def report_emergency_handler(args: dict, runtime_ctx: dict) -> dict[str, Any]:
    """主控 LLM 观察到疑似紧急 → 落 workorders 工单（type=sensitive_alert）。"""
    category = args.get("category", "other")
    category = category if category in VALID_CATEGORIES else "other"
    description = _DESC_SANITIZE.sub(" ", str(args.get("description", ""))).strip()[:300]

    elder_id = runtime_ctx.get("elder_id")
    if not elder_id:
        # 无法归户：词表主防线没拦住且长者未声纹验证——记日志，不落库（工单必须可归户）
        logger.warning(
            "emergency report without verified elder, dropped",
            extra={"event": "emergency_report_dropped", "category": category},
        )
        return {"reported": False, "reason": "elder_not_verified"}

    from app.models.enums import WorkorderPriority, WorkorderStatus
    from app.models.workorder import Workorder

    async with get_session_factory()() as db:
        db.add(
            Workorder(
                elder_id=elder_id,
                type="sensitive_alert",
                category=category,
                status=WorkorderStatus.OPEN,
                priority=WorkorderPriority.URGENT,
                title=f"敏感事件[LLM观察]：{category}",
                description=description or "（LLM 判断为疑似紧急，无附述）",
            )
        )
        await db.commit()

    logger.info(
        "emergency reported via llm watcher",
        extra={"event": "emergency_reported", "category": category},
    )
    return {"reported": True, "category": category}


TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(VALID_CATEGORIES),
            "description": (
                "紧急类型：acute_symptom 身体急症、fall 跌倒、lost 走失、"
                "emotional_crisis 情绪危机/轻生倾向、abuse 虐待或被打、"
                "emergency_call 呼救、other 疑似紧急但类型不明"
            ),
        },
        "description": {
            "type": "string",
            "description": "长者原话或观察到的紧急情况简述（≤300 字）",
        },
    },
    "required": ["category", "description"],
    # TDD §5.3：schema 不含 elder_id（服务端 Runtime Context 注入）
}


def build_tool_spec():
    from app.tools.registry import ToolSpec

    return ToolSpec(
        name="report_emergency",
        description=(
            "发现长者疑似紧急情况（身体急症/跌倒/走失/轻生倾向/虐待/呼救）时调用，"
            "立即上报紧急工单。宁可多报不可漏报；上报后仍须继续安抚长者。"
        ),
        schema=TOOL_SCHEMA,
        handler=report_emergency_handler,
    )
