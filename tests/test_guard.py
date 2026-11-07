"""Day5 死循环防护框架单测（大纲 5.4，D4 决策）。

纯框架测试，注入假时钟：同 Tool 第 3 次熔断 / 窗口过期自动恢复 /
第 8 次全局上限 / reset_turn / 不同 Tool 互不影响 / 动作防抽搐复用。
"""

from app.services.guard import (
    CIRCUIT_THRESHOLD,
    CIRCUIT_WINDOW_SECONDS,
    MAX_TOOL_CALLS_PER_TURN,
    ToolCallGuard,
)


def test_same_tool_third_call_denied():
    guard = ToolCallGuard()
    assert guard.record("query_weather", now=0.0).allowed
    assert guard.record("query_weather", now=1.0).allowed
    verdict = guard.record("query_weather", now=2.0)  # 窗口内第 3 次
    assert not verdict.allowed
    assert verdict.reason == "same_tool_circuit_open"


def test_window_expiry_recovers_automatically():
    guard = ToolCallGuard()
    assert guard.record("query_weather", now=0.0).allowed
    assert guard.record("query_weather", now=1.0).allowed
    assert not guard.record("query_weather", now=2.0).allowed
    # 窗口（60s）过后旧记录失效：可再次调用
    verdict = guard.record(
        "query_weather", now=CIRCUIT_WINDOW_SECONDS + 1.0
    )
    assert verdict.allowed


def test_denied_call_does_not_extend_window():
    guard = ToolCallGuard()
    guard.record("t", now=0.0)
    guard.record("t", now=1.0)
    guard.record("t", now=59.0)  # 被拒，不记账
    # 若被拒也记账，此处将仍在熔断窗口内而误拒
    assert guard.record("t", now=CIRCUIT_WINDOW_SECONDS + 0.5).allowed


def test_different_tools_independent():
    guard = ToolCallGuard()
    for i in range(CIRCUIT_THRESHOLD - 1):
        assert guard.record(f"tool_{i}", now=i).allowed
    assert guard.record("other_tool", now=10.0).allowed


def test_turn_budget_eighth_call_denied():
    guard = ToolCallGuard()
    for i in range(MAX_TOOL_CALLS_PER_TURN - 1):
        verdict = guard.record(f"tool_{i}", now=float(i))
        assert verdict.allowed, f"第 {i + 1} 次应放行"
    verdict = guard.record("one_more", now=99.0)  # 第 8 次：全局上限
    assert not verdict.allowed
    assert verdict.reason == "turn_tool_budget_exhausted"


def test_reset_turn_clears_all_state():
    guard = ToolCallGuard()
    guard.record("query_weather", now=0.0)
    guard.record("query_weather", now=1.0)
    guard.reset_turn()
    assert guard.record("query_weather", now=2.0).allowed  # 熔断解除
    for i in range(MAX_TOOL_CALLS_PER_TURN - 1):
        guard.record(f"t{i}", now=3.0 + i)
    # reset 后全局预算重新计数
    guard2 = ToolCallGuard()
    guard2.reset_turn()
    assert guard2.record("t", now=0.0).allowed


def test_action_suppression_reuses_same_guard():
    """动作防抽搐：同一动作 60s 内第 3 次停止下发（同款逻辑复用）。"""
    guard = ToolCallGuard()
    assert guard.record("action:nod", now=0.0).allowed
    assert guard.record("action:nod", now=20.0).allowed
    assert not guard.record("action:nod", now=40.0).allowed  # 60s 窗内第 3 次
