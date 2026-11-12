# 康伴 SECURITY.md（Day9 步 9，大纲 9.4 / 主文档 §4.5 骨架全节）

> 基线：tag `iter5-demo`（commit 604192e 起点至 Day9 全部提交）· alembic head `0018`（Day9 零迁移）
> 测试基线：603 既有例 + Day9 新增 60+ 例，全量全绿 · ruff 零新增告警
> 维护约定：新增防线/工具/关卡必须同步更新本文件对应小节（人读纪律）

---

## 1. 威胁模型

### 1.1 资产

| 资产 | 等级 | 载体 |
| --- | --- | --- |
| 长者身份与声纹向量 | 高 | accounts/elders/voiceprints（库内，不落盘不进日志） |
| 健康数据（血压/睡眠/情绪/主诉） | 高 | health_records / health_alerts / 工单描述 |
| 对话内容（含 ASR 原文语义） | 高 | conversations（原音频永不落盘） |
| 记忆（个人偏好/事件） | 中高 | memory_chunks（pgvector，per-elder 隔离） |
| 工单与 HITL 决策 | 中高 | workorders / decision_logs |
| 决策可解释性（trace_id 链） | 中 | 结构化日志（脱敏后） |

### 1.2 入口与信任边界

```
                    ┌────────────────── 信任边界：服务端 ──────────────────┐
  长者端(平板)      │  语音 WS(/ws/realtime)──► 声纹验证 ──► verified_elder_id │
  ──设备Token──────►│        │ ASR原文(元数据口径)                          │
  管理端(护工/医生/ │  /ws/notify ──JWT──► 范围过滤(服务端)                  │
  家属/管理员)      │  HTTP API ──JWT──► require_role + can_access_elder    │
  ──用户JWT───────►│        │                                              │
  主控 LLM(不可信) ─►│  tool_calls ──► registry(schema禁elder_id) ──► runtime_ctx│
                    │        │                                 （服务端注入） │
  MCP servers ─────►│  stdio 子进程（本机，无网络暴露）                       │
  （教学 mock）     │      data/mock_his / data/mock_wechat（gitignore）     │
                    └──────────────────────────────────────────────────────┘
```

### 1.3 不可信来源清单（一律拒绝/过滤）

1. **主控模型 tool_calls 参数**：elder_id 等身份字段一律忽略（runtime_ctx 注入，[registry.py](../app/tools/registry.py) schema 全量静态断言 `test_gate3b`）；
2. **客户端身份传参**：`elder_id` 不接受客户端来源（禁区 8）；WS 客户端消息永不参与权限判断（`test_notify_ws` 只收不发）；
3. **对话文本**：注入指令（15 条用例集）、幻觉诱导（33 场景集 hal-01..04）；
4. **MCP server 返回**：教学 mock 可信、真实厂商 API 返回按外部输入处理（入参归一 + 长度截断）。

---

## 2. OWASP LLM Top 10 逐条映射

| # | 威胁 | 项目文件（已有防线） | 测试编号 |
| --- | --- | --- | --- |
| LLM01 | 提示注入 | [safety.py](../app/router/safety.py) 词表一票否决 + [rules.py](../app/router/rules.py) 确定性层无视注入前缀 + 主控人设纪律（[elder_companion.md](../app/prompts/personas/elder_companion.md)）+ [guard.py](../app/services/guard.py) ToolCallGuard | tests/security/test_day9_injections.py（15 条参数化）· eval 33 场景 |
| LLM02 | 不安全输出处理 | [logging.py](../app/core/logging.py) sanitize（身份证/手机号/剂量 mask，message+extra+exception 统一过） | test_logging_sanitize.py（9 例 grep 断言） |
| LLM03 | 敏感信息泄露 | 记忆隔离（[memory.py](../app/services/memory.py) WHERE 强制 elder_id）+ 会话隔离（thread 服务端派生）+ 工单归户 | test_day9_gates.py gate1*/gate2* |
| LLM04 | 过度权限 | runtime_ctx 注入 + require_role + can_access_elder（护工本楼栋/医生家属有效关联）+ notify WS 服务端范围过滤 | test_day9_gates.py gate3* · test_notify_ws.py |
| LLM05 | 供应链 | MCP server 独立 pyproject 依赖隔离（仅 mcp>=1.0,<2）；本地权重白名单（AGENTS 禁区 4 豁免注记） | —（人工审查口径） |
| LLM06 | 敏感披露 | 敏感词表一票否决 + 工单 HITL + 「不透露其他长者任何信息」人设纪律 | test_day2_attacks.py · injections med-03/04 |
| LLM07 | 模型 DoS | 调用超时 3s（MCP 客户端）+ 重试 2 次指数退避 + 降级全路径 | test_mcp_workorder.py · test_mcp_family_notify.py |
| LLM08 | 智能体信任边界 | 三道隔离闸门（会话/记忆/工具授权）+ 工具 schema 静态断言 | test_day9_gates.py（17 例） |
| LLM09 | 幻觉 | 主控人设「不装懂不编造」+ 确定性层零编造（reply=None）+ 知识库召回优于生成 | eval hal-01..04（hallucination_block_rate=1.0 硬线） |
| LLM10 | 无限消耗 | tool_calls_max 场景断言 + eval 工具调用准确率 | eval 33 场景 · bench gate |

---

## 3. 降级矩阵（五依赖不可用时）

| 依赖不可用 | 行为 | 用户可见话术/表现 | 留痕 |
| --- | --- | --- | --- |
| LLM（System Two） | 规则层+S1 确定性场景照常；开放域升级失败 → 可见降级 | 「抱歉，我现在联系不上大脑，请稍后再试或找工作人员」 | 结构化日志 escalated 失败 |
| embedding（百炼） | 记忆链路整体降级：萃取跳过、召回空集（不报错不阻塞对话） | 对话无感（记忆相关话术降级为通用） | Settings Optional 口径 |
| MCP（weather/workorder/family-notify） | 客户端重试 2 次 → 降级：weather→本地 mock（degraded=true 可见）；workorder→本地 ORM 工单（契约不变）；family-notify→结构化日志 | weather 报数注明 mock；工单照常可查 | workorder_mcp_fallback / family_notify_degraded 事件 |
| WS（notify） | 双通道①结构化日志必达；在线推送失败不阻断图/端点主流程 | 管理端回轮询既有路径（workqueue） | health_notifier_fired / notify_ws_error |
| ASR/声纹（FunASR/CAM++） | 应用照常启动；语音链路走可见降级（WS error 事件 + 文字通道） | 「语音通道暂不可用，请用文字与我交流」 | realtime WS error 事件 |

---

## 4. 数据保留策略（当前=教学口径永久保留）

| 数据 | 位置 | 当前口径 | 生产建议 |
| --- | --- | --- | --- |
| conversations 块 | PostgreSQL | 永久 | TTL 180 天 + 归档冷存 |
| memory_chunks（含向量） | PostgreSQL | 永久（superseded 软删） | TTL 365 天，superseded 30 天物理清理 |
| decision_logs | PostgreSQL | 永久 | TTL 1 年（合规审计窗口） |
| workorders / health_records | PostgreSQL | 永久 | 医疗口径按法规（≥3 年） |
| 结构化日志 | stdout→文件 | 永久 | TTL 30 天，rotate |
| 原始音频/声纹文件 | 不存在 | **永不落盘**（Day3 铁律） | 维持 |
| data/mock_his、data/mock_wechat | 本地 JSONL | 永久（gitignore） | 换真厂商 API 后删除目录 |
| checkpoint（med/health sqlite） | data/*.sqlite | 永久 | TTL 30 天 |

---

## 5. HITL 关卡清单（确认前零副作用）

| # | 关卡 | 触发条件 | 放行人 | 实现 |
| --- | --- | --- | --- | --- |
| 1 | 敏感词表 | 胸痛/摔倒/救命/轻生等（[safety.py](../app/router/safety.py) 18 处正则） | 医生（urgent 工单） | cascade 一票否决 + 建单，确认前零通知 |
| 2 | System One 二层 | S1 分类 sensitive 且置信度不足词表命中时升级建议 | 医生 | laya 固定意图分类，不生成自由文本 |
| 3 | yellow interrupt | 健康图 yellow（如收缩压≥160/睡眠<5h/情绪≤2） | 护工（nurse_confirm） | LangGraph interrupt + workqueue |
| 4 | red interrupt | 健康图 red（收缩压≥180/急症词/跌倒） | 医生（doctor_confirm） | Command(resume)；副作用仅在图内放行 |
| 5 | 0015 审核链 | 用药计划类高危变更（改剂量/停药） | 医生 + 双记录 | medication audit 链 |
| 6 | 工具授权闸门 | 任何 tool 调用 | 服务端 runtime_ctx | schema 禁 elder_id；缺 ctx 拒绝（elder_not_verified） |

---

## 6. 附录

### 6.1 规则文件后门自查记录（2026-11-12）

| 检查项 | 结果 |
| --- | --- |
| safety.py 词表无条件放行（「忽略/always/白名单跳过」类逻辑） | 无（唯一命中为 is_sensitive 不信任 laya 的设计说明 docstring） |
| rules.py 无条件/跳过分支 | 无（命中为「执行类规则永远不并列」语义说明） |
| registry 工具 schema elder_id 注入后门 | 无（test_gate3b 全量静态断言 7 工具） |
| decision_logs/workqueue 旁路写路径（绕过图的副作用） | 无（副作用只在 health/medication 图内放行） |
| .env 密钥入库 | 无（.env 在 .gitignore；.env.example 仅占位） |

### 6.2 三道闸门测试矩阵摘要（tests/security/test_day9_gates.py，17 例全拦截）

| 闸门 | 用例 | 攻击意图 → 拦截方式 |
| --- | --- | --- |
| ① 会话隔离 | gate1a–1e（8 例含参数化） | 伪造 thread_id→OpenAPI 无入口；人 JWT 打设备端点→403；未声纹→403；已验证只见本人块；B 凭证查 A→空集 |
| ② 记忆隔离 | gate2a–2d（4 例） | 匿名召回→短路空集；跨 elder→WHERE 过滤零泄漏；混库 top-k→100% 归注入主体；源码静态断言（回归防线） |
| ③ 工具授权 | gate3a×2/3b/3c/3d（5 例） | 模型传受害者 elder_id→忽略；全 schema 无 elder_id；缺 ctx→拒绝；正常路径归户正确 |

### 6.3 MCP 工具审计表（三 server 统一口径）

| 维度 | weather（Day6） | workorder（Day9 步5） | family-notify（Day9 步6） |
| --- | --- | --- | --- |
| 传输/暴露面 | stdio 子进程，本机无网络端口 | 同左 | 同左 |
| 鉴权 | 无对外暴露（客户端为 app 可信代码） | 同左 | 同左 |
| 业务 schema elder_id | 无 | **无**（meta 外层附加，server 仅作归属记录不信任） | server 侧显式收 elder_id（客户端从 runtime_ctx 注入；模型经 registry schema 传不到） |
| 超时/重试 | 3s / 2 次退避 | 3s / 2 次退避 | 3s / 2 次退避 |
| 幂等 | 只读 | idempotency_key 确定性派生（主体+类目+摘要），跨重启幂等 | 只写（重复消息即重复投递，mock 口径） |
| 上下文边界 | 无状态、不注入对话历史、不返回用户资料 | 无状态；留痕 JSONL 不含对话内容 | 无状态；不返回家属资料；无绑定 skip |
| 降级 | 本地 mock（degraded=true） | 本地 ORM 工单（契约不变） | 结构化日志（不静默） |
| 测试 | test_tools_weather.py | test_mcp_workorder.py（8 例） | test_mcp_family_notify.py（12 例） |

### 6.4 通知实时化安全口径（Day9 步 7）

- `/ws/notify` 仅用户 JWT（设备 Token 4401 拒绝——人/设备互斥）；token 经查询参数（浏览器 WS 无法自定义 Header，已注记）；
- **范围过滤只在服务端**：逐连接复用 can_access_elder（护工=本楼栋、医生/家属=有效关联、admin=全量）；范围外不推送（不泄露存在性）；
- 事件字段白名单（对话原文/病史明细不外发，`test_notify_event_payload_whitelist` 断言）；
- 双通道：结构化日志必达（离线兜底）+ WS 尽力；WS 失败不阻断图/端点主流程。
