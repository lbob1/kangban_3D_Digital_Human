"""结构化 JSON 日志：标准库 logging 输出 JSON，通过 contextvars 注入 trace_id。

不记录请求正文与健康隐私内容；Day9 步 4 增加主动脱敏兜底（大纲 9.2）：
身份证号 / 手机号 / 药品剂量三类敏感串在落盘前 mask（LLM 回复、工单描述、
extra 字符串若意外夹带敏感串也不得明文出日志），测试以 grep 断言收口。
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

# ---- Day9 脱敏 sanitize（主文档 §4.2 顺序铁律：身份证 → 手机号 → 剂量）----

_ID_CARD_RE = re.compile(r"\d{17}[\dXx]")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_DOSE_RE = re.compile(r"\d+\.?\d*\s*(?:mg|毫克|片|粒)", re.IGNORECASE)

MASKED_ID = "***MASKED-ID***"
MASKED_PHONE = "***MASKED-PHONE***"
MASKED_DOSE = "***MASKED-DOSE***"


def sanitize_text(text: Any) -> str:
    """敏感串 mask（仅对 str 生效，其他类型原样返回）：

    顺序不可换——先长模式（18 位身份证，避免被手机号正则吃掉中段）后短模式；
    剂量模式最后（「10 mg」在身份证/手机号 mask 后的余文中独立存在）。
    """
    if not isinstance(text, str) or not text:
        return text
    sanitized = _ID_CARD_RE.sub(MASKED_ID, text)
    sanitized = _PHONE_RE.sub(MASKED_PHONE, sanitized)
    sanitized = _DOSE_RE.sub(MASKED_DOSE, sanitized)
    return sanitized


def get_trace_id() -> str | None:
    """获取当前请求的 trace_id；请求上下文外返回 None。"""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """绑定当前请求的 trace_id。"""
    trace_id_var.set(trace_id)


class JsonFormatter(logging.Formatter):
    """将日志记录渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_text(record.getMessage()),
        }
        trace_id = get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        # 附加结构化字段（来自 extra）——字符串值统一过 sanitize（主动脱敏兜底）
        for key in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "event",
            # Day2 审计留痕字段（audit_logs 表前的结构化日志替代方案）
            "action",
            "actor_id",
            "actor_role",
            "object_type",
            "object_id",
            "result",
            "idempotency_key",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = sanitize_text(value)
        if record.exc_info:
            payload["exception"] = sanitize_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class TraceIdFilter(logging.Filter):
    """确保 trace_id 存在于记录属性中，便于过滤器/格式器访问。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id()  # type: ignore[attr-defined]
        return True


def setup_logging() -> None:
    """初始化根日志：JSON 格式输出到 stdout。幂等，可重复调用。"""
    settings = get_settings()
    root = logging.getLogger()
    # 幂等：已配置过则只同步级别
    if getattr(root, "_kangban_json_configured", False):
        root.setLevel(settings.log_level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceIdFilter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    root._kangban_json_configured = True  # type: ignore[attr-defined]

    # 将 uvicorn 自带日志器统一接入 JSON 根日志，避免混入非结构化输出
    for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(uv_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """获取带 trace_id 注入的日志器。"""
    return logging.getLogger(name)
