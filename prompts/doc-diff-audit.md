# doc-diff-audit · 「只增不改」文档增补自检提示词

> 来源沉淀：Day10 README/SECURITY.md/AGENTS.md 增补（AI_USAGE.md 协作边界第 4 节）。

```
对以下文档执行「只增不改」增补：<文件列表>（README / AGENTS.md / SECURITY.md / .env.example 等）。

规则：
1. 只允许追加内容，禁止修改/删除任何既有行；AGENTS.md 既有规则一字不动；
2. 完成后必须运行 git diff --numstat <文件> 并在汇报中给出「N 增 0 删」实证；
3. 增补内容的三条硬约束：
   a. 数字必须实测取数（eval 报告/SQL/命令输出），估算口径显式标注；
   b. 引用的文件路径与命令先验证存在与可执行；
   c. 新增口径与既有章节冲突时停下提问，不得静默覆盖旧口径；
4. AGENTS.md 增补限定为「踩坑条目」：现象→根因→对策三段式，每条注明来源 Day。
```
