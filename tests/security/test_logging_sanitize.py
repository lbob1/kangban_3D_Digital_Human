"""Day9 步 4：日志脱敏 sanitize 层测试（大纲 9.2，主文档 §4.2 / D7）。

断言口径：构造含明文敏感串的日志记录 → JsonFormatter 输出 JSON 中 grep 不到
原文（身份证/手机号/剂量三类），mask 占位符出现；extra 字符串值与异常文本同口径。
"""

import json
import logging

from app.core.logging import MASKED_DOSE, MASKED_ID, MASKED_PHONE, JsonFormatter, sanitize_text

_ID = "110105194912300021"
_PHONE = "13812345678"
_DOSE = "每日 10.5 mg"
_PLAIN = "普通日志内容不含敏感串"


def _format_record(msg: str, extra: dict | None = None, exc: Exception | None = None) -> dict:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
    )
    for k, v in (extra or {}).items():
        setattr(record, k, v)
    rendered = JsonFormatter().format(record)
    return json.loads(rendered)


def _assert_no_plain_text(payload: dict, *secrets: str) -> None:
    rendered = json.dumps(payload, ensure_ascii=False)
    for secret in secrets:
        assert secret not in rendered, f"日志明文泄漏敏感串：{secret}"


def test_id_card_masked_in_message():
    """攻击面：身份证号明文进日志 message → 期望：***MASKED-ID***，原文 grep 不到。"""
    payload = _format_record(f"登记人身份证 {_ID} 校验通过")
    _assert_no_plain_text(payload, _ID)
    assert MASKED_ID in payload["message"]


def test_phone_masked_in_message():
    """攻击面：手机号明文进 message → 期望：***MASKED-PHONE***，原文 grep 不到。"""
    payload = _format_record(f"家属电话 {_PHONE} 已登记")
    _assert_no_plain_text(payload, _PHONE)
    assert MASKED_PHONE in payload["message"]


def test_dose_masked_in_message():
    """攻击面：药品剂量明文进 message（大纲 9.2 点名项）→ 期望：***MASKED-DOSE***。"""
    payload = _format_record(f"医嘱：{_DOSE}，饭后服用")
    _assert_no_plain_text(payload, "10.5")
    assert MASKED_DOSE in payload["message"]


def test_extra_string_values_sanitized():
    """攻击面：敏感串藏在 extra 结构化字段（如 object_id/描述性字段）→
    期望：字符串值统一过 sanitize，grep 不到原文。"""
    payload = _format_record(
        "敏感事件上报",
        extra={"event": "sensitive_reported", "result": f"工单描述含 {_ID} 与 {_PHONE}"},
    )
    _assert_no_plain_text(payload, _ID, _PHONE)


def test_exception_text_sanitized():
    """攻击面：异常堆栈夹带敏感串（如 SQL 参数回显）→ 期望：exception 字段同样 mask。"""
    payload = _format_record("落库失败", exc=ValueError(f"invalid id {_ID} dose {_DOSE}"))
    _assert_no_plain_text(payload, _ID, "10.5")


def test_id_and_phone_combined_no_cross_masking():
    """顺序铁律：身份证（18 位）先于手机号 mask——组合串不被手机号正则吃掉中段。"""
    combined = f"身份证{_ID}手机{_PHONE}"
    out = sanitize_text(combined)
    assert _ID not in out and _PHONE not in out
    assert MASKED_ID in out and MASKED_PHONE in out


def test_plain_log_untouched():
    """正常路径：无敏感串的日志内容零改动（脱敏只动敏感串，不伤正文）。"""
    payload = _format_record(_PLAIN)
    assert payload["message"] == _PLAIN


def test_non_string_values_pass_through():
    """非字符串 extra 值（数字/None）不受影响，日志结构不变。"""
    payload = _format_record("耗时", extra={"duration_ms": 12.5, "event": "timing"})
    assert payload["duration_ms"] == 12.5
    assert payload["event"] == "timing"


def test_mixed_string_multiple_patterns():
    """混合串：一句话同时含三类敏感串 → 三种占位符齐现、原文全灭。"""
    text = f"身份证{_ID}，电话{_PHONE}，剂量{_DOSE}"
    out = sanitize_text(text)
    _assert_no_plain_text({"message": out}, _ID, _PHONE, "10.5")
    assert all(m in out for m in (MASKED_ID, MASKED_PHONE, MASKED_DOSE))
