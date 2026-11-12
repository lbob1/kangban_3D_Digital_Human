# Day9 验收报告（大纲 9.1–9.4 · 主文档 §9 16 项验收）

> 日期：2026-11-12　|　执行：AI 协作（三处高风险 diff 逐处人工审查通过）
> 基线：`604192e`（Day8 收尾）→ tag `iter5-demo`　|　alembic head `0018`（Day9 零迁移）
> 测试基线：开工实测收集 603 例（主文档 586 为 Day8 报告口径，差异=口径注记，经用户确认按实际执行）→ 收官实测收集 **702 例**（新增 99 例），全量全绿 · ruff 零新增告警 · eval gate exit 0

## 一、16 项验收对照

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 真机重启演练（收遗留②） | ✅ | 挂起→kill→重启→get_state→医生确认→resume→副作用全链路通过，留档 [Day9_真机重启演练.md](Day9_真机重启演练.md)；POC-06 真机口径收口 |
| 2 | 会话隔离闸门 | ✅ | OpenAPI 全站无 thread_id 入参（攻击面枚举）；人 JWT 打设备端点 403；未声纹 403；已验证只见本人块；B 凭证查 A 空集（gate1a–1e，8 例） |
| 3 | 记忆隔离闸门 | ✅ | pgvector WHERE 强制 elder_id（源码静态断言）+ 匿名短路 + 跨 elder 空集 + 混库 top-k 100% 归注入主体（gate2a–2d） |
| 4 | 工具授权闸门 | ✅ | 模型传受害者 elder_id 被忽略；7 工具 schema 全量静态无 elder_id；缺 ctx 拒绝（elder_not_verified）；正常路径归户正确（gate3*） |
| 5 | 注入测试集 ≥12 条 | ✅ | [injections.yaml](../tests/security/injections.yaml) 15 条（提示注入5/越权探测3/医疗诱导4/幻觉诱导2/命令注入1）参数化实测，拒绝断言逐条人读 |
| 6 | 漏拦清零 | ✅ | 步 3 实测漏拦 = **0**（15 条全部按期望层拦截）；如实注记：agent 层 11 条拒答行为以人设纪律静态断言 + 升级留痕验证（LLM 行为不 CI 化） |
| 7 | 日志脱敏 | ✅ | sanitize 层（身份证→手机号→剂量顺序铁律）+ message/extra/exception 统一过 mask，9 例 grep 断言明文零泄漏 |
| 8 | MCP workorder | ✅ | emergency_report 走 MCP（返回契约不变，既有 4 例原样绿）；mock JSONL 留痕 + 幂等（同 key 同工单号，跨重启）+ 3s 超时重试降级 ORM；真机子进程冒烟通过 |
| 9 | MCP family-notify | ✅ | mock outbox 留痕 + 无绑定 skip + elder_id 只取 runtime_ctx（schema 人审后注册）；真机冒烟 sent/skipped 通过 |
| 10 | MCP 审计表 | ✅ | 三 server 鉴权/超时/幂等/上下文边界逐项留档 [SECURITY.md](../SECURITY.md) §6.3 |
| 11 | 通知实时化（收遗留①） | ✅ | /ws/notify JWT+服务端范围过滤（护工本楼栋/家属医生有效关联/admin 全量）；双通道（日志必达+WS 尽力）；三端玻璃弹窗；真机 E2E 冒烟（确认前零通知 ✓ 秒收 ✓）+ 浏览器截图 [Day9_护工端WS通知toast截图.jpg](Day9_护工端WS通知toast截图.jpg) |
| 12 | 既有功能零回归 | ✅ | 603 既有例零回归全绿（emergency_report 既有测试原样通过，实证契约不变） |
| 13 | 核心工具覆盖率 | ✅ | app/tools 行覆盖 **100%**（596/596，mock 边界口径；含 MCP 客户端会话建立/复用/isError 重试/单例分支全覆盖，无不可达分支） |
| 14 | 幻觉 eval | ✅ | scene20 补批 4 条（hal-01..04：编造他人故事/虚构承诺/虚构用药事实/诱导虚构确认）→ **33 场景**全过；`hallucination_block_rate=1.0` 进 gate 硬线（阻塞项），gate exit 0 |
| 15 | SECURITY.md | ✅ | 六节齐：威胁模型（资产×入口×信任边界图）/ OWASP LLM Top10 逐条映射（防线+测试编号）/ 降级矩阵（五依赖）/ 数据保留策略 / HITL 六关卡清单 / 附录（后门自查+闸门矩阵摘要+MCP 审计表+通知安全口径） |
| 16 | 收尾 | ✅ | 三主题（陶瓷白/暖陶雅集/夜空黑）+ 375px 截图留档 [Day9_主题截图/](Day9_主题截图)（375px 以 CSS zoom 模拟，浏览器 resizeTo 受安全策略限制——如实注记）；pytest 全量 **702** 全绿（≥626 达标）；ruff 零新增；tag `iter5-demo` |

## 二、提交清单（10 笔，全程未 push）

| commit | 内容 |
| --- | --- |
| `4825926` | 步1 真机重启演练留档 |
| `ee82e9b`+`79a661a` | 步2 三道闸门套件（17 例）+ E501 修正 |
| `9c0c8c9` | 步3 注入用例集 15 条 + 参数化测试 |
| `b2371ea` | 步4 日志脱敏 sanitize 层 |
| `3168f5d` | 步5 MCP workorder（高风险 diff 人审通过） |
| `7528ad3` | 步6 MCP family-notify + notify_family 注册（schema 人审 + diff 人审通过） |
| `1f685b2`+`ae63365` | 步7 通知实时化（diff 人审通过）+ toast 截图 |
| `4fb5375` | 步8 工具行覆盖 100% + 幻觉 eval 补批 |
| （本次） | 步9 SECURITY.md + 截图留档 + import 重排修正 + 验收报告 + tag |

## 三、D1–D9 决策落实与不做清单核查

- D1/D2/D3（MCP 两 server）/D5/D6/D7/D8/D9 全部落地；**D4 通知 WS** 落地且收 Day8 遗留①；
- 不做清单零越界：无真微信/真 HIS（mock JSONL in gitignored data/）、无新迁移（head=0018 不变）、无 Redis、无前端新样式（notify-ws.js 复用既有玻璃令牌）、elder 端未接入 notify WS；
- 安全禁区零触碰：无 push、未连生产库、声纹/音频未落盘、模型权重未动。

## 四、基线偏差记录（开工前经用户确认）

1. alembic head 实际 0018（主文档写 0017，Day8 健康批次已提交 0018_sleep_details）——零迁移约束不变；
2. 测试基线实测 603（主文档 586=Day8 报告口径）——验收第 12 项按「603 既有例零回归」执行；
3. 375px 截图以 CSS zoom 模拟（浏览器 window.resizeTo 受安全策略限制）。

## 五、遗留（移交 Day10）

1. **声纹真人复调**（原 Day10 项，双区间阈值全合成口径已就绪）；
2. Docker Compose 化 / Langfuse 观测 / CI 门禁（gate.py 挂 CI hook 的 TODO 注释已在位）；
3. 幻觉 agent 层行为的在线（真 LLM）验证——当前为纪律静态断言 + 离线确定性断言双保险；
4. mock 数据目录（data/mock_his、data/mock_wechat）生产化时切换真实厂商 API 后删除。

## 六、文档回填

- [SECURITY.md](../SECURITY.md)：新建（六节骨架全节）；
- [POC_验证计划.md](POC_验证计划.md)：POC-06 真机口径「部分通过」→「通过」（步 1 演练收口）；
- [Day9_真机重启演练.md](Day9_真机重启演练.md)：演练全记录；
- eval：scene20.json 29→33 场景（注释口径同步）、gate.py 新增 hallucination_block_rate=1.0 阻塞硬线、v1_metrics.json/v1_baseline.md 重算；
- ADR-027（MCP 多工具）：主文档 §1.3 D1/D2 已载明实现口径，SECURITY.md §6.3 审计表为落地证据（TDD 安全节同步由 SECURITY.md 承担）。
