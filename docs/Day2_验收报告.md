# Day2 验收报告

> 执行日期：2026-11-04　|　范围：Day_plan/Day2_3D康伴数字人.md 第 9 章 16 项验收
> 环境：Windows / Python 3.13.7 / PostgreSQL(pgvector 容器 kangban-pg:5433) / kangban_dev + kangban_test
> 口径说明：Day2 文档标题写"新增 6 表/17 张"，实际按用户裁决执行 **新增 5 表 / 共 16 张业务表**（5 新表 + Day1 11 表），下表第 3 项按此口径记录。

| # | 验收项 | 命令/方法 | 实际结果 | 是否通过 | 证据（关键摘录） |
| --- | --- | --- | --- | --- | --- |
| 1 | 2.1 清单文件存在 | PowerShell `Test-Path` 批量校验 32 个新增/修改文件 | 全部存在，无缺失 | ✅ | `ALL 32 FILES EXIST` |
| 2 | 提示词模板库 | 查看 `docs/prompts/` | 7 个文件（README + 6 模板），四要素齐备 | ✅ | `01_需求澄清.md` … `06_写commit.md` + `README.md` |
| 3 | 迁移与表结构 | `alembic upgrade head` 后 psql `\dt` / `\d` | 17 个表名 = 16 业务表 + alembic_version（按 5 新表口径）；accounts/device_tokens/doctor_elder_assignments 字段、CHECK、唯一约束、外键 RESTRICT、索引完整 | ✅ | `ck_accounts_role`、`uq_device_tokens_token_hash`、`uq_doctor_elder_assignments_acct_elder` 等 |
| 4 | 种子管理员幂等 | 连续执行 `python scripts/seed_admin.py` 两次 | 首次创建、重复执行跳过；accounts 恰 1 行 | ✅ | `已存在管理员账号，跳过创建（幂等）。`（两次）；本地账号 `admin` / `AdminLocal2026!` |
| 5 | 登录 | HTTP 实测 `POST /api/v1/auth/login` | 正确 200 含 role=admin；错误密码与不存在用户均 401，错误体一致（除 trace_id） | ✅ | `[PASS] 200 role=admin`；`401/401 一致=True` |
| 6 | 伪造/过期 Token | 篡改签名、过期 JWT 调 `/auth/me/user` | 均 401 | ✅ | `伪造签名/过期 JWT → 401/401` |
| 7 | /auth/me 双向身份与互斥 | 人/设备 Token 分别调 me 与对方接口 | 人 Token 返回 `admin/admin`；设备 Token 返回设备身份；人→`/devices/me` 403、设备→`/auth/me/user` 403 | ✅ | 日志 `auth_forbidden result=token_type_mismatch` x2 |
| 8 | 非管理员建长者 | 护工/医生/家属 Token 调 `POST /elders` | 403 ×3 | ✅ | 日志 `role_not_allowed:nurse/doctor/family` |
| 9 | 范围查询 | 护工查本楼/跨楼、医生查关联内/外、家属查询 | 本楼 200；跨楼 404 且响应不含姓名；医生关联内 200/关联外 404；家属 403 | ✅ | `200/404 泄露=False`；`200/404`；`403` |
| 10 | 设备注册-吊销-重放全流程 | 签发凭证→注册→复用→库内吊销→重放 | 签发 200（明文一次性）→注册 200→复用 401→`UPDATE 1` 吊销→重放 401 | ✅ | `[PASS] 11c 401`；`[PASS] 11f 401（UPDATE 1）` |
| 11 | devices/me 单/双人间 | 单人间(TAB-01)与双人间(TAB-02)设备分别调用 | 单人间 `mode=single` 锁定 elder（王验收 bed1）；双人间 `mode=double` 候选 2 人 | ✅ | `{"mode":"single","elder":{...,"name":"王验收",...},"candidates":[]}`；`候选数=2` |
| 12 | 床位冲突 | 同床位再建长者 / bed_no 超容量 | 占用冲突 409；超容 422 | ✅ | `[PASS] 409`；`[PASS] 422` |
| 13 | pytest 连 kangban_test | `python -m pytest`（DATABASE_URL_TEST，事务回滚隔离） | 52 passed 全绿；写操作均有回查 DB 断言；未写 kangban_dev | ✅ | `52 passed, 6 warnings in 15.61s` |
| 14 | 3 条攻击用例 | `python -m pytest tests/security/test_day2_attacks.py -s` | A1 伪造/篡改 JWT、A2 吊销/过期重放、A3 跨楼栋 404 无泄露，共 4 用例全拦截；结构化告警留痕 | ✅ | `4 passed`；`{"event":"auth_failed","result":"invalid_or_expired_token"}` 等告警带 trace_id |
| 15 | POC-05 回填 | 查看 `docs/POC_验证计划.md` | 结果记录栏已回填执行日期/实测数据/结论/证据；总览表状态"Day2 部分通过（Day9 复测待做）" | ✅ | 拦截率 4/4=100%；thread_id 越权纳入 Day9 复测 |
| 16 | Git 小步提交与红线 | `git log` / `git status` / `git remote` | Day2 共 6 次本地提交；`.env` 被 ignore 未跟踪；6 个提交未推送（未 push） | ✅ | `978ff36→42ddff8` 六连提交；`git check-ignore .env` 命中；`unpushed commits: 6` |

## 附加验证

- **密钥缺失 fail-fast**：`.env` 的 `JWT_SECRET_KEY` 留空（按红线由用户自行生成填入）时，服务启动正常但签发 Token 处抛出 `RuntimeError: JWT_SECRET_KEY 未配置：请在 .env 中生成并填入后重启服务`，未产生不安全降级。
- **审计留痕全链路**：验收实测期间服务端结构化 JSON 日志完整覆盖 `login_succeeded/login_failed`、`auth_failed`、`auth_forbidden`、`elder_created`、`assignment_created`、`device_register_token_issued`、`device_registered`，均含 `trace_id`，写操作含 `actor_id/actor_role/object_type/object_id/result`。
- **实测数据清理**：验收所用 HTTP 实测数据（房间/设备/账号/长者/凭证/关联）已全部删除并逐表核验归零；kangban_dev 仅保留 seed_admin 创建的 `admin` 账号。验收用临时密钥仅经环境变量注入进程，未写入任何文件。

## 遗留事项（不阻塞验收）

1. `JWT_SECRET_KEY` 由用户自行生成并填入 `.env`（`python -c "import secrets; print(secrets.token_urlsafe(48))"`）。
2. 文档口径偏差：Day2 文档"新增 6 表/17 张"与实际"5 表/16 业务表"，建议在 Day3 开工前修订 Day2 文档对应表述（待用户确认后处理）。
3. POC-05 Day9 发布前复测待做（含 thread_id 越权场景）。
4. push 待用户单独授权（当前 6 个本地提交未推送）。
