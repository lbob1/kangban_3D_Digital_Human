# AGENTS.md — AI 协作约定

## 一句话定位

康伴 3D 数字人康养陪伴系统：CCRC 康养社区长者平板上的 3D 数字人，承担情感陪伴、用药提醒、健康采集、戏曲/影视娱乐和信息搜集；技术栈 Python / FastAPI / PostgreSQL + pgvector / SQLAlchemy 2.x / Alembic。

## 目录约定

- `app/api/` HTTP 路由层：参数校验、统一错误，不写复杂业务编排。
- `app/core/` 配置、结构化日志、异常边界、生命周期。
- `app/middleware/` 请求级中间件（trace_id）。
- `app/db/` 异步引擎与会话入口；业务代码不自行创建引擎。
- `app/models/` SQLAlchemy 2.x ORM；`app/schemas/` Pydantic 契约。
- `app/services/` 领域服务；不直接处理 HTTP 或连接细节。
- `alembic/versions/` 数据库迁移；与 ORM 保持一致。
- `tests/` pytest；`docs/` 设计文档。

## 常用命令

```powershell
python -m uvicorn app.main:app --reload   # 启动
python -m pytest                          # 测试（不连数据库）
alembic upgrade head                      # 迁移（先人工审查）
ruff check .                              # 静态检查
```

## 安全禁区（任何情况下不得违反）

1. 不连接生产数据库，只使用本地开发库 `kangban_dev`。
2. 不提交密钥：`.env` 永不入库；`.env.example` 只允许 `CHANGE_ME` 或空占位。
3. 不使用真实长者数据；不虚构任何长者信息。
4. 不调用未授权媒体资源或外部模型服务；不下载、不加载模型权重。
5. 不做医疗诊断、不替代 SOS；高危副作用必须人工确认（HITL）。
6. 不执行 `git push`；远程仓库操作必须获得单独授权。
7. 不回改已执行的 Alembic 迁移；后续业务表通过新迁移加入。
8. `elder_id` 只能来自服务端 Runtime Context，不信任客户端或模型传参。

## 高风险文件清单（AI 生成后必须人工审查 diff）

- `app/auth/**` — 身份与鉴权边界
- `alembic/versions/**` — 数据库结构变更
- `app/tools/**` — Tool 权限与执行上下文
- `mcp-servers/**` — 外部工具协议与鉴权
- `Dockerfile` — 部署与运行环境

## 模型路由表

| 层级 | 模型/服务 | 用途 | 约束 |
| --- | --- | --- | --- |
| System One | Chinese-Jev / Laya（本地权重，选型待 POC） | 固定意图分类、敏感拦截、置信度升级建议 | 不生成自由文本，不执行高危副作用 |
| System Two | DeepSeek / Qwen 兼容 API（待 POC） | 复杂开放域对话兜底、结构化输出 | 不接管业务回答与工具调用安全边界 |
| ASR/TTS | 后续选定的流式服务（待 POC） | 普通话/方言语音转写与朗读、viseme | 方言只经官方 API 与可选规范化接入 |
| 声纹 | CAM++ 或等价服务（待 POC） | 双人间身份确认信号 | 只作身份信号，失败走主动确认 |

配置只引用环境变量名（见 `.env.example`），不写真实密钥；Day1 不加载模型、不调用任何模型服务。
