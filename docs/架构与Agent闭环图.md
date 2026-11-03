# 架构与 Agent 闭环图

> 标注说明：【Day1 实装】= 本阶段已交付；【后续】= 后续阶段能力，Day1 不实现。

```mermaid
flowchart TB
    subgraph CLIENT["客户端层【后续】"]
        PAD["长者平板<br/>VRM 3D 形象 + viseme 口型"]
        NURSE["护工看板 / 医生视图"]
        FAMILY["家属 H5（签到/关怀/礼包）"]
        ADMIN["运营后台"]
    end

    subgraph VOICE["语音链路【后续】"]
        RTC["WebRTC + aiortc 实时音视频"]
        ASR["普通话/方言流式 ASR"]
        DIALECT["可选方言语义规范化"]
        TTS["普通话/方言流式 TTS"]
    end

    subgraph API["应用层【Day1 实装：骨架】"]
        APP["FastAPI create_app<br/>/health 存活检查【Day1 实装】"]
        AUTH["四级身份 + Runtime Context<br/>人 JWT / 设备 Token【后续】"]
        SAFE["安全判断与敏感拦截<br/>（一票否决）【后续】"]
        ROUTE["三级路由级联【后续】"]
        S1["System One 快决策<br/>Chinese-Jev/Laya【后续】"]
        AGENT["受约束 CompanionAgent<br/>ReAct / Tool 预算【后续】"]
    end

    subgraph EXEC["执行层【后续】"]
        RULE["规则层 / 确定性服务"]
        TOOLS["Tool 权限 + MCP 客户端<br/>天气 / HIS mock / 微信 mock"]
        MEDWF["LangGraph 用药子图<br/>Checkpoint / 重试 / HITL"]
        HEALTHWF["LangGraph 健康子图<br/>绿/黄/红分级 + HITL"]
        CARE["家属关怀与礼包服务<br/>累计签到 / 幂等发奖 / 线下核销"]
        SCHED["APScheduler 定时外呼/采集"]
    end

    subgraph DATA["数据层"]
        PG[("PostgreSQL + pgvector<br/>11 张基础表【Day1 实装】<br/>业务/记忆/声纹/Checkpoint")]
        REDIS[("Redis<br/>短期状态/限流【后续】")]
        OBS["Logs / Metrics / Traces / Langfuse<br/>trace_id 串联【Day1 实装：JSON 日志 + trace_id】"]
    end

    HITL["人工确认队列 HITL【后续】"]

    PAD --> RTC --> APP
    PAD --> ASR --> DIALECT --> SAFE
    RTC --> TTS --> PAD
    NURSE --> APP
    FAMILY --> APP
    ADMIN --> APP

    APP --> AUTH --> SAFE --> ROUTE
    ROUTE -->|固定指令| RULE
    ROUTE --> S1
    S1 -->|高置信单步| RULE
    S1 -->|低置信/复杂| AGENT
    AGENT --> TOOLS
    AGENT --> MEDWF
    AGENT --> HEALTHWF
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
    CARE --> OBS
    TOOLS --> OBS
```

## 闭环说明

1. **请求进入**【Day1 实装基础链路】：Uvicorn → `create_app()` → TraceIdMiddleware（合法 `X-Trace-ID` 透传/非法重生成，contextvars 绑定，响应头回传）→ 异常边界 → API 路由。Day1 仅 `/health`。
2. **身份与安全**【后续】：四级身份级联（设备绑定→房间候选→声纹→主动确认/护工授权）建立 Runtime Context；敏感意图安全判断优先于普通路由。
3. **三级路由**【后续】：规则层 → System One（结构化快决策）→ 唯一受约束主控 Agent；简单请求在规则层/System One 闭环。
4. **执行**【后续】：确定性服务 / MCP Tool / LangGraph 子图（用药、健康）；高危副作用必须 HITL 确认后执行。
5. **数据**【Day1 实装 11 表】：PostgreSQL + pgvector 承载业务数据、长期记忆、声纹与 Checkpoint；Redis 承载短期状态【后续】。
6. **观测闭环**【Day1 实装：结构化 JSON 日志 + trace_id】：每个请求完成日志含 trace_id、方法、路径、状态码、耗时；Metrics/Traces/Langfuse 为后续扩展。

## Day1 实装清单

- FastAPI 应用工厂 + `/health`（不访问数据库）
- Pydantic Settings 统一配置（后续字段 Optional）
- 结构化 JSON 请求日志 + trace_id 中间件
- PostgreSQL 异步引擎/会话入口 + SQLAlchemy 2.x 11 表 ORM
- Alembic 两版初始迁移（0001 核心四表 / 0002 领域七表）

## Day1 明确不实装（后续能力）

JWT/设备 Token、长者 CRUD、WebRTC、ASR/TTS、方言规范化、声纹推理、System One、主控 Agent、
MCP 工具、LangGraph 业务图、APScheduler 外呼、家属签到与礼包、Docker 全量部署。
