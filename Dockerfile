# 康伴 3D 数字人 backend 镜像（Day10 步骤 1 · 多阶段构建）
# 为什么多阶段：①依赖层只随 pyproject/uv.lock 变化重建（uv.lock 锁定，日常改代码命中缓存）；
#              ②运行层不带 uv 缓存/pip 残留/dev 依赖（pytest、ruff 不进镜像），体积小、攻击面小。
# 为什么 python:3.12-slim：项目 requires-python>=3.12；slim 在完整度与体积间取平衡（aiortc/asyncpg 均有预编译 wheel，无需编译工具链）。

# ---------- 阶段一：依赖层（uv 安装锁定依赖） ----------
FROM python:3.12-slim AS deps

# uv 官方静态二进制（不污染 Python 环境）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先只复制依赖清单：这两个文件不变时，后续改代码不会击穿本层缓存
COPY pyproject.toml uv.lock ./

# --frozen：严格按 uv.lock 安装，不重新解析版本；--no-dev：不装 pytest/ruff（dev 组）
RUN uv sync --frozen --no-dev --no-install-project

# ---------- 阶段二：运行层（应用代码 + 非 root 运行） ----------
FROM python:3.12-slim

WORKDIR /app

# 非 root 用户（最小权限原则）
RUN groupadd -r kangban && useradd -r -g kangban kangban

# 从依赖层取虚拟环境
COPY --from=deps /app/.venv /app/.venv

# 应用代码 + MCP servers（backend 启动时 stdio 拉起子进程，与主进程同容器）+ 迁移脚本
COPY app/ ./app/
COPY mcp-servers/ ./mcp-servers/
COPY alembic/ ./alembic/
COPY alembic.ini ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER kangban

EXPOSE 8000

# 默认命令（compose 里会被 command 覆盖为先迁移再启动）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
