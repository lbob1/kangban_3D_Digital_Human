"""规则层（Day6，大纲 6.1）：10 条表驱动确定性规则，命中即直达 handler。

口径（Day6 主文档 §4.2）：
- 10 条封顶不追加；规则克制红线，数据结构化（id/正则/handler/预告话术）；
- 纯本地指令零模型调用零 Token；「今天/明天 + 天气」为规则快路径直达 MCP Tool
  （D9 双路径，日期在规则内收口由 handler 解析）；其余变体表达（「下不下雨」等）
  不命中规则，交 System One 汇合同一管线；
- 「换一段 / 听越剧京剧 / 我要吃药」Day6 走受控预告话术，Day7 接管同规则换 handler；
- 规则命中也落 decision_logs（route_layer=rule, model_version=null）——由 cascade 负责。
- handler 的实际执行在 cascade（P1-5）；本模块只做匹配与决策产出，保持纯函数可单测。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# 尾部标点/空白归一化：ASR 终句可能带句末标点，精确词规则不受影响
_TRAILING_PUNCT = re.compile(r"[！!。.？?，,～~\s]+$")


@dataclass(frozen=True)
class Rule:
    """一条确定性规则：id + 正则 + 动作 id + 可选确定性回复文本。"""

    rule_id: str
    pattern: re.Pattern[str]
    action: str
    # 确定性回复文本：预告话术/收尾话术直接播报；事件类（停/音量）为 None
    reply_text: str | None = None


# 大纲 6.1 十条清单（顺序即优先级，首条命中即返回）
RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="stop_playback",
        pattern=re.compile(r"^停$"),
        action="stop_playback",
    ),
    Rule(
        rule_id="volume_up",
        # 「大声一点」「声音大一点」同一条规则内的表述收口（条数封顶 10 不追加）
        pattern=re.compile(r"大声|声音大"),
        action="volume_up",
    ),
    Rule(
        rule_id="volume_down",
        pattern=re.compile(r"小声|声音小"),
        action="volume_down",
    ),
    Rule(
        rule_id="tell_time",
        pattern=re.compile(r"几点"),
        action="tell_time",
    ),
    Rule(
        # D9 规则快路径：今天/今日/明天/明日/后天 + 天气 同一条规则内收口
        # （条数封顶 10 不追加）；具体日期由 weather handler 从 rule.text 解析传参
        rule_id="weather_today",
        pattern=re.compile(r"(今天|今日|明天|明日|后天)[^。？！?！]{0,8}天气"),
        action="weather_fast_path",
    ),
    Rule(
        rule_id="farewell",
        pattern=re.compile(r"^再见$"),
        action="farewell",
        reply_text="好嘞，您先歇着，想聊天随时叫我。",
    ),
    Rule(
        rule_id="repeat_last",
        pattern=re.compile(r"重复|再说(一)?遍"),
        action="repeat_last",
    ),
    Rule(
        # Day7 步7 原位接管（同 rule_id/正则零改动，仅换 action）：media_next
        # → 换一段切歌（handler 步8 接真播放；当前保留 Day6 预告话术防回归）
        rule_id="media_next_preview",
        pattern=re.compile(r"换一(段|个|首)"),
        action="media_next",
        reply_text="戏曲点播功能就快上线啦，到时候想换哪段换哪段。",
    ),
    Rule(
        # Day7 步7 原位接管：opera_play → 戏曲点播（handler 步8 接真播放）；
        # Day7+ 扩展（十条封顶不追加，2026-11-10 用户实测反馈）：引导词扩
        # 「播放/放/来/点/唱」（原仅「听」，「播放音乐」漏匹配），
        # 词表扩「戏曲/戏剧」（「我想听戏曲」漏匹配）与「歌」（「想听歌」漏匹配）——
        # 「播放音乐/我想听戏曲」→ 命中；类型由 handler 分流（音乐|歌曲|老歌|歌→music）
        rule_id="opera_preview",
        pattern=re.compile(r"(听|播放|放|来|点|唱).{0,6}(越剧|京剧|黄梅戏|戏曲|戏剧|音乐|歌曲|老歌|歌)"),
        action="opera_play",
        reply_text="戏曲点播功能就快上线啦，到时候给您放越剧和京剧。",
    ),
    Rule(
        # Day7 步7 原位接管：medication_confirm 动态 handler——「我吃过药了」
        # （确认词）复入用药图 confirmed；「我要吃药」当日计划确定性播报
        rule_id="medication_preview",
        pattern=re.compile(r"吃(点)?药|服药"),
        action="medication_confirm",
        reply_text=None,
    ),
)


@dataclass(frozen=True)
class RuleMatch:
    """规则命中结果：cascade 据此执行 handler 并落 decision_logs。"""

    rule_id: str
    action: str
    reply_text: str | None
    latency_ms: float  # 实测匹配耗时（「停 <5ms」验收证据）
    text: str = ""  # 命中时的归一化原句（weather handler 解析日期用；单测构造可缺省）


def match_rule(text: str) -> RuleMatch | None:
    """按表顺序匹配第一条命中规则；无命中返回 None（交下一层）。"""
    start = time.perf_counter()
    normalized = _TRAILING_PUNCT.sub("", text.strip())
    for rule in RULES:
        if rule.pattern.search(normalized):
            elapsed = (time.perf_counter() - start) * 1000
            return RuleMatch(rule.rule_id, rule.action, rule.reply_text, elapsed, normalized)
    return None


# 信息类动作：同一句话里允许并列命中并合并回答（2026-11-10 用户决策：
# 「明天天气如何？现在几点了？」两问都必须回答）。执行类动作（停/音量/媒体/用药）
# 不参与合并——一次只执行一个动作，首条命中即返回。
_INFORMATIONAL_ACTIONS = frozenset({"tell_time", "weather_fast_path"})


def match_rules(text: str) -> list[RuleMatch]:
    """匹配全部命中规则；信息类（时间/天气）同句并列时按出现顺序合并返回。

    其余场景与 match_rule 等价（返回首条命中）；执行类规则永远不并列。
    """
    start = time.perf_counter()
    normalized = _TRAILING_PUNCT.sub("", text.strip())
    matches: list[tuple[int, Rule]] = []  # (first match position, rule)
    for rule in RULES:
        hit = rule.pattern.search(normalized)
        if hit is not None:
            matches.append((hit.start(), rule))
    if not matches:
        return []
    elapsed = (time.perf_counter() - start) * 1000
    first = matches[0][1]
    informational = [r for pos, r in sorted(matches) if r.action in _INFORMATIONAL_ACTIONS]
    if first.action not in _INFORMATIONAL_ACTIONS or len(informational) < 2:
        return [RuleMatch(first.rule_id, first.action, first.reply_text, elapsed, normalized)]
    return [
        RuleMatch(r.rule_id, r.action, r.reply_text, elapsed, normalized) for r in informational
    ]
