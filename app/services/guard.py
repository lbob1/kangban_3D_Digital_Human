"""Tool/动作死循环防护框架（大纲 5.4「Loop 七大风险」，Day5 立框架、Day6 生效）。

设计口径（D4 决策）：Day5 只交付可独立单测的纯框架——不接入任何既有循环；
Day6 主控 Agent 接入时在每次 Tool 调用前 `record(tool_id)`、每轮开始 `reset_turn()`，
对 guard.py 零改动。动作下发防抽搐用同一类（同一动作 60s 内 ≥3 次 → 停止下发）。

「Loop 七大风险」其余项在本文件只登记口径、不实现（Day6+）：
- 上下文膨胀：窗口内消息条数/token 预算上限，超限截断历史（Day6 记忆摘要协同）；
- 成本失控：单轮 token/调用费用累计上限，超限降级模板回复；
- 高危副作用：Tool 白名单 + HITL 确认（对齐安全禁区 5，工具侧另行实现）；
- 提示注入：外部工具返回内容消毒与来源标注（Day6+ 安全评审）；
- 超时传播：Tool 级 timeout 与整体 turn deadline 联动（复用 Day5 熔断三档口径）。
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass

MAX_TOOL_CALLS_PER_TURN = 8  # 单轮全局上限（大纲值）
CIRCUIT_WINDOW_SECONDS = 60  # 同一 Tool 熔断观察窗
CIRCUIT_THRESHOLD = 3  # 窗口内第 3 次同 Tool 调用 → 熔断（前 2 次放行）


@dataclass(frozen=True)
class GuardVerdict:
    """单次调用判定：allowed=False 时 reason 为结构化拒绝码。"""

    allowed: bool
    reason: str | None = None


class ToolCallGuard:
    """单会话实例；线程不安全（事件循环内单任务使用，与 aiortc 规则一致）。"""

    def __init__(self) -> None:
        self._total = 0
        self._window: dict[str, deque[float]] = defaultdict(deque)

    def record(self, tool_id: str, now: float | None = None) -> GuardVerdict:
        """调用前判定并记账；now 供单测注入时钟。"""
        current = time.monotonic() if now is None else now
        # 单轮全局预算：本次将是第 8 次（MAX=8）→ 直接拒绝（前 7 次放行）
        if self._total + 1 >= MAX_TOOL_CALLS_PER_TURN:
            return GuardVerdict(False, "turn_tool_budget_exhausted")
        # 同 Tool 熔断：先清出窗外旧记录，再判窗口内次数
        window = self._window[tool_id]
        cutoff = current - CIRCUIT_WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= CIRCUIT_THRESHOLD - 1:
            # 已有 2 次在窗内 → 本次为第 3 次，拒绝且不记账（不延长窗口）
            return GuardVerdict(False, "same_tool_circuit_open")
        window.append(current)
        self._total += 1
        return GuardVerdict(True)

    def reset_turn(self) -> None:
        """每轮回复开始时重置（全局预算与窗口一并清空）。"""
        self._total = 0
        self._window.clear()
