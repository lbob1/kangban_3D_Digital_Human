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

## Day2 架构摘要（身份与权限）

Day2 交付身份与权限地基：人账号 bcrypt + JWT 登录（60 分钟）、设备 Token 签发/吊销（90 天、只存 SHA-256 哈希）、
`/auth/me` 双向身份自查、长者档案 CRUD（管理员）与范围查询（护工按楼栋、医生按关联）、
设备绑定与 `devices/me` 自查（单人间锁定 / 双人间候选）、三张授权关联表管理接口、
种子管理员脚本与独立测试库 `kangban_test`。人 JWT 与设备 Token 严格互斥，互调 403。

## Day3 架构摘要（实时语音链路）

Day3 交付实时语音全链路，三个外部能力均按适配器模式抽象（ADR-018，可插拔）：
WebRTC 音频通道（aiortc，`POST /api/v1/rtc/offer` 设备 Token 鉴权，事件循环绑定 uvicorn）、
WebSocket 事件通道（`/api/v1/voice/ws`，auth/verification/transcript/tts/error 事件流）、
FunASR 流式识别（16k mono PCM）、Edge-TTS 流式合成、CAMPPlus 声纹验证
（192 维向量入库 voiceprints 表，验证通过发 300 秒免验 Token）。
身份遵循 ADR-005：单人间设备→房间级联锁定，双人间声纹在住长者集合中匹配，
`elder_id` 永不接受前端传参；临时声纹 Token 仅内存不落库。
FunASR/声纹模型未配置时应用照常启动，走 WS 可见降级 + 文字输入通道，绝不静默失败；
原始音频不落盘、不入日志，只记时长/延迟/置信度元数据。

## Day4 架构摘要（3D 数字人与 LLM 对话）

Day4 在语音链路上叠加 3D 形象与真实对话：three-vrm 本地化渲染（零外部 CDN 依赖），
7 个 VRM 形象注册表驱动切换、加载失败自动回落且不影响语音链路；
口型同步以音频能量近似驱动（TTS → MediaStreamDestination → 引擎 analyser）；
回复链升级为 LLM 流式分句（Qwen 兼容 API + 人设 few-shot prompt），
`[act:*]` 动作标记剥除后单独下发（不朗读、不进字幕）；
医疗话题硬拒答（few-shot 约束 + 真实链路实测验证）。会话按 ADR-021 用
PostgreSQL 持久化（conversation_messages 成对落库），替代 Redis。
语音链路同步加固：TTS 回声防护窗、VAD 连续帧起判与最小语音时长过滤、
FunASR 零结果自适应死线（消除噪声误判导致的会话阻塞）。
测试基线 192 例全绿，ruff 当日改动零新增。

## Day5 架构摘要（全链路延迟与流式体验）

Day5 完成延迟观测、熔断降级与口型升级三件事（tag `iter2-demo`）：
口型同步从能量近似升级为音素级 viseme 时间轴（EdgeTTS WordBoundary →
「词→口型→时刻」时间轴先于音频经 WS 下发，前端 `playViseme` 按播放起点调度五种近似口型，
引擎不支持时零改动回落能量口型）；新增熔断三档（ASR 终句超 2s 朗读"没听清"、
LLM 首 token 超 3s 播拟真占位语"嗯~我想想哈。"每轮至多 1 次、TTS 首包超 1s 字幕先行，
均经 hint/tts 事件可见降级）并预留 `app/services/guard.py` ToolCallGuard
调用预算+熔断双闸框架（假时钟单测，Day6 接 MCP Tool 调用点零改动生效）；
文字输入通道接入同一条 LLM 回复管线（空文本拒绝、500 字截断）。
新增迁移 0007 `latency_metrics` 延迟打点表（asr_wait/llm_first/llm_total/tts_first/e2e
分列 + 占位/成功标记），POC-08 固定 20 语句 ×20 轮跑批并以 SQL percentile_cont 独立复算：
定位 qwen3 思考模式为首 token 根因，`enable_thinking=false` 根治后
首 token 2980ms→483ms（-84%），e2e P50 4480ms→2561ms、P95 10600ms→4560ms（-57%）；
剩余大头为 EdgeTTS 首包 ~1.3s（免费非流式），教学口径暂不换流式厂商，
差距如实记录于 POC-08（EM-01 ≤1.5s 未达标，留待 TTS 选型）。
演示前体验优化：语音页新增老人生物识别免 Token 进入（房间选择 → 指纹演示直进 /
声纹真比对 Day3 声纹库，后端签发短效设备 JWT 自动连接，显式 Token 输入保留）、
数字人四态状态徽章（待机/聆听/思考橙黄律动/回复绿律动，单一状态机事件驱动防卡状态）、
生物识别房间弹窗流转修复。测试基线 252 例全绿，覆盖率 67%→70%。

## Day6 架构摘要（四级级联路由与敏感 HITL）

Day6 落地四级级联路由与 HITL 人工确认闭环（16 项验收 13 项自动实测通过，
测试基线 252→361 全绿，覆盖率 70%→73%；验收明细见本地 `Day_plan/Day6_验收报告.md`，
该目录不入 Git，clone 后无此文件）：
路由口径为**敏感词表 → 规则层 → System One（laya）→ 主控 Agent**
（`app/router/cascade.py` 接入语音链路）：敏感词表六类命中一票否决——只播安抚话术、
不调用大模型，工单落 `workorders(type=sensitive_alert)` 并经 `decision_logs` 全程留痕
（迁移 0008）；规则层 10 条表驱动固定指令直达（零模型）；System One 采用
laya 0.4.30 本地权重白名单（POC-03 方案 A，AGENTS.md 豁免注记，200ms 超时降级）；
主控 CompanionAgent 以 ReAct + ToolCallGuard 预算接线，MCP 天气 Tool
（stdio + open-meteo + 重试降级）经规则/S1 双路径汇合同一回复管线；
`report_emergency` 作为第四道防线 Tool。评测基线 50 条 fixture：
意图准确率 96.4%、敏感召回 100%（`eval/reports/day6_router_baseline.md`）。

## Day6+ 追加迭代摘要（批1-6）

Day6 后按用户逐批验收追加 6 批（提交 `9647f13`…`559d69f`，已随主线推送至远端）：
批1 敏感词表隔字正则 + 天气 date 参数收口；批2 敏感工单分级可见性矩阵
（abuse 仅 admin+family，护工/医生不可见；迁移 0009 `workorders.category`）+
护工/医生弹窗提醒 + `report_emergency` Tool；批3 对话记录双端可见——老人端
声纹验证后最近 20 轮（服务端 Runtime Context 判定 elder，未验证 403）、管理端
按长者全历史 + decision_logs 路由叠加；批4 对话块分层重构（每次「开始对话」
新开块、块ID+子对话ID 两级、北京时间统一、倒序、工单标记）+ 修复管理端
`KB.session` 笔误致加载器崩溃；批5 紧急工单卡片内嵌护工/医生/家属工作台
（护工/医生 10s 轮询弹窗、家属只读）+ 老人端对话块展开/收起；批6 工单按账号
独立已读（`workorder_reads` 表）+ 行点击详情弹窗 + resolve 开放护工/医生
（abuse 仍仅 admin）、原因说明+解决方案必填（`workorders.solution` 独立列，
迁移 0010）、家属端可见处理状态与解决方案。

## Day7 架构摘要（记忆五步链路 + 用药确认图 + 娱乐点播，tag `iter3-demo`）

Day7 落地三条业务链路（测试基线 396→515 全绿，详见 `eval/reports/v1_baseline.md`）：

- **记忆五步链路**（大纲 7.1 / US-03 / POC-07）：`app/services/memory.py` 封装
  萃取（块结束 LLM 3 条 JSON schema）→ 存储（1024 维向量，0012 固定维度 + ivfflat；
  0013 补 importance 列）→ 召回 top-5（强制 `elder_id + visibility + status` 三件套过滤，
  elder_id 只从服务端 Runtime Context 注入）→ 召回后 LLM 二次过滤（降级 top2 importance）→
  冲突检测/遗忘归并（矛盾旧记忆 superseded 不物理删除；importance × 90 天衰减 + 周归并）。
  冲突相似度阈值经 POC-07 实测校准 0.9→0.75（偏差记录见 POC 结果栏）；
- **用药确认图**（大纲 7.2 / US-01/02 / POC-06，ADR-023）：LangGraph StateGraph +
  AsyncSqliteSaver checkpointer，`calling→waiting_confirm→confirmed/retry(≤3)/escalated`，
  `thread_id=med-{plan_id}-{date}-{slot}` 幂等复入，重启 `get_state` 断点续跑；
  **定时外呼（APScheduler）与对话内确认（S1/规则/主控 Tool）共用同一张图**；
  外呼执行走 WS 下行 `medication_reminder` + 既有 TTS 链路（D1 口径，服务端无法向离线
  浏览器主动发起 WebRTC，离线退避 3 次落漏服工单，文档如实标注与大纲字面差异）；
- **娱乐点播**（大纲 7.3 / US-06）：「我想听越剧」规则直达偏好选曲真播放、「换一段」
  排除最近播放切歌、「停/大声点」复用 Day6 分支零模型；`media_history` 落库留痕；
- **评测基线 v1**（大纲 7.4）：`eval/fixtures/scene20.json` 20 场景六类 +
  `scripts/bench_day7.py` 离线可复算 + 8 项指标/六层失败归因 + `eval/gate.py` 阈值门禁
  （敏感召回≥95% 不达标 exit 1，CI hook 留 Day10）。

**媒体版权边界声明**：`media_library` 版权 CHECK 强制——仅
`copyright_status ∈ (licensed, teaching, public_domain)` 的曲目允许 `status=active`
对外可达，未授权素材一律 hidden 且不可播放。当前演示曲库全部为 `scripts/import_media.py`
运行时合成的正弦波音频（本地生成占位波形，**不含任何真实曲目片段**）；
接入真实戏曲/影视内容必须先取得授权凭证并将 `copyright_status` 置为合法值，
严禁录入未授权媒体地址。

## Day7+ 联调补丁摘要（2026-11-10，4 笔提交，ADR-025）

Day7 标签后当天联调实测暴露 7 个问题，全部当场定位修复（测试 499→515）：

- **点播双通道汇合（ADR-025）**：新增主控工具 `play_media`（`app/tools/play_media.py`）——
  自由话术（「播放富士山茶」「来点陈奕迅的歌」）由 LLM 理解后确定性执行：曲库内相似度
  模糊匹配（阈值 0.45，容忍 ASR 错字），匹配不到如实返回曲库名单，**禁止编造播放成功**；
  规则快路径原地扩词（引导词「播放/放/来/点/唱」+「歌/戏曲/戏剧」，仍十条封顶）；
  两通道汇合同一 `media_service` 管线（同一留痕/WS 下行/播放器）；
- **曲库播放器弹窗**（`app/static/voice.html` + `voice.js`）：语音点播自动弹出 +
  「🎵 曲库」手动打开；暂停/上一首/下一首/进度拖动/自动连播；关闭弹窗不停播；
  播放期麦克风自动静音防回声转写（暂停/结束恢复）；语音「停」跨弹窗生效；
- **多意图合并回复**：信息类规则同句并列按出现顺序合并回答——「明天天气如何？现在几点了？」
  两问都答（`rules.match_rules`，执行类动作不参与合并）；
- **链路韧性**：ASR 终句 20s 硬超时（防 FunASR 不回终句导致会话永久挂死）；
  TTS 失败自动重试 1 次 + 真实错误详情直接上屏（实测曾定位「启动服务时代理软件未关」
  导致的 SSLCertificateError——**启动服务前请关闭系统代理**）；
- **开始对话一键恢复**：WebRTC 通道异常自动重建（原需点两次），断线状态条明示；
- **对话块默认收起**：左侧对话记录默认收起全部块、仅最新一块展开（点击块头查看其余）；
- **媒体 404 修复**：`/static` 别名挂载（⚠️ 必须注册在 `/` 挂载之前——`Mount("/")`
  前缀通吃，后注册的 `/static` 永不命中）；
- **用药管理补丁**：列表接口自带 `elder_name`（修「未知长者」显示）；新增
  `GET /api/v1/medication/elders`（医生/管理员，全部在住长者下拉——仅用药管理放开）；
  写权限=医生+管理员，DELETE 软删（cancelled）；
- **用户自备曲库**：迁移 0016 扩 `music` 类型；`scripts/import_user_media.py` 扫描
  `music/`、`yuejv/` 文件夹幂等导入 mp3。**用户自备素材已加 .gitignore 不入库，
  仅限本地教学演示，不可分发**。

## Day10 架构摘要（Docker 化 + 本地观测 + 收官，tag `final`）

Day10 完成三件事（16 项验收详见验收报告）：**统一 compose 编排**（pg + backend + funasr `profile: core`，
backend 多阶段 Dockerfile 首次容器化，容器启动自动 `alembic upgrade head`，接管既有命名卷
`kangban_pg_data` 演示数据零丢失）；**本地可观测双通道**（手写 `/metrics` Prometheus 文本端点——
进程计数 + P50/P95 环形缓冲 + decision_logs 时间窗聚合，口径对齐 `eval/gate.py`；
`scripts/trace_lookup.py` 按 thread_id 一条命令串全链并生成自包含 HTML 报告存
`eval/reports/trace/`，决策 D2 变更为纯本地方案，不接 Langfuse，trace 数据不出本机）；
**回滚演练 + pre-push 门禁**（代码回滚与 alembic 可逆双通道留档 `docs/Day10_回滚演练.md`，
hook 接 `eval/gate.py` 不达标阻断推送）。

### Docker 快速开始（同学电脑 / 演示口径）

```powershell
git clone <repo-url>
cd 1103_3D_Digital_Human
Copy-Item .env.example .env        # 填写下方必填项
docker volume create kangban_pg_data   # 必须先建卷：compose 引用既有命名卷（external），新机器缺卷会启动失败
docker compose up -d               # pg + backend，迁移自动跑；funasr 可选 --profile core
curl.exe -i http://127.0.0.1:8600/health
python scripts/seed_admin.py       # 创建 admin（容器外跑，连 15433；密码交互输入）
# 浏览器打开 http://127.0.0.1:8600/static/login.html
```

**`.env` 必填项清单**（其余变量占位可启动，语音链路未配置走可见降级）：

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_PASSWORD` | pg 容器初始化口令；须与 `DATABASE_URL` 内嵌口令一致 |
| `DATABASE_URL` / `DATABASE_URL_TEST` | compose 部署时容器内自动覆盖为 `pg:5432`，无需改；本机直跑用 `127.0.0.1:15433` |
| `JWT_SECRET_KEY` | 登录功能必需；`python -c "import secrets; print(secrets.token_hex(32))"` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 自由对话必需；未配置时回复降级模板话术 |

### 成本与延迟实测（Day10 步骤 6，取数源 `eval/reports/v1_metrics.json` + decision_logs）

| 指标 | 实测值 | 口径说明 |
| --- | --- | --- |
| S1 闭环率 | **68.75%**（目标 ≥60% 达成） | 33 场景中 16 个 decide 驱动场景不经主控即闭环；gate 基线 0.50 为观察项 |
| 任务成功率 / 敏感召回 / 幻觉拦截 | 100% / 100% / 100%（阻塞硬线） | 33/33 场景，离线可复算（`scripts/bench_day7.py`） |
| P95 延迟 | 106.4ms | **离线桩口径**（场景级墙钟含 DB）；真实链路口径见下 |
| 单会话 Tool 调用 | 299 条决策：0 次 152 条 / 1 次 147 条，**全部 ≤1** | decision_logs 实查；远低于 ToolCallGuard 预算上限 8 |
| 分层记忆省 Token | 召回 top-5 注入 ≈ 数百 token vs 全历史注入（50 轮 × 百 token 级 ≈ 数千 token），**估算省 90%+** | 估算口径非实测：项目未做真实 LLM token 双路对比计量，按「top-5 条记忆 vs 全量对话历史注入」规模差估算，如实标注 |
| prompt cache 命中率 | **N/A** | 项目未实现 prompt cache；若百炼网关返回 cached 字段则统计，否则不造假数据 |
| token 成本（评测轮） | 外部 embedding 0 次、外部 LLM 0 次 | 离线桩口径（stub 2/1 次）；真实 API 计量未在 Day10 范围，如实注记 |

**真实链路延迟**（Day5 POC-08 实测，20 语句 ×20 轮）：`enable_thinking=false` 后 LLM 首 token
483ms，e2e P50 2561ms / P95 4560ms；剩余大头为 EdgeTTS 首包 ~1.3s（免费非流式，教学口径保留）。

### 运维注记（Day10 实测踩坑）

- **宿主机端口残留进程**：本机直跑过 uvicorn 的残留进程会占住 127.0.0.1:8600 挡住容器端口映射
  （容器 healthy 但 curl 不通）。启动前 `netstat -ano | findstr :8600` 检查，命中则 `taskkill /PID <pid> /F`；
- **测试库**：pytest 需 `kangban_test` 库（compose 起 pg 后建库 + vector 扩展）；
  compose 容器名与本机直跑一致（`kangban-pg`），两种部署口径通用同一条命令：
  `docker exec kangban-pg psql -U kangban_dev -d kangban_dev -c "CREATE DATABASE kangban_test;" -c "\c kangban_test" -c "CREATE EXTENSION IF NOT EXISTS vector;"`；
- **曲库目录**：`music/`、`yuejv/` 因版权不入 Git 不进镜像，同学侧为空属预期（扫描导入 0 新增）。

## 环境要求

- Python >= 3.12
- PostgreSQL 15+ 且已安装 pgvector 扩展（本地开发推荐直接使用 `pgvector/pgvector:pg17` Docker 镜像）
- 本地开发库：`kangban_dev`（不连接任何生产数据库）

## 目录说明

```
app/
├── main.py          # create_app 应用工厂与 Uvicorn 入口
├── api/             # HTTP 路由聚合（/health 与 /api/v1 业务接口）
├── auth/            # 人 JWT、设备 Token、密码哈希与鉴权依赖（高风险区）
├── core/            # 配置、日志、异常边界、生命周期
├── middleware/      # trace_id 中间件
├── db/              # 异步引擎与会话入口
├── models/          # SQLAlchemy 2.x ORM（11 基础表 + Day2 6 表）
├── schemas/         # Pydantic 请求/响应结构
└── services/        # 领域服务（health、account、authorization、elder、device）
alembic/             # 迁移环境与版本（0001/0002 基础表、0003 账号凭证、0004 授权关联）
scripts/             # seed_admin.py 种子管理员
tests/               # pytest（连接 kangban_test，事务回滚隔离）
docs/                # ER、PRD/TDD/ADR、POC 计划、prompts 提示词模板库
```

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖（含 dev 与 infra 扩展组）
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,infra]"

# 2. 准备本地配置：复制 .env.example 为 .env，填写本地 PostgreSQL 连接信息
Copy-Item .env.example .env

# 3. 本地 PostgreSQL（Docker 方式，仅本地开发；端口按本机实际占用调整）
docker run -d --name kangban-pg -p 5433:5432 `
  -e POSTGRES_USER=kangban_dev -e POSTGRES_PASSWORD=<本地开发密码> -e POSTGRES_DB=kangban_dev `
  pgvector/pgvector:pg17

# 4. 执行迁移（执行前必须人工阅读 alembic/versions/ 下迁移文件）
alembic upgrade head

# 5. 创建独立测试库（pytest 专用，与开发库隔离）
docker exec kangban-pg psql -U kangban_dev -d kangban_dev -c "CREATE DATABASE kangban_test;" `
  -c "\c kangban_test" -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 6. 创建首个管理员（仅当无 admin 时生效；密码交互输入，不回显不落日志）
python scripts/seed_admin.py

# 6a. 演示数据准备（可选；演示/验收前执行，跳过则曲库为空、用药无种子计划）
uv run python scripts/import_media.py   # 生成合成波形演示曲库（本地生成，不含真实曲目片段）
uv run python scripts/seed_day7.py      # 幂等写入 5 位在住长者的用药计划/媒体偏好/播放留痕（可重复执行）

# （可选）导入自备 mp3：曲库为空或想用本地音乐/戏曲时执行（已加 .gitignore 不入库，仅本地演示）
uv run python scripts/import_user_media.py

# 7. 启动服务（启动前关闭系统代理，否则 TTS 走代理报 SSL 证书错误）
python -m uvicorn app.main:app --reload

# 8. 验活
curl.exe -i http://127.0.0.1:8000/health

# 9. 测试（连接 DATABASE_URL_TEST 指向的 kangban_test，事务回滚隔离）
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

### Day2 必填配置（登录/设备接口启用前必须配置）

| 变量 | 说明 |
| --- | --- |
| `JWT_SECRET_KEY` | 签名密钥；本地生成 `python -c "import secrets; print(secrets.token_hex(32))"`，只写入 `.env`，不进 Git/日志 |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 人 JWT 有效期（默认 60 分钟） |
| `DEVICE_TOKEN_EXPIRE_DAYS` | 设备 Token 有效期（默认 90 天） |
| `DEVICE_REGISTRATION_TOKEN_HOURS` | 一次性设备注册凭证有效期（默认 24 小时） |
| `DATABASE_URL_TEST` | pytest 专用测试库连接串（kangban_test），与开发库隔离 |

### 种子管理员与测试

```powershell
# 创建首个管理员（已存在 admin 时跳过，幂等）
python scripts/seed_admin.py

# 测试直接运行；conftest 读取 DATABASE_URL_TEST 连接 kangban_test，
# session 级执行 alembic upgrade head，函数级事务回滚隔离，不写 kangban_dev
python -m pytest
```

### 后续预留配置（Optional，空值不影响启动）

`LLM_*`、`EMBEDDING_*`、`SYSTEM_ONE_*`、`ASR_*`、`TTS_*`、`DIALECT_*`、`VOICEPRINT_*`、
`WEBRTC_*`、`AUDIO_RETENTION_HOURS`、`LANGFUSE_*`。
这些变量在对应功能阶段才做必填校验；当前全部为 `CHANGE_ME` 或空占位，不得填入真实密钥。

### Day3/Day4 语音链路配置（Optional，未配置走可见降级）

| 变量 | 说明 |
| --- | --- |
| `FUNASR_URL` | FunASR WebSocket 服务地址；未配置时语音识别降级为文字通道 |
| `FUNASR_INSECURE` | 跳过 FunASR 证书校验（本地 Docker 自签场景，默认 false） |
| `SHERPA_MODEL_DIR` | CAMPPlus 声纹模型目录；未配置时声纹门控整体降级 |
| `VOICEPRINT_THRESHOLD` | 声纹验证阈值（默认 0.55，POC-04 复核后只记不调） |
| `VOICEPRINT_TOKEN_TTL` | 声纹验证通过后免验 Token 有效期（默认 300 秒，仅内存） |
| `EDGE_TTS_VOICE` | Edge-TTS 音色（默认 zh-CN-XiaoxiaoNeural，无需密钥） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Qwen 兼容 API 配置；未配置时回复降级为模板话术，通话不中断 |

### Day5 熔断三档阈值（Optional，默认即大纲值，.env 可选覆盖）

| 变量 | 说明 |
| --- | --- |
| `ASR_PROMPT_SECONDS` | 一档：终句等待超时（默认 2 秒）→ 朗读"没听清，请再说一遍" |
| `LLM_PLACEHOLDER_SECONDS` | 二档：LLM 首 token 超时（默认 3 秒）→ 播占位语，每轮至多 1 次 |
| `TTS_SLOW_SECONDS` | 三档：首句 TTS 首包超时（默认 1 秒）→ 字幕先行提示 |

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
