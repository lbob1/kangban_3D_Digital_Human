# 架构与 Agent 闭环图

> 标注说明：【Day1 实装】= 本阶段已交付；【Day2 实装】= Day2 已交付（账号授权、健康档案、四页前端、三主题）；【后续】= 后续阶段能力，暂不实现。

```mermaid
flowchart TB
    subgraph CLIENT["客户端层"]
        PAD["长者平板<br/>VRM 3D 形象 + viseme 口型【后续】"]
        NURSE["护工看板 / 医生视图 / 家属 H5<br/>静态页 + 健康档案【Day2 实装】"]
        ADMIN["运营后台<br/>七标签页含健康授权【Day2 实装】"]
    end

    subgraph VOICE["语音链路【后续】"]
        RTC["WebRTC + aiortc 实时音视频"]
        ASR["普通话/方言流式 ASR"]
        DIALECT["可选方言语义规范化"]
        TTS["普通话/方言流式 TTS"]
    end

    subgraph API["应用层"]
        APP["FastAPI create_app<br/>/health + 静态托管【Day1 实装】"]
        AUTH["四级身份 + Runtime Context<br/>人 JWT / 设备 Token / 楼栋-长者授权【Day2 实装】"]
        SAFE["安全判断与敏感拦截<br/>（一票否决）【后续】"]
        ROUTE["三级路由级联【后续】"]
        S1["System One 快决策<br/>Chinese-Jev/Laya【后续】"]
        AGENT["受约束 CompanionAgent<br/>ReAct / Tool 预算【后续】"]
    end

    subgraph EXEC["执行层"]
        RULE["规则层 / 确定性服务【后续】"]
        TOOLS["Tool 权限 + MCP 客户端<br/>天气 / HIS mock / 微信 mock【后续】"]
        MEDWF["LangGraph 用药子图<br/>Checkpoint / 重试 / HITL【后续】"]
        HEALTHWF["LangGraph 健康子图<br/>绿/黄/红分级 + HITL【后续】"]
        HEALThp["健康档案分级服务<br/>basic/confidential + ACL + 审计<br/>AI 解读（频控/出域白名单）【Day2 实装】"]
        CARE["家属关怀与礼包服务<br/>累计签到 / 幂等发奖 / 线下核销【后续】"]
        SCHED["APScheduler 定时外呼/采集【后续】"]
    end

    subgraph DATA["数据层"]
        PG[("PostgreSQL + pgvector<br/>20 张表：Day1 11 表 + Day2 9 表<br/>账号/授权/健康档案五表【实装】")]
        REDIS[("Redis<br/>短期状态/限流【后续】")]
        OBS["Logs / Metrics / Traces / Langfuse<br/>trace_id 串联【Day1 实装：JSON 日志 + trace_id】"]
    end

    HITL["人工确认队列 HITL【后续】"]

    PAD --> RTC --> APP
    PAD --> ASR --> DIALECT --> SAFE
    RTC --> TTS --> PAD
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
