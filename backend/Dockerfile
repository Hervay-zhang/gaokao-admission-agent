# ============================================================
#  后端镜像：Python 3.12.8-slim，非 root 运行
#  多阶段：builder 装依赖 → runtime 只带必要文件
# ============================================================
FROM python:3.12.8-slim@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS builder

# uv 官方镜像（用于安装依赖）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 先只复制依赖清单，利用构建缓存
# 注意：必须带 --extra rag（jieba）——T30 E2E 曾发现容器缺 jieba 导致 RAG 问答全部 STREAM_ERROR
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project --extra rag

# ---------------- runtime ----------------
FROM python:3.12.8-slim@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS runtime

# 时区 + 中文支持
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN useradd -m -u 1000 appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=appuser:appuser . /app

# 入口脚本可执行（迁移 → 可选导入 → uvicorn；失败响亮退出）
RUN chmod +x /app/entrypoint.sh

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 容器启动：迁移（失败即退出，绝不静默跳过）→ 可选导入 → uvicorn
ENTRYPOINT ["/app/entrypoint.sh"]
