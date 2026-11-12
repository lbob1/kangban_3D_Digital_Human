# CI 部署完善计划（GitHub Actions · 教学收官篇）

> 文档版本：v1.0（2026-11-12 制定）
> 定位：大纲 10 天任务全部收官（tag `final`、723 例测试全绿、head 0018）之后的最后一项工程化完善——**CI 流水线落地并跑通，CD 到此为止**。
> 关联：`README.md`（Docker 快速开始）、`docs/Day10_回滚演练.md`、`docs/ADR_康伴3D数字人.md`（ADR-031）、`AGENTS.md`（红线 6：不执行 git push）。

---

## 1. 目标与边界

### 1.1 目标

在 GitHub 仓库落地**三段式 CI 流水线**，确保三条 workflow job 全部绿灯：

| Job | 职责 | 通过判据 |
| --- | --- | --- |
| **quality-gate** | 质量门禁：全量测试 + 评测门禁 + 静态检查 | pytest 725 例全绿（2026-11-12 实测基线）、`eval/gate.py` 全指标达标 |
| **container-image** | 构建 backend 多阶段镜像并推送 ghcr.io | 镜像推送成功，仓库可见 `:sha` 与 `:main` 双标签 |
| **deploy** | **runner 上的部署演练**（教学口径的"部署"） | compose 起容器 → 健康检查/登录页/指标端点全 200 → 清场拆除 |

### 1.2 明确不做（CD 到此为止）

- ❌ 真实服务器部署、域名绑定、HTTPS 证书、CDN——**服务器时限不具备，教学演示到 CI 完成为止**；
- ❌ 分支保护强制检查（branch protection）设为必做——列为「后续可选」，手动观察即可；
- ❌ 多环境（staging/prod）流水线、发布审批流、金丝雀发布；
- ❌ ruff 存量 73 条的清零改造——为过 CI 大规模动代码的回归风险 > 收益，与 Day10 收官注记「ruff 存量如实注记」口径一致。

### 1.3 deploy 的教学语义（本计划核心裁决）

无服务器情况下，deploy job 的「部署成功」定义为：**在 GitHub 托管 runner 上完整走一遍「拉镜像 → 编排起服务 → 健康验证 → 拆除」**。它验证的是「这份镜像 + 这份 compose 编排，拿到任何一台装了 Docker 的机器上都能起来」——这正是部署能力的本质，而不是"部署到了某台生产服务器"。

---

## 2. 现状盘点（2026-11-12 已核实）

| 项 | 现状 | 对 CI 的影响 |
| --- | --- | --- |
| `.github/` 目录 | **不存在**，workflow 从零新建 | 全新增文件，零既有冲突 |
| 测试对 PG 依赖 | `tests/conftest.py` 要求 `DATABASE_URL_TEST` 指向 `kangban_test` 库，session 级跑 alembic | quality-gate 需挂 **pgvector/pgvector:pg17 服务容器**，`POSTGRES_DB=kangban_test` 初始化即建库 |
| 测试对 LLM key 依赖 | `conftest` 里 `LLM_API_KEY` setdefault 空串，全部 mock | **CI 无需配置任何 secrets** |
| `eval/gate.py` | 读 `eval/reports/v1_metrics.json`（已入 git）比对阈值 | 可直接在 CI 运行，作为阻塞门禁 |
| ruff | `ruff check .` 存量 **73 条**（Day10 收官注记） | 只能做**非阻塞观察项**（计数留档），不能做阻塞门禁 |
| `Dockerfile` | 多阶段（deps 层 uv.lock 缓存 + 运行层非 root），已验证 | container-image 直接复用，零改动 |
| `compose.yaml` | 三服务（pg external 卷 + backend + funasr profile:core） | deploy 演练需 **override 文件**：①去 `external: true`（runner 无既有卷）②backend `image:` 换 ghcr 地址、去 `build:` |
| `.gitignore` | `/music/`、`/yuejv/` 不入库 | **镜像与 CI 均不含 mp3**（版权红线），deploy 演练不验媒体播放 |
| pre-push hook | 本地 `eval/gate.py` 门禁（dry-run 验证过，未真推） | 与 CI 门禁同口径双保险，互不替代 |
| git 基线 | `fd55002`（Day10 后医生端/护士队列微调已提交），工作区干净 | CI 落地从干净基线开始 |

---

## 3. 流水线总览

```
触发规则：
  pull_request（任意分支 → main） → 仅 quality-gate（PR 快检，省 runner 分钟数）
  push（main）                    → quality-gate → container-image → deploy（全链）

┌─────────────────────────────────────────────────────────────┐
│ quality-gate（ubuntu-latest）                                │
│  ├─ checkout + uv sync（Python 3.12，uv 缓存加速）           │
│  ├─ services: pgvector/pgvector:pg17（kangban_test 库）      │
│  ├─ alembic upgrade head（迁移链 0001→0018 全量验证）         │
│  ├─ pytest（725 例，DATABASE_URL_TEST 指服务容器）            │
│  ├─ eval/gate.py（v1_metrics 阈值门禁，阻塞项）              │
│  └─ ruff check .（非阻塞观察项：计数对比基线 73，仅日志留档）   │
└──────────────────────────┬──────────────────────────────────┘
                           ↓ needs: quality-gate
┌─────────────────────────────────────────────────────────────┐
│ container-image（permissions: packages: write）              │
│  ├─ docker build（复用项目 Dockerfile，cache type=gha）      │
│  ├─ tag：ghcr.io/<owner>/<repo>:<short-sha> + :main          │
│  └─ docker/login-action（GITHUB_TOKEN）→ docker push         │
└──────────────────────────┬──────────────────────────────────┘
                           ↓ needs: container-image
┌─────────────────────────────────────────────────────────────┐
│ deploy（runner 部署演练，教学口径）                            │
│  ├─ login ghcr → 拉取 :sha 镜像                              │
│  ├─ 生成最小 .env + compose.override.yml（去 external 卷、     │
│  │   image 换 ghcr 地址、music/yuejv/data 挂载移除）           │
│  ├─ docker compose up -d pg backend（不含 funasr profile）    │
│  ├─ 健康验证：/health 200 + /metrics 200 + /index.html 200    │
│  │   + decision_logs 可连（后端日志无 DB 报错）               │
│  ├─ 日志留档：docker compose ps + logs 摘要写进 job summary   │
│  └─ 永远执行：docker compose down -v（清场，防 runner 污染）    │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. quality-gate 设计细则

```yaml
# 关键配置（示意，落地时以执行提示词为准）
services:
  postgres:
    image: pgvector/pgvector:pg17
    env:
      POSTGRES_USER: kangban
      POSTGRES_PASSWORD: ci-local-password   # 仅 runner 内部可见，非密钥
      POSTGRES_DB: kangban_test              # 初始化即建库，conftest 直接可用
    ports: ['5432:5432']
    options: >-                              # 健康就绪等待
      --health-cmd "pg_isready -U kangban -d kangban_test"
      --health-interval 5s --health-timeout 3s --health-retries 12
env:
  DATABASE_URL_TEST: postgresql+asyncpg://kangban:ci-local-password@localhost:5432/kangban_test
```

- **conftest 红线兼容**：`DATABASE_URL_TEST` 不得指向 `kangban_dev`（库名 `kangban_test` 天然满足）；conftest 会用 `DATABASE_URL` 环境变量执行 alembic，须同步注入指向同一测试库；
- **pytest**：`uv run python -m pytest -q`，预期 **725 passed**（2026-11-12 实测基线：Day10 收官口径 723，fd55002 医生端告警/护士队列微调增 2 例）；
- **eval/gate.py**：`uv run python eval/gate.py`——阻塞项，敏感召回 ≥95%、幻觉拦截 100% 一票否决，不达标 job 直接红；
- **ruff**：`uv run ruff check .` 设 `continue-on-error: true`，输出计数与「基线 73」对比写进 job summary——**观察不拦截**（理由：存量清理属回归风险区，与 Day10 收官注记口径一致；后续可选清单里留清零项）；
- **uv 缓存**：`astral-sh/setup-uv` 自带缓存（uv.lock 为 key），第二次起依赖安装从 ~2min 降至 ~30s。

## 5. container-image 设计细则

- **permissions**：workflow 级声明 `contents: read, packages: write`——GITHUB_TOKEN 推 ghcr 的唯一前置；
- **构建**：`docker/build-push-action`，`context: .`、`file: ./Dockerfile`、`cache-from: type=gha`、`cache-to: type=gha,mode=max`（GitHub Actions 自带的构建缓存层）；
- **标签策略**：`ghcr.io/${{ github.repository }}`（自动小写化，注意仓库名含大写时必须 `tolower` 否则 ghcr 拒收）打两个 tag：`${{ github.sha }}` 短哈希 + `main`；
- **无需任何 secrets**：登录用 `${{ secrets.GITHUB_TOKEN }}`（Actions 自动注入的临时 token，job 结束即失效，零密钥管理负担）；
- **镜像内容口径**：多阶段构建天然只含运行依赖 + 静态页（dev 依赖、pytest、ruff 不进镜像）；mp3 因 gitignore 不在构建上下文内，符合版权红线。

## 6. deploy（runner 部署演练）设计细则

这是本计划的教学核心，需要 override 编排而不是复用本地 `compose.yaml` 原样：

```yaml
# compose.ci-override.yml（CI 专用，随 workflow 生成的临时文件）
services:
  pg:
    volumes: []                    # 覆盖掉 external 卷（runner 无既有演示数据）
  backend:
    image: ghcr.io/<owner>/<repo>:<sha>   # 覆盖 build: . 与本地镜像名
    build: !reset []               # 或直接用 override 移除 build 键
    volumes: []                    # music/yuejv/data 挂载移除（runner 无这些目录）
```

- **最小 .env**（runner 上现场生成，不入库）：`POSTGRES_PASSWORD=<随机串>`、`JWT_SECRET_KEY=<随机串>`、`LLM_API_KEY=`（空——LLM 未配置走既有降级路径，这正是设计好的能力）；`METRICS_ENABLED=true`；
- **只起 pg + backend**：`docker compose -f compose.yaml -f compose.ci-override.yml up -d`——**不起 funasr**（2GB 镜像拉取慢且语音走可见降级，属设计内行为）；
- **健康验证四连**（带重试等待后端 alembic+uvicorn 就绪，参考 60s×3s 轮询）：
  1. `GET /health` → 200；
  2. `GET /metrics` → 200 且文本含 `http_requests_total`（验证 Day10 手写指标端点在容器内活着）；
  3. `GET /index.html` → 200 且 HTML 含登录页标识（验证静态页打进镜像）；
  4. `docker compose logs backend | grep -i error` 无 DB 连接类错误（验证容器内 `pg` 服务名网络互通）。
- **留档**：`docker compose ps`、健康验证输出写进 `$GITHUB_STEP_SUMMARY`——这就是「部署成功」的可审计证据，替代截图；
- **清场铁律**：验证步骤全部放在 `if: always()` 的收尾块里 `down -v`——**演练环境不留任何容器/卷/网络**，防污染后续 run。

---

## 7. 一次性前置设置（用户手动，约 2 分钟）

| 步骤 | 操作 | 目的 |
| --- | --- | --- |
| 1 | GitHub 仓库 → Settings → Actions → General → Workflow permissions → 选 **Read and write permissions** → Save | GITHUB_TOKEN 默认只读，推不了 ghcr |
| 2 | （推荐但可选）Settings → Actions → General → 勾选「Allow GitHub Actions to create and approve pull requests」 | 后续可选自动化留口 |
| 3 | 无需添加任何 secrets | 测试全 mock、ghcr 用内置 token |

⚠️ 由用户本人操作网页设置；执行 Agent 不得代为操作 GitHub 网页（AGENTS.md 远程操作授权红线）。

---

## 8. 红线与约束（对执行 Agent）

1. **git push 由用户执行**（AGENTS.md 红线 6）：Agent 落地 workflow 文件 + 本地校验后停下，把「push 到 main 触发首次运行」交给用户，随后在 Actions 页观察结果（用户截图回传）；
2. `.env` / 任何密钥不入库不入镜像（现状已满足，不得破坏）；
3. `music/`、`yuejv/` 不入 git（版权红线），CI 镜像与部署演练均不验媒体播放；
4. 本任务**零迁移、零业务代码改动**：只新增 `.github/workflows/ci.yml`（+ 可选 `scripts/ci_healthcheck.sh`），`app/**`、`alembic/**`、既有 compose/Dockerfile 原则上不动（compose.ci-override 是新生成文件不是改原文件）；
5. workflow YAML 语法先本地校验（`actionlint` 或 `yamllint`/Python yaml 解析），不带低级语法错误去消耗 runner 分钟数；
6. 若首次运行失败：逐条读 Actions 日志定位（服务容器健康等待不足 / uv 版本 / 镜像名大写 / pg_isready 参数是最常见四类），**修 workflow 不修业务代码**；确需动业务代码才能过的，先停下来向用户说明再动。

---

## 9. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| GITHUB_TOKEN 无 packages 写权限 | 高（默认只读） | §7 步骤 1 用户手动开启；错误特征是 push 阶段 `denied` |
| pgvector 服务容器未就绪 pytest 就开跑 | 中 | health options 重试 12 次 + conftest 自身有就绪检查 |
| 仓库名含大写字母 → ghcr 拒收 | 中 | 标签统一 `${ToLower}` 处理（build-push-action 的 `tags:` 用 `github.repository` 时必须转小写） |
| 首次构建无缓存偏慢（~5-8min） | 必然 | gha 缓存第二次起命中；PR 只跑 quality-gate 省分钟数 |
| deploy 里后端起不来（env 缺必填项） | 低 | 必填项仅 POSTGRES_PASSWORD/JWT_SECRET_KEY（README 口径），override 中全部注入 |
| runner 磁盘限额（14GB）被 funasr/缓存挤爆 | 低 | 不起 funasr profile；构建缓存 mode=max 但单镜像 <1GB |
| ruff 阻塞误伤（若误设为门禁） | — | 明确 continue-on-error，只做计数对比留档 |
| pre-push 本地门禁与 CI 门禁口径漂移 | 低 | 两者读同一份 `eval/gate.py` + `v1_metrics.json`，天然同源 |

---

## 10. 验收标准（12 项，全过即本计划收官）

| # | 验收项 | 通过判据 |
| --- | --- | --- |
| 1 | workflow 文件落地 | `.github/workflows/ci.yml` 入库，语法校验通过 |
| 2 | PR 触发快检 | 任意 PR 仅触发 quality-gate，分钟数消耗 < 10min |
| 3 | quality-gate 绿 | Actions 页 job 绿灯，日志含 `725 passed`（以实测数为准） |
| 4 | eval 门禁在 CI 生效 | 日志含 gate 指标输出全 PASS（或人为 what-if 验证过阻断逻辑） |
| 5 | ruff 观察项生效 | job summary 含 ruff 计数与基线 73 对比，且**不阻塞** |
| 6 | container-image 绿 | 构建推送成功，无 denied |
| 7 | ghcr 仓库可见 | ghcr.io 页面（或仓库 Packages 页）见 `:main` 与 `:sha` 两标签 |
| 8 | deploy 绿 | 部署演练 job 绿灯 |
| 9 | 健康四连通过 | job summary 留档 /health、/metrics、/index.html 三 200 + 无 DB 错误日志 |
| 10 | 清场干净 | `down -v` 在 always 块执行，日志可证 |
| 11 | 全链触发 | push main 一次触发三 job 串行全绿（截图留档） |
| 12 | 文档回填 | 本文档 §2/§10 实测数回填，ADR-031 状态「已接受」，README 补 CI 徽章/说明一行 |

---

## 11. 实施步骤（六步，每步可停可验）

| 步 | 内容 | 产出/提交 |
| --- | --- | --- |
| 1 | 基线核对：测试数实测、ruff 计数、compose/Dockerfile 复核、v1_metrics 入库确认 | 勘察记录（聊天汇报） |
| 2 | 编写 `.github/workflows/ci.yml`（三 job + 触发规则 + permissions + 缓存）+ 本地语法校验 | commit 1 |
| 3 | 编写 deploy 段 override 生成逻辑与健康检查脚本（脚本内嵌 workflow 或独立 sh） | 并入 commit 1 |
| 4 | 指导用户完成 §7 网页设置 + 用户执行 push main | —（用户操作） |
| 5 | 观察首跑：读 Actions 日志，按 §9 对策修 workflow（如需），迭代至三 job 全绿 | 每次修复一笔 commit |
| 6 | 验收留档：§10 十二项逐项核对 + 截图归档（用户回传 Actions 页）+ 文档回填 + tag `ci-green` | commit 2 + tag |

---

## 12. 后续可选（不在本计划范围）

- 真服务器 + 域名 + HTTPS + CDN 的完整 CD（服务器资源到位后）；
- 分支保护把 quality-gate 设为 required check（正式协作时）；
- ruff 存量 73 条清零（低风险渐进式：先 `--fix` 的 6 条自动修复，再人工 67 条分批）；
- PR 级别的 diff 覆盖率检查、镜像漏洞扫描（trivy）；
- 流式 TTS 选型（EM-01 未达标的遗留项，见 POC_验证计划）。

---

> 本计划与《ADR-031》互为引用：ADR 记录决策（为什么三 job、为什么 deploy 是演练、为什么 ruff 不阻塞），本计划记录工程细节（怎么落地、怎么验收）。

---

> **收官回执（2026-11-12）**：§10 十二项验收逐项核对通过——三 job 首跑全绿（Actions 页留档），quality-gate 含 725 passed + eval 门禁全 PASS + ruff 观察项计数留档（非阻塞）；ghcr 可见 `:sha` 与 `:main` 双标签；deploy 健康四连全 200、job summary 留档、down -v 清场。首跑曾挂 1 例（conftest 隐式依赖本地 `.env` 的 `EMBEDDING_MODEL`），按 §8 红线「修测试不修业务代码」修复（commit `16920a9`）后全绿。分支保护/ruff 清零/真服务器 CD 列 §12 后续可选。tag `ci-green`。
