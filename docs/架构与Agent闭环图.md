# 架构与 Agent 闭环图

> 标注说明：【Day1 实装】= 本阶段已交付；【Day2 实装】= Day2 已交付（账号授权、健康档案、四页前端、三主题）；【Day3 实装】= Day3 已交付（WebRTC/FunASR/EdgeTTS/CAMPPlus 声纹全链路）；【Day4 实装】= Day4 已交付（VRM 3D 形象、LLM 流式对话、会话持久化）；【Day5 实装】= Day5 已交付（viseme 时间轴口型、熔断三档、延迟观测）；【后续】= 后续阶段能力，暂不实现。

```mermaid
flowchart TB
    subgraph CLIENT["客户端层"]
        PAD["长者平板<br/>VRM 3D 形象 + viseme 时间轴口型【Day4 实装 VRM / Day5 实装 viseme】"]
        NURSE["护工看板 / 医生视图 / 家属 H5<br/>静态页 + 健康档案【Day2 实装】<br/>紧急工单卡片+弹窗提醒（护工/医生弹窗、家属只读）【Day6+ 批5/6 实装】"]
        ADMIN["运营后台<br/>七标签页含健康授权【Day2 实装】"]
    end

    subgraph VOICE["语音链路【Day3 实装】"]
        RTC["WebRTC + aiortc 实时音视频【Day3 实装】"]
        WSCH["WebSocket /voice/ws 事件通道<br/>auth/verification/transcript/action/viseme/hint/tts/error【Day3 实装】"]
        ASR["普通话流式 ASR（FunASR）【Day3 实装】"]
        DIALECT["可选方言语义规范化【后续】"]
        TTS["普通话 TTS（EdgeTTS）【Day3 实装】"]
    end

    subgraph API["应用层"]
        APP["FastAPI create_app<br/>/health + 静态托管【Day1 实装】"]
        AUTH["四级身份 + Runtime Context<br/>人 JWT / 设备 Token / 楼栋-长者授权【Day2 实装】"]
        SAFE["安全判断与敏感拦截<br/>（一票否决）【部分实装：医疗话题硬拒答 Day4；System One 一票否决仍后续】<br/>敏感词表六类一票否决 + 创建 HITL 工单【Day6 实装】"]
        ROUTE["三级路由级联【后续】【Day6 实装：敏感词表→规则层→System One→主控】"]
        S1["System One 快决策<br/>Chinese-Jev/Laya【后续】<br/>laya 0.4.30 白名单（POC-03 方案 A）【Day6 实装】"]
        AGENT["受约束 CompanionAgent<br/>ReAct / Tool 预算【后续】<br/>ReAct + ToolCallGuard 接线【Day6 实装】"]
    end

    subgraph EXEC["执行层"]
        RULE["规则层 / 确定性服务【后续】<br/>10 条表驱动规则（固定指令直达）【Day6 实装】"]
        TOOLS["Tool 权限 + MCP 客户端<br/>天气 / HIS mock / 微信 mock【后续】<br/>天气 MCP（stdio + open-meteo）双路径汇合【Day6 实装】"]
        MEDWF["LangGraph 用药子图<br/>Checkpoint / 重试 / HITL【后续】"]
        HEALTHWF["LangGraph 健康子图<br/>绿/黄/红分级 + HITL【后续】"]
        HEALThp["健康档案分级服务<br/>basic/confidential + ACL + 审计<br/>AI 解读（频控/出域白名单）【Day2 实装】"]
        CARE["家属关怀与礼包服务<br/>累计签到 / 幂等发奖 / 线下核销【后续】"]
        SCHED["APScheduler 定时外呼/采集【后续】"]
    end

    subgraph DATA["数据层"]
        PG[("PostgreSQL + pgvector<br/>25 张表（+Day6 conversations/decision_logs、Day6+批6 workorder_reads）【实装】<br/>22 张表：Day1 11 表 + Day2 9 表<br/>+ Day4 conversation_messages + Day5 latency_metrics<br/>账号/授权/健康档案五表【实装】")]
        REDIS[("Redis<br/>【后续】（无高频临时状态、限流计数、缓存等，短期会话暂时由 PostgreSQL 承担，ADR-021）")]
        OBS["Logs / Metrics / Traces / Langfuse<br/>trace_id 串联【Day1 实装：JSON 日志 + trace_id】"]
    end

    HITL["人工确认队列 HITL【后续】<br/>敏感工单队列（admin/nurse/doctor 流转·按账号已读·解决方案；家属只读）【Day6 实装 / Day6+ 批2-6 扩展】"]

    PAD --> RTC --> APP
    PAD --> ASR --> DIALECT --> SAFE
    RTC --> TTS --> PAD
    PAD --> WSCH --> APP
    NURSE --> APP
    ADMIN --> APP

    APP --> AUTH --> SAFE --> ROUTE
    ROUTE -->|固定指令| RULE
    ROUTE --> S1
    S1 -->|高置信单步| RULE
    S1 -->|低置信/复杂| AGENT
    AGENT --> TOOLS
    AGENT --> MEDWF
    AGENT --> HEALTHWF
    NURSE --> HEALThp
    ADMIN --> HEALThp
    HEALThp --> PG
    SAFE -->|敏感意图| HITL
    SAFE -->|敏感命中一票否决·创建工单| PG
    HITL --> PG
    MEDWF --> HITL
    HEALTHWF --> HITL
    RULE --> PG
    TOOLS --> PG
    CARE --> PG
    SCHED --> MEDWF
    RTC --> REDIS

    APP --> OBS
    MEDWF --> OBS
    HEALThp --> OBS
    CARE --> OBS
    TOOLS --> OBS
```

## 闭环说明

1. **请求进入**【Day1 实装基础链路】：Uvicorn → `create_app()` → TraceIdMiddleware（合法 `X-Trace-ID` 透传/非法重生成，contextvars 绑定，响应头回传）→ 异常边界 → API 路由 → StaticFiles 根路径托管前端。Day1 仅 `/health`；Day2 起承载全部业务 API。
2. **身份与安全**【Day2 实装账号授权部分】：人 JWT / 设备 Token 双凭证体系（迁移 0003/0004）、护工-楼栋 / 医生-长者 / 家属-长者授权关联、范围外 404 隐藏存在性；四级身份级联（设备绑定→房间候选→声纹→主动确认/护工授权）中的声纹与主动确认仍为【后续】。
3. **健康档案闭环**【Day2 实装】：范围判定 → health_grants ACL → basic/confidential 分级过滤三层访问控制；写操作留审计流水；护工待录入/预警、医生全档编辑+摘要+导出、家属查看+已阅+AI 解读（仅 basic 级脱敏出域、每小时 1 次频控、失败不留档，ADR-017）。
4. **三级路由**【后续】：规则层 → System One（结构化快决策）→ 唯一受约束主控 Agent；简单请求在规则层/System One 闭环。
5. **执行**【后续】：确定性服务 / MCP Tool / LangGraph 子图（用药、健康分级工作流）；高危副作用必须 HITL 确认后执行。
6. **数据**【实装 20 表】：PostgreSQL + pgvector 承载业务数据（Day1 11 表 + Day2 账号 2 表、授权关联 3 表、健康档案 5 表）、长期记忆、声纹与 Checkpoint；Redis 承载短期状态【后续】。
7. **观测闭环**【Day1 实装：结构化 JSON 日志 + trace_id】：每个请求完成日志含 trace_id、方法、路径、状态码、耗时；健康档案写操作另留 `health_audit_logs` 审计流水；Metrics/Traces/Langfuse 为后续扩展。

## Day1 实装清单

- FastAPI 应用工厂 + `/health`（不访问数据库）
- Pydantic Settings 统一配置（后续字段 Optional）
- 结构化 JSON 请求日志 + trace_id 中间件
- PostgreSQL 异步引擎/会话入口 + SQLAlchemy 2.x 11 表 ORM
- Alembic 两版初始迁移（0001 核心四表 / 0002 领域七表）

## Day2 实装清单

- 迁移 0003（accounts、device_tokens）/ 0004（护工-楼栋、医生-长者、家属-长者授权关联）/ 0005（健康档案五表）
- 人 JWT 登录、设备注册/凭证吊销、`/auth/me`、基于授权关联的角色范围判定（范围外 404）
- 健康档案服务：三层 ACL 读写、审计流水、医生摘要+家属已阅、打印版导出、护工待录入清单与预警视图、管理员授权矩阵与健康审计
- 健康 AI 解读：OpenAI 兼容适配器（环境变量注入）、basic 级出域白名单、频控与 503/429/502 语义、`/config/flags` 公开开关
- 前端四页改造（nurse/doctor/family/admin）+ 三主题系统（陶瓷白默认/暖陶雅集/夜空黑）+ 登录首页与调试页
- 测试：pytest 全量通过（含 POC-05 攻击用例与健康档案 ACL 用例）

## Day2 之前明确不实装（后续能力）

WebRTC、ASR/TTS、方言规范化、声纹推理、System One、主控 Agent、
MCP 工具、LangGraph 业务图、APScheduler 外呼、家属签到与礼包、Docker 全量部署。

## Day3 实装清单（2026-11-05）

> Day3 将上节「后续能力」中的 WebRTC、ASR/TTS、声纹推理三项提前落地（POC-01/02 实测通过后进入正式模块）；方言规范化、System One、主控 Agent 等仍为【后续】。

- POC 先行：POC-01 WebRTC 本地回声实测通过（`scripts/poc01_webrtc_echo.py`；跨机器待教师 TURN 凭据）、POC-02 FunASR 流式 ASR + Edge-TTS 实测通过（说完→最终转写 242ms，TTS 首包基线 1331ms）。
- `compose.funasr.yaml`：FunASR 实时识别容器（host 10096→容器 10095，模型卷持久化），停容器即触发语音降级通道。
- `app/integrations/` 三 Provider（ADR-018）：`funasr_ws.py`（WSS 2pass 流式，offline 结果即终句，10s 结果死线）、`edge_tts.py`（Edge-TTS + PyAV 解码 16k，免 ffmpeg）、`sherpa_voiceprint.py`（CAMPPlus 192 维 + TEN-VAD，存储无关）。
- `app/services/voice.py`：VoiceSession 声纹门控状态机（feed_pcm → 服务端 VAD → 语音段 → 声纹验证/转写），阈值 0.55，300s 临时验证 Token 仅内存（`voice_token_store` 模块单例），模板回复 + TTS 播报，`embedding_reloader` 会话内快照现场重查。
- `app/realtime/session.py`：aiortc RTCSessionManager（按 device_id 键控、重复 offer 关旧建新、推送循环单帧异常自愈）。
- `app/api/routes/voice.py`：`WS /voice/ws`（首条消息鉴权：设备 Token / 管理员 JWT 互斥）、`POST /voice/enroll/device`、`POST /voice/enroll/admin`（双人间管理员通道，ADR-005 落地）、`GET /voice/voiceprints`；晚间追加生物识别演示接口 `GET /voice/biometric/rooms`、`POST /voice/biometric/login`（见下）。
- 前端：`voice.html` 独立适老语音测试台（WebRTC + WS 双通道、声纹录入 AudioWorklet 5s 窗、TTS 播放期静音麦克半双工防回声）；`elder.html` 增语音入口；`index.html` 登录页视频背景（无缝循环视频 + 海报兜底 + 播放拦截/减弱动效/待机暂停四重降级）与「老人生物识别进入」（房间号直选 + 指纹演示点击即进 / 声纹真比对 Day3 声纹库，经 `BIOMETRIC_DEMO_ENABLED` 开关控制，默认开）。
- 静态资产入库：端侧 VAD WASM（12.5MB）、登录页视频与海报（`app/static/media/`）、指纹图标 PNG（用户指定样式原图转透明底）。
- 测试：语音四件套 28 例 + 生物识别 9 例，全量 **136 passed** 零回归；`AGENTS.md` 追加实时/语音模块 6 条规则，realtime 列入高风险审查清单。
- 实测事故沉淀 9 起并全部修复（FunASR offline 不带 is_final、TTS onended 不可靠致麦克永久静音、零音频 finish 致会话永久卡死→10s 结果死线 + 静音缓冲禁入 ASR 等，详见 Git 提交 0b25e2b…289ba42）。

## Day4 实装清单（2026-11-06）

> 上图 PAD 节点「VRM 3D 形象 + viseme 口型【后续】」中的 **VRM 3D 形象部分**于本日提前落地（viseme 口型仍为能量近似，Day5 升级）；回复生成进入真实 LLM（System Two），三级路由/System One/主控 Agent/Tool 仍为【后续】。

- 前端：three-vrm 本地化渲染（零外部 CDN 依赖），`app/static/js/avatar.js` 引擎封装 + 7 个 VRM 形象注册表驱动切换，加载失败自动回落且不影响语音链路；口型同步以音频能量近似驱动（TTS → MediaStreamDestination → 引擎 analyser）。
- 回复链升级：LLM 流式分句（`app/integrations/llm.py`，Qwen 兼容 API + 人设 few-shot prompt + `_SentenceAssembler` 凑句即产出），`[act:*]` 动作标记剥除后经 WS `action` 事件单独下发（不朗读、不进字幕），`app/services/actions.py` 动作表驱动 3D 动作；医疗话题硬拒答（few-shot 约束 + 真实链路实测验证）。
- 会话持久化（ADR-021）：迁移 0006 `conversation_messages` 成对落库（user/assistant），LLM 上下文注入最近 5 轮，替代 Redis 短期会话（上图 REDIS 节点仍为【后续】）。
- 语音链路加固：TTS 回声防护窗（播放期静音麦克 + 服务端超时兜底双防线）、VAD 连续帧起判（SPEECH_ONSET_MS）与最小语音时长过滤（MIN_SPEECH_MS）、FunASR 零结果自适应死线（消除噪声伪终句阻塞）。
- 测试基线 192 例全绿（+47），覆盖率 61%。

## Day5 实装清单（2026-11-07）

> 上图三处口径随本日更新：① PAD「viseme 口型」由能量近似升级为音素级时间轴（该【后续】标注项已实装）；② 数据层实装表数 20→22（Day4 加 conversation_messages、Day5 加 latency_metrics）；③ SAFE「安全判断」中的医疗话题硬拒答已在 LLM 链路生效（System One 一票否决仍为【后续】）。三级路由、System One、主控 Agent、Tool/MCP 仍为【后续】，其中 Tool 防护框架已预埋（见下）。

- 口型同步升级（POC-02 复测通过）：`edge_tts.py` 利用 WordBoundary 事件换算「词→口型→时刻」viseme 时间轴，WS `viseme` 事件**先于同句音频**下发；前端 `playViseme` 以播放起点为 t0 调度五种近似口型，引擎不支持时零改动回落能量口型。
- 延迟观测：迁移 0007 `latency_metrics`（asr_wait_ms / llm_first_ms / llm_total_ms / tts_first_ms / e2e_ms 分列 + placeholder_used / success 标记），每轮回复落 1 行，POC-08 用 SQL percentile_cont 复算 P50/P95。
- 熔断三档（大纲 5.2）：ASR 终句超 2s → 朗读"没听清，请再说一遍"（asr_prompt_seconds）；LLM 首 token 超 3s → 播拟真占位语"嗯~我想想哈。"（llm_placeholder_seconds，每轮至多 1 次）；TTS 首包超 1s → 字幕先行（tts_slow_seconds）。均经 WS `hint`/`tts` 事件可见降级，不中断通话。
- 延迟根因修复：POC-08 固定 20 语句 ×20 轮跑批定位 qwen3 思考模式为首 token 瓶颈，`stream_chat` 显式下发 `enable_thinking=false`（服务端不识别时 400 自动去字段重试）——首 token 2980ms→483ms（-84%），e2e P50 4480ms→2561ms、P95 10600ms→4560ms（-57%）；剩余大头为 EdgeTTS 首包 ~1.3s（免费非流式），教学口径暂不更换，EM-01（≤1.5s）未达标如实记录于 POC-08，留待流式 TTS 选型（候选：DashScope CosyVoice / 火山豆包 / Azure Speech / 开源自部署）。
- Tool 防护预埋：`app/services/guard.py` ToolCallGuard 调用预算 + 熔断窗口双闸（假时钟 7 例单测），Day6 接 MCP Tool 调用点零改动生效；文字输入通道接入同一条 LLM 回复管线（空文本拒绝零副作用、500 字截断成轮）。
- 信令专项（POC-07 定案）：ICE answer 生成实测 P50=7.5ms / P95=15.9ms，Day3 记录的 5 秒为环境瞬态，远优于 5 秒目标。
- 演示前体验优化：`voice.html` 新增老人生物识别免 Token 进入（房间选择 → 指纹演示直进 / 声纹真比对 Day3 声纹库，后端签发短效设备 JWT 自动连接，显式 Token 输入模块保留）；数字人四态状态徽章（待机灰 / 聆听蓝 / 思考橙黄律动 / 回复绿律动，单一状态机 WS 事件驱动防卡状态）；生物识别房间弹窗流转修复。
- 测试基线 252 例全绿（+3），覆盖率 67%→70%（达标线 ≥70%）；迭代标签 `iter2-demo`（本地，未 push）。

## 当前已完成 vs 后续能力（截至 Day5，2026-11-07）

| 能力 | 状态 |
| --- | --- |
| FastAPI 地基 / 身份权限 / 健康档案 / 四页前端 | ✅ Day1-2 |
| WebRTC + FunASR + EdgeTTS + CAMPPlus 声纹（生物识别进入） | ✅ Day3 |
| VRM 3D 形象 + LLM 流式对话 + 动作标记 + 会话持久化 | ✅ Day4 |
| viseme 时间轴口型 / 熔断三档 / 延迟观测与基线 / ToolCallGuard 框架 / 状态徽章 | ✅ Day5 |
| 四级级联路由（敏感词表→规则层→System One→主控 Agent）/ MCP 天气工具 / 敏感词表一票否决 + HITL 敏感工单队列 | ✅ Day6 |
| 对话块分层结构 / 北京时间统一 / 老人端对话记录视图 / 工单四端卡片+弹窗 / 按账号独立已读 / 解决方案列 | ✅ Day6+ 批1-6 |
| 方言语义规范化、System One、三级路由、主控 Agent、Tool/MCP 实调、LangGraph 子图、APScheduler 外呼、家属签到礼包、流式 TTS 选型、Docker 全量部署 | 【后续】Day6 起 |

## Day6 实装清单（2026-11-09）

> 上图 ROUTE / S1 / AGENT / RULE / TOOLS / HITL / SAFE 等节点于本日由【后续】追加【Day6 实装】标注（节点仅加注、结构未变）。四级级联路由口径：**敏感词表 → 规则层 → System One（laya）→ 主控 Agent**，敏感命中一票否决，全程零自由文本生成。

- 四级级联路由：`app/router/` cascade.py 接入 voice.py —— safety.py 敏感词表（六类，隔字正则抗干扰）→ 规则层 10 条表驱动（固定指令直达、零模型）→ System One 契约（9 字段）+ laya 适配器（懒加载 / 200ms 超时降级）→ 主控 CompanionAgent（ReAct + ToolCallGuard 接线 + 决策降级直答）。
- 敏感一票否决 + HITL 工单：命中只播安抚话术、不调用大模型；工单落 `workorders(type=sensitive_alert)`，`decision_logs` 全程留痕（迁移 0008）；admin 列表/流转两端点 + 敏感事件 Tab。
- MCP 天气 Tool：stdio + open-meteo + 重试降级（mock 可见话术），规则/S1 双路径汇合同一回复管线；`report_emergency` 第四道防线 Tool（批2）。
- 权重白名单：laya 0.4.30 + pinned revision（AGENTS.md 豁免注记，POC-03 方案 A 裁决）；评测基线 50 条（意图准确率 96.4% / 敏感召回 100%，`eval/reports/day6_router_baseline.md`）。
- 测试基线 252→361 全绿，覆盖率 70%→73%。详见 [Day6_验收报告.md](../Day_plan/Day6_验收报告.md)。

## Day6+ 追加批次清单（批1-6，2026-11-09）

| 批次 | 提交 | 内容 |
| --- | --- | --- |
| 前置 | `550b444` | 天气精简答复流式化（句末即逐句 TTS）：e2e P50 -24% / P95 -33%，P95 反超 Day5 基线 |
| 批1 | `9647f13` | 敏感词表隔字正则（「胸口有点疼」类拆字绕过）；明天/后天天气规则内收口 date 参数；voice.html 天气数据卡 |
| 批2 | `99eabe2` | 敏感工单分级可见性矩阵（abuse 仅 admin+family，护工/医生不可见；迁移 0009 `workorders.category`）；护工/医生弹窗提醒；family 只读关联长者；`report_emergency` Tool |
| 批3 | `d8c3efd` | 对话记录双端可见：老人端声纹验证后最近 20 轮（服务端 Runtime Context 判定 elder，未验证 403）；管理端按长者全历史 + decision_logs 路由叠加 |
| 批4 | `a617af7` `53389a9` | 对话记录重构：左侧固定面板 + 分层对话块（每次「开始对话」新开块、块ID+子对话ID 两级、北京时间统一、倒序、工单标记）；修复 `KB.session` 笔误致管理端加载器 IIFE 崩溃 |
| 批5 | `296b41b` | 紧急工单卡片内嵌护工/医生/家属工作台（护工/医生 10s 轮询弹窗、家属只读）；老人端对话块点击展开/收起 |
| 批6 | `559d69f` | 工单按账号独立已读（`workorder_reads` 表，迁移 0010）+ 行点击详情弹窗（打开即标已读）；resolve 开放护工/医生（abuse 仍仅 admin），原因说明+解决方案必填（`workorders.solution` 独立列）；家属端可见处理状态与解决方案 |
