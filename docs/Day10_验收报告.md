# Day10 验收报告（项目收官）

> 日期：2026-11-12　|　主文档：《Day_plan/Day10_3D康伴数字人.md》§9 十六项
> 收官 commit 链：52f4880(步1)→7d7d459(步3)→dddde6a(步2)→9a9d093/e83d1a9/674759f(步4-5)→7c9c866(步6)→5b7e3ad(步7)→a68f0eb(步8)→d73b329(步9)→本报告
> 基线：Day9 报告 702 例；本日实测收集 **723 例全绿**（exit 0）；alembic head **0018 零迁移**；tag `final`

## 结论预览

**16 项验收全部通过**（其中第 2/5/6 项含如实注记的口径边界，见对应行）。
Day10 期间遭遇一次真实基础设施事故（宿主机磁盘满→Docker 存储损坏），按 SOP 恢复且
演示数据零丢失——事故复盘见 `docs/Day10_回滚演练.md` §4，反而成为回滚通道的最佳实证。

## 逐项验收

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | compose 三服务 healthy + 接管现有卷数据零丢失 | ✅ | pg(15433)/backend(8600)/funasr(profile core) 全 healthy；卷迁移（匿名卷→`kangban_pg_data` 命名卷）pg_dump 双保险，对账 29 表 elders=6/workorders=38/conversations=63/health_records=79 零丢失；事故后二次对账一致 |
| 2 | 容器内静态页/登录/WS/用药图/健康 HITL 实测 | ✅注记 | 浏览器 E2E：登录卡房间网格→指纹演示直进→通话态全过（截图留档）；WS 文字通道真实对话 4 轮实测（报时/点播隔离/天气工具/主控开放域，见第 6 项）；**语音 WS 双向音频需真实麦克风，浏览器未实测——经 723 例测试与 Day3/5 验收覆盖，用药图/健康 HITL 经 Day7/8 验收与 723 例覆盖，如实注记** |
| 3 | 干净重建冒烟 + alembic 自动迁移 + 必填项清单 | ✅ | 步骤 2 干净重建冒烟通过；事故后全量 `--no-cache` 重建再验证；README「Docker 快速开始」含必填项清单（POSTGRES_PASSWORD/JWT_SECRET_KEY/LLM_*） |
| 4 | /metrics 格式正确且与 decision_logs 可复算一致 | ✅ | 8 例测试；对账 299=173(rule)+118(agent)+8(safety)，与直查 SQL 一致；`METRICS_ENABLED=false`→404 有测试 |
| 5 | trace_lookup 1 分钟串全链 | ✅注记 | 真实对话 4 轮串全链成功（终端摘要见第 6 项）；**设计注记：thread_id 主键在语音链路不成立**（decision_logs.thread_id 仅用药/健康图填写，语音轮次为 NULL）——实测发现后新增 `--latest` 时间窗回退（±300s 对齐，零表改动，+2 例测试共 12 例） |
| 6 | HTML 报告完整展示真实对话全链 | ✅ | `eval/reports/trace/trace_time-win_20261112_170159.html`（4.1KB 自包含单文件，离线可开）：4 层决策（rule×3 含 tools=1 + agent）+ 8 条消息 + 分层耗时条；对话经生物识别登录→WS 文字通道真实产生 |
| 7 | README 成本表 | ✅ | S1 闭环率 68.75%（≥60% 目标达成）/Tool 全部 ≤1/记忆省 token 估算口径显式标注/prompt cache N/A 如实注记；`git diff --numstat` 54 增 0 删 |
| 8 | 回滚演练留档（双通道） | ✅ | `docs/Day10_回滚演练.md`：代码回滚 500→200 实录；alembic 0018→0017→0018 双向可逆（**测试库口径**——dev 库 73 行睡眠数据风险评估后用户裁决）；真实事故 SOP |
| 9 | pre-push hook dry-run 触发 gate | ✅ | `.git/hooks/pre-push` 调 eval/gate.py；`git push --dry-run` 全 PASS exit 0（显示 52f4880..7c9c866 未真推——不 push 红线不变） |
| 10 | 部署门禁六项清单成文 | ✅ | SECURITY.md 附录（15 增 0 删实证）：验收达成/测试通过/密钥安全/迁移可逆/监控就绪/回滚路径 |
| 11 | 演示脚本四段分镜 | ✅ | `Day_plan/Day10_演示脚本与分镜.md`：5 分钟四段三列表（用药外呼/陪聊点播/健康 HITL/家属日报）+ 前置清单 + 拍摄注意；**注记：Day_plan 目录被 .gitignore 忽略且从未入库（早期 a667830 决策），用户裁决保持不入库，文档在工作区交付** |
| 12 | INTERVIEW_QA 20 问 + 四反例 | ✅ | `INTERVIEW_QA.md`：四维 20 问全部对应真实代码与实测数据 + 简历四反例对照表 |
| 13 | 三份设计文档成稿 | ✅ | HARNESS_DESIGN.md（分层决策/ReAct/Guard/降级链，汇总 ADR-003~026）/ IDENTITY_RULES.md（人设/话术红线/四级级联/动作语义）/ AI_USAGE.md（双件套模式/决策点提问/数据诚实） |
| 14 | 复盘四件套（AGENTS.md 人审通过） | ✅ | ①AGENTS.md 踩坑 4 条（14 增 0 删，人审通过）②Skill `new-day-plan`（.trae/skills/，含双模板 assets，不入库）③prompts/ v2（索引+3 模板）④`docs/Day10_复盘报告.md` 十大陷阱逐条对照 |
| 15 | 723 例零回归、ruff 零新增、head 0018 | ✅注记 | `pytest -q` exit 0 全绿（723 = 702 基线 + 8 metrics + 12 trace_lookup + 1 例历史口径尾差，如实注记）；ruff 0.17.3：Day10 新增/改动文件零违例，全仓 73 处存量违例与 HEAD 基线完全一致（stash 对比实证，非本日引入）；alembic head 0018 零迁移 |
| 16 | tag `final` + 验收报告 + 声纹注记收官 | ✅ | 本报告 + tag `final`（不 push）；声纹维持 0.55 基线 + 合成口径零误识区注记，真人复调列「后续可选」（D3） |

## 收官口径

- **实测优于承诺**：S1 闭环率 68.75% 达成 ≥60% 目标（大纲观察项 0.50）；敏感召回/幻觉拦截/任务成功率 100%；
- **诚实注记清单**：语音 WS 浏览器未实测（测试覆盖）/thread_id 主键口径修正（时间窗回退）/
  ruff 73 处存量（非本日引入）/Day_plan 不入库（用户裁决）/记忆省 token 为估算口径/prompt cache N/A/
  EdgeTTS 首包 ~1.3s 未达标留 TTS 选型（POC-08）；
- **真实事故收益**：磁盘满→Docker 存储损坏的完整恢复 SOP 与数据零丢失对账，回滚三通道全部实证。

## 「后续可选」清单（收官不阻塞）

声纹真人复调（D3 注记收官）／HTTPS 自签证书／真 CI 服务器／幻觉 agent 层在线验证／
真实 API token 计量／语音 WS 真机双向音频实测。
