# prompts/ · 提示词库 v2（Day10 步骤 9）

> 从 Day1~Day10 执行提示词提炼的可复用模板归档。规划双件套的完整流程已固化为项目 Skill
> `.trae/skills/new-day-plan/`（含两份模板 assets），本库存放 Skill 之外的独立提示词模板。

## 索引（按用途）

| 文件 | 用途 | 来源沉淀 |
| --- | --- | --- |
| [day-review.md](day-review.md) | 每日复盘/验收报告生成提示词骨架 | Day6~Day9 验收报告惯例 |
| [incident-report.md](incident-report.md) | 基础设施/线上事故复盘提示词（时间线→根因→SOP→教训） | Day10 Docker 存储损坏事故 |
| [doc-diff-audit.md](doc-diff-audit.md) | 「只增不改」文档增补的自检提示词 | Day10 README/SECURITY/AGENTS 增补 |

## 使用约定

1. 模板中 `<>` 为占位符，使用时替换；正文口径以《Day_plan/DayN_主题.md》为准；
2. 数据诚实原则内置于所有模板：**未实现写 N/A、达不到写差距、估算标注估算口径**；
3. 新模板入库必须注明来源沉淀（哪一天的什么教训），无来源不入库。
