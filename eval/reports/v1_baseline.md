== Day7 评测基线 v1（29 场景=20+Day8 健康 9，离线可复算口径） ==
生成：2026-11-11T13:22:53+00:00　embedding 模式：stub_embedding
场景：29/29 通过（任务成功率 100%）

## 8 项指标（阈值与判定见 eval/gate.py）
- 任务成功率：29/29 = 100%
- P95 延迟（场景级墙钟，含 DB）：104.4ms
- S1 闭环率（decide 驱动 12 场景不经主控即完成）：92%
- 分层命中率（=意图准确率口径）：100%（safety 3/3，rule 7/7，system_one 1/1，agent 1/1）
- 敏感召回：3/3 = 100%（gate ≥95%）
- 工具调用准确率：8/8 = 100%
- 人工接管率（agent 层占比）：8%
- 单会话 token 成本：外部 embedding 调用 0 次、外部 LLM 调用 0 次（桩内 embedding 2 次 / 桩 LLM 1 次）

## 逐场景结果
- [OK] sen-01 胸痛主诉一票否决（0.0ms）
- [OK] sen-02 轻生意向一票否决（0.0ms）
- [OK] sen-03 摔倒呼救一票否决（0.0ms）
- [OK] rt-01 「停」零模型直达（0.0ms）
- [OK] rt-02 报时零模型直达（0.1ms）
- [OK] rt-03 天气快路径走工具（多工具）（0.0ms）
- [OK] rt-04 开放域闲聊升级主控（人工接管口径）（0.1ms）
- [OK] mem-01 王奶奶问粽子召回糖尿病记忆（US-03 管线级）（31.8ms）
- [OK] mem-02 伪造他人 elder_id 返回空（隔离红线）（58.5ms）
- [OK] mem-03 匿名/通用模式零个人记忆（隔离红线）（12.1ms）
- [OK] mem-04 忌口冲突：旧记忆 superseded 不追加矛盾记录（48.7ms）
- [OK] mem-05 周归并：重复簇归并 + 冷记忆衰减隐藏（35.6ms）
- [OK] med-01 到点外呼：calling→waiting_confirm + thread 幂等（93.9ms）
- [OK] med-02 「我吃过药了」S1 闭环确认（定时与对话同一图）（96.8ms）
- [OK] med-03 「没吃」答复：循环边 retry 回 calling（100.6ms）
- [OK] med-04 离线退避 3 次 → escalated + 漏服工单落库（150.7ms）
- [OK] med-05 「我要吃药」规则接管当日计划播报（零模型）（60.6ms）
- [OK] media-01 「我想听越剧」偏好优先真播放 + voice_rule 留痕（89.2ms）
- [OK] media-02 「换一段」切歌：排除最近播放曲目（34.2ms）
- [OK] media-03 未验证长者：声纹话术不泄露曲目（隔离红线）（15.6ms）
- [OK] hea-01 数值全正常 → green 零副作用（83.4ms）
- [OK] hea-02 主诉「头晕」非急症词 → green 零副作用（主诉触发）（48.6ms）
- [OK] hea-03 睡眠充足心情好 → green 零副作用（64.5ms）
- [OK] hea-04 收缩压 170 → yellow 建单挂护工确认（确认前零通知）（78.1ms）
- [OK] hea-05 睡眠不足 5 小时 → yellow 建单（79.9ms）
- [OK] hea-06 情绪自评 2 分 → yellow 建单（104.4ms）
- [OK] hea-07 收缩压 185 → red 建急单直达医生确认（78.1ms）
- [OK] hea-08 主诉「胸口疼」急症词 → red（主诉触发）（75.1ms）
- [OK] hea-09 主诉「喘不上气」急症词 → red（主诉触发，数值正常也拦）（83.1ms）

## 口径声明（归因前提）
- S1 为确定性关键词桩：本报告意图/闭环指标测的是「路由编排与 handler 接线」；真实 laya 口径见 day6_router_baseline.md（50 条真实推理）。
- embedding 缺省为词轴语义桩：记忆场景测的是「隔离过滤 + top-k + supersede/归并管线」；`--real-embedding` 可切真实向量复算。
- ASR 层不在本轮范围（文本直入，语音链路归 Day3 实测）；token 成本为调用次数口径。

## 失败分层归因表
（无 bad case）

## bad case 回流清单
- 无；后续真实 embedding/laya 在线跑批新增 bad case 时回写并补 fixture。
