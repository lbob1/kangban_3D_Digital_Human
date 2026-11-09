== Day6 三级路由评测基线（50 条，词表/规则/laya 真实推理） ==
用例：50（safety 8 / rule 12 / system_one 10 / agent 20）

## 分层占比（实际路由）
- agent: 19 (38%)
- rule: 12 (24%)
- safety: 8 (16%)
- system_one: 11 (22%)

## 意图准确率（safety+rule+system_one 共 28 条，目标 ≥90%）
- 27/28 = 96.4% → 达标

## 敏感召回（目标 ≥95%）
- 8/8 = 100.0% → 达标

## 闭环率（不经主控即完成应答的占比）
- 62%（rule/safety/system_one 层直答；闭环率按路由口径，回答质量归 Day7 记忆/对话评估）
- S1 降级轮数（degraded=true，超时/未就绪）：0

## 各层端到端路由延迟（decide 墙钟，含 laya warm 推理）
- safety: P50=0.0ms P95=0.0ms n=8
- rule: P50=0.0ms P95=0.0ms n=12
- system_one: P50=113.6ms P95=121.1ms n=11
- agent: P50=114.2ms P95=119.1ms n=19

## 成本对比口径（每 50 轮 LLM 调用次数估算）
- Day5 基线：每轮 1 次流式调用 = 50 次
- Day6 实测：agent 轮 19 次（决策轮，直答免二次） + 精简答复按 POC-08 实测单列
