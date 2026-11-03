# 康伴 3D 数字人康养陪伴系统

部署在 CCRC 康养社区长者卧室平板上的 3D 数字人陪伴系统：通过 WebRTC 全双工语音与长者交流，
提供情感陪伴、用药提醒、健康采集、戏曲/影视娱乐和信息搜集服务。机构是主要付费方，长者是主要使用者。

- PRD（需求基线）：`docs/PRD_康伴3D数字人.md`
- ADR（架构决策）：`docs/ADR_康伴3D数字人.md`
- TDD（技术设计）：`docs/TDD_康伴3D数字人.md`
- 数据模型：`docs/ER_数据模型设计.md`
- 工作流选型：`docs/工作流选型说明.md`
- 架构与 Agent 闭环图：`docs/架构与Agent闭环图.md`

## Day1 架构摘要

Day1 交付可运行地基：FastAPI 应用工厂 + `/health` 存活检查、Pydantic Settings 统一配置、
结构化 JSON 请求日志（trace_id 贯穿）、PostgreSQL 异步引擎/会话入口、SQLAlchemy 2.x 十一张基础表 ORM、
Alembic 两版初始迁移、pytest 骨架测试。

技术栈：Python 3.12+ / FastAPI / Uvicorn / SQLAlchemy 2.x + asyncpg / PostgreSQL + pgvector / Alembic / pytest。

## 环境要求

- Python >= 3.12
- PostgreSQL 15+ 且已安装 pgvector 扩展（本地开发推荐直接使用 `pgvector/pgvector:pg17` Docker 镜像）
- 本地开发库：`kangban_dev`（不连接任何生产数据库）

## 目录说明

```
app/
├── main.py          # create_app 应用工厂与 Uvicorn 入口
├── api/             # HTTP 路由聚合（Day1 仅 /health）
├── core/            # 配置、日志、异常边界、生命周期
├── middleware/      # trace_id 中间件
├── db/              # 异步引擎与会话入口
├── models/          # SQLAlchemy 2.x ORM（11 张基础表）
├── schemas/         # Pydantic 请求/响应结构
└── services/        # 领域服务（Day1 仅 health）
alembic/             # 迁移环境与版本（0001 设施核心四表、0002 领域七表）
tests/               # 骨架测试（不要求连接数据库）
docs/                # ER、工作流选型、架构闭环图及 PRD/TDD/ADR
```

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖（含 dev 与 infra 扩展组）
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,infra]"

# 2. 准备本地配置：复制 .env.example 为 .env，填写本地 PostgreSQL 连接信息
Copy-Item .env.example .env

# 3. 本地 PostgreSQL（Docker 方式，仅本地开发）
docker run -d --name kangban-pg -p 5432:5432 `
  -e POSTGRES_USER=kangban_dev -e POSTGRES_PASSWORD=<本地开发密码> -e POSTGRES_DB=kangban_dev `
  pgvector/pgvector:pg17

# 4. 执行迁移（执行前必须人工阅读 alembic/versions/ 下迁移文件）
alembic upgrade head

# 5. 启动服务
python -m uvicorn app.main:app --reload

# 6. 验活
curl.exe -i http://127.0.0.1:8000/health

# 7. 测试（不要求连接数据库）
python -m pytest
```

## 配置说明

配置统一由 `app/core/config.py` 的 Pydantic Settings 加载（`.env` + 进程环境变量），
完整变量清单见 `.env.example`。

### Day1 必填配置（启动必需）

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | 本地 PostgreSQL 异步连接串（kangban_dev），迁移共用 |
| `APP_NAME` / `APP_ENV` / `APP_HOST` / `APP_PORT` / `APP_TIMEZONE` / `LOG_LEVEL` | 应用与日志基础信息 |
| `REDIS_URL` / `SQL_ECHO` | 声明式预留 / SQL 日志开关 |

### 后续预留配置（Optional，空值不影响 Day1 启动）

`LLM_*`、`EMBEDDING_*`、`SYSTEM_ONE_*`、`ASR_*`、`TTS_*`、`DIALECT_*`、`VOICEPRINT_*`、
`WEBRTC_*`、`JWT_*`、`AUDIO_RETENTION_HOURS`、`LANGFUSE_*`。
这些变量在对应功能阶段（Day2 起）才做必填校验；当前全部为 `CHANGE_ME` 或空占位，不得填入真实密钥。

## Day1 验收命令

| 验收项 | 命令 | 预期 |
| --- | --- | --- |
| 目录与关键文件 | `Test-Path app/main.py` 等 | 均为 True |
| 启动 | `python -m uvicorn app.main:app --reload` | 正常启动 |
| 健康检查 | `curl.exe -i http://127.0.0.1:8000/health` | HTTP 200，含 `status: ok` |
| trace_id 透传 | `curl.exe -i -H "X-Trace-ID: <合法UUID>" http://127.0.0.1:8000/health` | 响应头回传同一 trace_id |
| Settings 加载 | `python -c "from app.core.config import get_settings; print(get_settings().app_env)"` | 输出 local |
| pytest | `python -m pytest` | 全部通过，不连数据库 |
| 迁移 | `alembic upgrade head` | 两版迁移成功 |
| 表结构 | psql `\dt` / `\d elders` | 恰好 11 张表；elders 结构完整 |

## MVP 禁区（务必遵守）

1. 不做医疗诊断，不替代 SOS；高危副作用必须人工确认（HITL）。
2. 不连接生产数据库；只使用本地开发库 `kangban_dev`。
3. 不提交任何真实 API Key、数据库密码、长者数据、真实 HIS 地址或未授权媒体地址。
4. `.env` 永不入库；示例配置只允许安全占位值（`CHANGE_ME` 或空值）。
5. 不导入、不链接未授权媒体；媒体仅使用授权、教学或公有领域资源。
6. 教学阶段不接真实 HIS/微信，全部使用 mock。
7. 不下载、不加载模型权重；不调用外部模型服务（后续阶段按 ADR-014 经 POC 确定后接入）。
8. Alembic 迁移执行前必须人工审查；后续业务表只能通过新迁移演进，不回改已执行迁移。
9. `elder_id` 只能来自服务端可信运行时上下文，不信任客户端或模型传参。
10. 原始音频仅临时保存，最长 72 小时删除；日志脱敏。
