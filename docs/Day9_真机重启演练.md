# Day9 步 1 · 真机重启演练记录（POC-06 真机口径收口）

> 执行时间：2026-11-12 09:03–09:13（Asia/Shanghai）　|　基线 `604192e`　|　alembic head `0018`（零迁移）
> 演练口径：真机 uvicorn（127.0.0.1:8000）+ 本地开发库 `kangban_dev`（15433）+ 真实账号登录（nurse1/doctor1）

## 演练流程与证据

| 步骤 | 操作 | 证据 |
| --- | --- | --- |
| ① 起服务 | `uvicorn app.main:app`（run1 日志留档） | `Application startup complete` |
| ② 护工登记 red | nurse1 登录 → `POST /api/v1/health-records?elder_id=演示长者`，`bp_systolic=185, bp_diastolic=95` | 返回 `triage_level=red`，reason「收缩压185≥180（sbp偏差大，建议人工复核）」，`record_id=885bcfad-69a0-471f-a52d-e26016755728` |
| ③ red 建单挂起 | 图 red_workorder → interrupt ② `doctor_confirm`（确认前零通知） | 工单 `5cf2514b-79b8-433e-8c83-f7920425e84f` status=open / priority=urgent；日志 `health_triaged`（trace f87b911b） |
| ④ **强杀进程** | `Stop-Process -Force`（PID 25952），端口 8000 关闭确认 | exit code -1（预期） |
| ⑤ 重启服务 | uvicorn 重启（run2 日志留档），checkpoint `data/health_checkpoints.sqlite` 持久化未丢 | 服务正常拉起，scheduler restarted |
| ⑥ get_state 恢复 | 服务层 `get_graph_state(thread_id)` | `level=red`，`__next__=('doctor_confirm',)` —— **挂起态跨重启恢复 ✅** |
| ⑦ 医生确认 resume | doctor1 登录 → `POST /api/v1/doctor/health-alerts/{workorder_id}/confirm` | 返回 status=acknowledged、pending_site=None（图走完） |
| ⑧ 副作用生效 | notifier 放行 + 医院 mock 转诊 | 日志：`health_default_notifier` → `hospital_referral_created` → `health_confirmed action=resolve`（trace f6752c30）；工单 description 追加「已确认转诊，转诊单号 **REF-2026-11-12-1C23EB2D**」 |

## 结论

- **挂起 → kill → 重启 → get_state → 医生确认 → Command(resume) → 副作用全链路通过**；
- 确认前零通知红线复验：③→④→⑤→⑥ 期间无任何 notifier 日志，副作用日志全部出现在 ⑦ 确认之后（时间戳 01:12:35 均晚于 confirm 请求）；
- POC-06 真机口径由 Day8「部分通过」升级为「**通过**」（见 POC_验证计划.md 回填）。

## 留档文件

- 演练脚本与原始日志：`%TEMP%\day9_drill\`（uvicorn_run1.log / uvicorn_run2.log / drill_a.py / drill_b.py，临时目录不入库）；
- 本记录为验收报告第 1 项证据引用源。
