"""动作标记协议（整句版）测试：解析硬规则 + 规则触发表 + 动作表加载降级。"""

from pathlib import Path

import pytest

from app.services import actions as actions_mod
from app.services.actions import (
    DEFAULT_ACTIONS_PATH,
    ActionCatalog,
    ActionDef,
    apply_reply_rules,
    get_catalog,
    load_catalog,
    parse_action_markers,
)


def make_catalog(
    marker: dict | None = None,
    action_ids: tuple[str, ...] = ("wave", "nod", "bye"),
) -> ActionCatalog:
    return ActionCatalog(
        actions=tuple(ActionDef(id=i, label=i) for i in action_ids),
        marker=marker or {"max_per_reply": 2, "no_repeat_consecutive": True},
    )


# ---------- parse_action_markers：解析硬规则 ----------


def test_parse_basic_marker():
    text, acts = parse_action_markers("[act:wave]您好呀", make_catalog())
    assert text == "您好呀"
    assert acts == [("wave", None)]


def test_parse_repeat_valid_param():
    text, acts = parse_action_markers("[act:nod:2]好的", make_catalog())
    assert text == "好的"
    assert acts == [("nod", "2")]


@pytest.mark.parametrize("raw", ["[act:nod:9]", "[act:nod:0]", "[act:nod:abc]"])
def test_repeat_invalid_downgrades_to_once(raw):
    text, acts = parse_action_markers(f"{raw}好的", make_catalog())
    assert text == "好的"
    assert acts == [("nod", None)]


def test_unknown_id_dropped_but_marker_stripped():
    text, acts = parse_action_markers("[act:dance]你好", make_catalog())
    assert text == "你好"
    assert acts == []


def test_max_two_markers():
    text, acts = parse_action_markers("[act:wave][act:nod][act:bye]好的", make_catalog())
    assert text == "好的"
    assert acts == [("wave", None), ("nod", None)]


def test_consecutive_same_action_dropped():
    text, acts = parse_action_markers("[act:wave][act:wave]你好", make_catalog())
    assert text == "你好"
    assert acts == [("wave", None)]


def test_marker_case_insensitive():
    text, acts = parse_action_markers("[ACT:Wave]你好", make_catalog())
    assert text == "你好"
    assert acts == [("wave", None)]


def test_no_marker_passthrough_unchanged():
    text, acts = parse_action_markers("你刚才说的是：你好", make_catalog())
    assert text == "你刚才说的是：你好"
    assert acts == []


def test_marker_never_enters_text_even_when_all_invalid():
    text, acts = parse_action_markers(
        "[act:dance][act:wave][act:nod][act:bye]好的", make_catalog()
    )
    assert "[" not in text and "act" not in text
    assert acts == [("wave", None), ("nod", None)]


def test_parse_with_default_marker_rules_when_empty():
    text, acts = parse_action_markers("[act:wave][act:nod][act:bye]好", make_catalog(marker={}))
    assert acts == [("wave", None), ("nod", None)]
    assert text == "好"


def test_catalog_unavailable_strips_all_markers_without_actions(monkeypatch):
    monkeypatch.setattr(actions_mod, "get_catalog", lambda path=None: None)
    text, acts = parse_action_markers("[act:wave]你好")
    assert text == "你好"
    assert acts == []


# ---------- apply_reply_rules：规则触发表 ----------


def test_rule_hello_prepends_wave():
    assert apply_reply_rules("你好呀，今天不错").startswith("[act:wave]")


def test_rule_bye_and_thanks():
    out = apply_reply_rules("再见，谢谢您")
    assert out.startswith("[act:bye][act:nod]")


def test_rule_cap_at_two_and_consecutive_dedupe():
    # 谢谢/好的同属 nod 组：命中后连续去重，最终 wave + nod 两个
    out = apply_reply_rules("你好，谢谢，好的")
    assert out == "[act:wave][act:nod]你好，谢谢，好的"


def test_rule_no_hit_returns_original():
    assert apply_reply_rules("今天天气不错") == "今天天气不错"


def test_rule_empty_text_unchanged():
    assert apply_reply_rules("") == ""
    assert apply_reply_rules("   ") == "   "


def test_rule_output_parses_back_through_pipeline():
    cleaned, acts = parse_action_markers(apply_reply_rules("你好，再见"), make_catalog())
    assert acts == [("wave", None), ("bye", None)]
    assert cleaned == "你好，再见"


# ---------- 动作表加载：真实文件与降级 ----------


def test_load_real_actions_catalog():
    catalog = load_catalog(DEFAULT_ACTIONS_PATH)
    assert len(catalog.actions) == 9
    assert catalog.allowed_ids == frozenset(
        {"wave", "bye", "nod", "shake", "clap", "think", "tilt", "comfort", "stretch"}
    )
    assert catalog.marker.get("max_per_reply") == 2
    assert catalog.marker.get("no_repeat_consecutive") is True


def test_get_catalog_missing_file_returns_none():
    assert get_catalog(Path("Z:/definitely/not/here/actions.json")) is None


def test_pipeline_with_real_catalog():
    catalog = get_catalog()
    assert catalog is not None
    cleaned, acts = parse_action_markers(apply_reply_rules("你好"), catalog)
    assert acts == [("wave", None)]
    assert cleaned == "你好"
