# HARNESS_DESIGN · Agent Harness 设计文档

> 生成：2026-11-12（Day10 步骤 8，大纲 10.4）｜本文档**汇总既有 ADR 与实现**，不引入新决策。
> 代码锚点：`app/router/cascade.py`、`app/services/companion.py`、`app/services/guard.py`、`app/services/voice.py`、`app/graphs/`。

## 1. 设计立场

「Agent Harness」在本项目里指：**模型不直接面对老人与系统副作用，中间的约束层才是产品本体**。
三条设计公理（源自 ADR-003/009/022）：

1. 确定性的事交给确定性代码——能不走模型就不走（成本/延迟/可测试性）；
2. 敏感判断不信任概率模型——词表一票否决优先于任何分类器；
3. 高危副作用默认不自动执行——确认（HITL）是图内节点，不是 prompt 里的「请先问我」。

## 2. 分层决策架构（ADR-003/022，`app/router/cascade.py`）

```
输入（语音转写/文字）
  → ①敏感词表（六类，隔字正则，一票否决：只播安抚话术+敏感工单，0 模型）
  → ②规则层（10 条表驱动固定指令：停/音量/报时/天气/告别/换一段/点播/用药播报…，0 模型）
  → ③System One（laya 0.4.30 本地权重，固定业务意图分类，置信度阈值 0.85，200ms 超时降级；
     只分类不生成自由文本——ADR-022 方案 A）
  → ④主控 CompanionAgent（ReAct + 工具，开放域兜底）
```

- 每一层决策落 `decision_logs`（layer/intent/confidence/latency/tool_calls/escalated）——
  这是 trace、metrics、评测三方共用的唯一事实源；
- 执行类动作不参与多意图合并；工具调用跨层合并计数（cascade 层职责）。

## 3. ReAct 循环与单轮执行（`app/services/companion.py`）

- `run_turn` 一轮循环：LLM 决策 → 工具调用（经 Guard）→ 观察回填 → 流式终答；
- 单轮封装（无跨轮记忆漂移）：会话上下文由调用方组装传入，Agent 本身无状态；
- 终答只输出给老人的口语正文，`[act:*]` 动作标记由解析器剥离单独下发（不朗读不进字幕）。

## 4. 上下文组装（`app/services/voice.py`）

System prompt 分段拼装，**安全段永远在前**：

1. 人设主干（`app/prompts/personas/elder_companion.md`：语气/安全边界/输出约束/few-shot 分寸示例）；
2. 动作协议（`build_action_prompt`：可用动作表+格式约束，未知动作丢弃）；
3. 记忆块（召回 top-5：强制 `elder_id+visibility+status` 三件套过滤 + LLM 二次过滤）；
4. 紧急观察规则（急症词提示线）。

配置红线：阈值与分级规则**在代码里，不进 prompt**——prompt 只影响语气，不影响权限。

## 5. 工具防护闸（`app/services/guard.py`，ToolCallGuard）

| 机制 | 口径 | 目的 |
| --- | --- | --- |
| 单轮预算 | 工具调用 ≤8 次/轮，超限拒绝码返回 | 防循环调用与成本失控 |
| 同工具熔断 | 同一工具连续失败进熔断窗口 | 防对外部服务的打点式重试 |
| 未注册拦截 | 工具名不在注册表 → 拦截 + `hallucination_block_total` 计数 | 幻觉工具硬防线 |
| 每轮重置 | 计数随轮次重置 | 预算语义限定单轮 |

配套第四防线 Tool：`report_emergency`（模型可主动呼救上报，但上报本身也留痕可审）。

## 6. 幻觉防护三层（评测硬线）

1. 确定性层前置：敏感/规则路径不经过模型，无从幻觉；
2. 运行时拦截：Guard 未注册拦截 + 编造性回答诱导场景（Day9 hal-01~04）实测拦截率 **100%**；
3. 评测门禁：`eval/gate.py` `hallucination_block_rate=1.0` 为一票否决硬线，接 pre-push hook——
   指标不达标推不出代码。

## 7. 降级链 / Recovery（每层都有「坏了怎么办」）

| 依赖 | 失败模式 | 降级行为 | 依据 |
| --- | --- | --- | --- |
| System One | 超时/未就绪（200ms） | degraded=true 直通下一层（评测口径单独归因） | ADR-022 |
| LLM API | 首 token >3s | 拟真占位语「嗯~我想想哈。」每轮 ≤1 次 | Day5 熔断二档 |
| LLM 不可用 | API 异常 | 模板话术回复，通话不中断 | Day4 可见降级 |
| ASR | 终句 >2s / 20s 硬超时 | 「没听清请再说一遍」/ 防永久挂死 | Day5/Day7+ |
| TTS | 首包 >1s | 字幕先行；失败重试 1 次并上屏真实错误 | Day5/Day7+ |
| MCP 工具 | 外部服务失败 | 本地降级路径（契约不变），WS error 事件可见 | ADR-006/014 |
| 用药外呼 | 长者离线 | 退避 3 次 → escalated + 漏服工单 | ADR-023 |
| FunASR/声纹未配置 | 部署环境缺失 | 应用照常启动，WS error + 文字通道 | AGENTS.md 实时规则 |

原则：**可见降级，绝不静默失败**——每次降级都有事件/日志/评测归因。

## 8. 有状态长流程：LangGraph 双图（ADR-004/023/026）

- **用药图**：`calling→waiting_confirm→confirmed/retry(≤3)/escalated`；SQLite checkpointer
  断点续跑；`thread_id=med-{plan}-{date}-{slot}` 幂等；定时外呼与对话内确认共用同一图；
  HITL 模式=**口头确认**（classify_reply）。
- **健康图**：三分支 green/yellow/red + **双 interrupt**（yellow→护工、red→医生），
  三通道汇同一图；确认前零通知；副作用（建单/通知/转诊）只在图内放行；
  workqueue 端点只以 `Command(resume)` 驱动图。

## 9. 观测与评测闭环（ADR-011）

- 决策留痕（decision_logs）→ trace_lookup 一条命令串全链 + 自包含 HTML 报告（D2 纯本地）；
- `/metrics` 聚合同一事实源，口径与 eval/gate.py 对齐；
- 评测基线（33 场景七类）+ 阈值门禁进 pre-push —— **Harness 的行为被测试锁死**：
  分层命中、隔离红线、幻觉拦截、HITL 零提前通知均有自动化断言。

## 10. ADR 索引（Harness 相关）

ADR-003（三级路由与受约束主控）· ADR-004（仅长流程用 LangGraph）· ADR-006（MCP 封装）·
ADR-009（高危副作用必须 HITL）· ADR-014（Provider 适配器+POC 决定）· ADR-021（会话状态 PG）·
ADR-022（System One 方案 A）· ADR-023（用药图）· ADR-025（点播双通道汇合）· ADR-026（健康三分支双 interrupt）
