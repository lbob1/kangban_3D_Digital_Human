"""医院对接 mock（Day8 D6）：函数级模拟转诊，不建 referral 表；真实对接留 Day10 后。

- 返回假转诊单号 + 可配延迟（HOSPITAL_MOCK_DELAY_MS）营造演示真实感；
- 留痕不落本模块：调用方（健康图 doctor_resolve 节点）将转诊单号写 graph state
  并追加进工单 description（WorkorderAction 由确认端点以真实账号身份写入）。
"""

import asyncio
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


async def create_referral(elder_name: str, reason: str, *, delay_ms: int | None = None) -> str:
    """模拟创建转诊单：延迟 HOSPITAL_MOCK_DELAY_MS（默认 800ms）后返回假单号。"""
    if delay_ms is None:
        delay_ms = get_settings().hospital_mock_delay_ms
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    referral_no = f"REF-{datetime.now(UTC).date().isoformat()}-{uuid.uuid4().hex[:8].upper()}"
    logger.info(
        "hospital mock referral created",
        extra={
            "event": "hospital_referral_created",
            "referral_no": referral_no,
            "elder_name": elder_name,
        },
    )
    return referral_no
