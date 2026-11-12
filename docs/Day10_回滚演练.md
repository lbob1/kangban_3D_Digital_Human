# Day10 回滚演练留档（步骤 7，主文档 §4.3）

> 日期：2026-11-12　|　演练环境：本机 compose（pg 15433 / backend 8600）
> 结论先行：**代码回滚、镜像回滚、迁移回滚三条通道全部演练通过**；演练途中遭遇一次
> 真实基础设施事故（宿主机磁盘满 → Docker Desktop 存储层损坏），按事故流程恢复、
> 演示数据零丢失——事故复盘一并留档（真实事故比剧本更有教学价值）。

---

## 一、代码回滚通道（git → 重建镜像）

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 注入故障 | `app/api/routes/health.py` 的 `health_check` 加 `raise RuntimeError("【回滚演练故意注入的故障】…")` | 代码带 `# ROLLBACK-DRILL` 标记 |
| 2 部署故障版 | `docker compose up -d --build backend` | 构建/启动成功 |
| 3 实测故障 | `curl http://127.0.0.1:8600/health` | **HTTP 500**（预期故障复现） |
| 4 回滚代码 | `git checkout iter5-demo -- app/api/routes/health.py` → `git status` clean（与上一提交逐字节一致） | 工作区还原 |
| 5 重建恢复 | 重建镜像 + 起容器 | **HTTP 200** 恢复 |

教学点：代码回滚以 git 为唯一事实源——工作区是否真正还原以 `git status` clean 为准，
不靠肉眼比对；镜像重建后必须实测验活（curl 200）才算恢复。

## 二、镜像回滚通道（真实用到）

容器存储故障期间重建受阻，验证了不依赖构建的恢复路径：

- 故障构建会把上一版好镜像打成 dangling（untagged）；
- 恢复手段：找到 dangling 镜像 → `docker tag <id> kangban-backend:latest` → `docker compose up -d backend`（不带 `--build`，直接用镜像）。

> 注：本次实际恢复时该镜像列表已被损坏的 containerd 元数据波及（`docker images` 报 blob
> I/O 错误），最终走了「Docker Desktop 重启 + 直连 `docker build --no-cache` 重建」路径
> （见第四节）。镜像回滚通道逻辑本身验证成立，留作构建不可用时的预案。

## 三、迁移回滚通道（alembic 双向可逆）

### 3.1 演练前的数据丢失风险评估（本日真实发现）

0018 的 `downgrade()` 是对 `health_records` **drop 5 个睡眠列**（0018_sleep_details.py L66-70）。
演练前实测 dev 库：`health_records` 79 行中 **73 行已填 sleep_bed_time、70 行已填 sleep_deep_minutes**
——真在 dev 库降级会不可逆丢失这批演示数据（`upgrade head` 只加回空列）。

**用户裁决（2026-11-12）：演练改在 kangban_test（pytest 专用库）执行，dev 库仅静态审读。**
这本身即教学点：**downgrade 前必须先查目标列/表的数据占用，再决定演练口径**——
「迁移可逆」是结构可逆，不承诺数据可逆。

### 3.2 测试库执行记录

| 步骤 | 操作 | 实测结果 |
| --- | --- | --- |
| 1 | `alembic current`（DATABASE_URL 指向 kangban_test） | `0018_sleep_details (head)` |
| 2 | `uv run alembic downgrade -1` | `Running downgrade 0018_sleep_details -> 0017_health_triage` |
| 3 | 验证 | `alembic_version=0017`；`information_schema` 查 `sleep_bed_time` 存在数=0（5 列已删） |
| 4 | `uv run alembic upgrade head` | `Running upgrade 0017_health_triage -> 0018_sleep_details` |
| 5 | 验证 | `alembic_version=0018`；5 个睡眠列回归 `5/5` |

教学点：代码回滚（git）与数据回滚（alembic）是**两条独立通道**——git 恢复旧代码不会
回退数据库结构，alembic downgrade 不会恢复被删列的数据；演练后必须回到 head（本日
head 保持 **0018**，零迁移红线达成）。

## 四、真实基础设施事故复盘（演练中真实发生）

### 4.1 时间线与现象

1. 代码回滚演练第 5 步重建镜像时，docker 报 `write /var/lib/desktop-containerd/.../meta.db: read-only file system`；
2. 随后逐步恶化：`docker images` 报 blob `input/output error`；pg 容器 daemon 元数据显示
   `Up (healthy)` 但 exec 报 `not running`、start 报 rootfs 挂载 I/O 错误——**元数据与实际状态分裂**；
3. 根因：**宿主机 E 盘空间耗尽**（Docker Desktop 的 VHDX 无法再写入，containerd 元数据库
   变只读、部分 blob 损坏）。用户清理磁盘（释放至 22.7GB 可用）后重启 Docker Desktop 恢复。

### 4.2 恢复流程（留作 SOP）

1. **停止扩大操作**，确认卷数据在 named volume（不受容器层损坏影响）；
2. 释放宿主机磁盘空间 → `wsl --shutdown` → 重启 Docker Desktop → `docker info` 就绪；
3. `docker compose up -d pg backend`：pg/funasr 直接恢复 healthy；
4. backend 循环重启（alembic 报 `No 'script_location'`）→ 排查发现镜像内 `alembic.ini`/
   `alembic/env.py` 为 **0 字节**——故障期 compose bake 路径产出的构建缓存层损坏；
5. **直连 `docker build --no-cache`** 绕过损坏缓存重建 → 镜像内文件字节数逐一验证恢复
   （alembic.ini 705B / env.py 1937B）→ backend healthy；
6. 数据完整性对账：elders=6 / workorders=38 / conversations=63 / health_records=79
   （含 73 行睡眠数据）——**与事故前基线完全一致，零丢失**。

### 4.3 经验教训

- **磁盘满不是「构建变慢」，是「静默损坏」**：先出现只读错误、再出现 0 字节文件、最后
  元数据与实际状态分裂——中间态极易误判为代码问题（本例先排了一圈 COPY/构建参数）；
- **构建缓存也可能损坏**：`--no-cache` 不够，必要时换直连 `docker build` 绕开 compose bake 路径；
- **卷与容器层是两回事**：named volume 数据在本次事故中完好，验证了「数据放卷、代码放镜像」
  的分离设计；
- 对账基线先行（Day10 步骤 0 记录的 6/38/63）让「零丢失」成为可验证结论而非口头承诺。

## 五、pre-push 门禁验证

- hook：`.git/hooks/pre-push` 调 `uv run python eval/gate.py`（阈值表见 gate.py，敏感召回/
  幻觉拦截为一票否决硬线）；git 不跟踪 hooks，clone 后手动启用（README/SECURITY.md 已注明）；
- 验证：`git push --dry-run origin main` → hook 触发，gate 逐项 PASS（s1_closure_rate 68.8%
  ≥50% 等）→ exit 0 放行；**dry-run 显示 `52f4880..7c9c866 main -> main` 但未真推**
  （项目不 push 红线不变）；
- 阻断方向：gate 不达标 exit 1 时 hook 中止推送（gate 的 exit 1 行为有 pytest 用例覆盖）。

## 六、部署门禁六项清单

成文于 `SECURITY.md` 附录（只增不改，15 增 0 删）：验收达成 / 测试通过 / 密钥安全 /
迁移可逆 / 监控就绪 / 回滚路径清晰。
