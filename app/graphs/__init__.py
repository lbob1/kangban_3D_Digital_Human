"""LangGraph 图模块（Day7 步6）。

- medication.py：用药确认图（calling/waiting_confirm/confirmed/retry/escalated）
- checkpointer.py：SQLite checkpointer（路径走 Settings，服务重启后 get_state 恢复断点）

为什么用 LangGraph 而非手写 if-else（大纲 Vibe 点）：本状态机有
「重试→回外呼」循环边、超时自动流转、服务重启断点恢复三需求——
状态图显式化节点/边/状态后，三者在图结构与 checkpointer 中天然成立，且逐档可测。
"""
