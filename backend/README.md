# 后端说明（gaokao-backend）

FastAPI 后端：志愿推荐（冲稳保）+ 政策问答（RAG）。

## 快速开始

```bash
cp ../.env.example ../.env      # 首次
uv sync                          # 安装依赖

# 结构同步（数据库就绪后，T04 起有效）
uv run alembic upgrade head

# 启动
uv run uvicorn app.main:app --reload
# http://127.0.0.1:8000/docs
```

## 健康检查

| 端点 | 说明 |
|---|---|
| `GET /health` | 存活探针 + 配置快照（脱敏） |
| `GET /api/health/llm` | LLM 模型降级链与各模型状态 |
| `GET /api/health/db` | 数据库连通性（SELECT 1） |
| `GET /api/health/redis` | Redis 连通性（PING） |

## 双模式

- `MOCK_MODE=true`（默认）：LLM 走确定性 mock，**无需 API key** 即可跑通全链路（CI / 演示）；
- `MOCK_MODE=false`：走真实模型，按 `LLM_MODELS` 降级链自动 failover。

## 环境变量（要点）

详见项目根 `.env.example`。关键项：

- `DATABASE_URL`：MySQL 8（本机 `127.0.0.1:3306` 或容器 `mysql:3306`）
- `REDIS_URL`：Redis 7（本机开发 `127.0.0.1:6380`）
- `LLM_MODEL` / `LLM_MODELS`：主模型 + 降级链（防单模型停服）
- `CURRENT_YEAR` / `REF_YEARS` / `YEAR_WEIGHTS`：年份维度与三年加权口径

## 代码规范与检查

```bash
uv run ruff check .     # 必须 All checks passed!
uv run pytest           # 冒烟测试（T01 对应 J1–J8）
```

## 分层约定

见 `../docs/CONVENTIONS.md`。要点：`api → services → (db / rag / core)` 单向依赖；
业务代码禁止硬编码模型名，一律注入 `LLMClient`。

## 目录

```
app/
├── api/         路由层（校验 / 限流 / 取会话 → 调 service）
├── core/        基础设施（config / logging / exceptions / redis / llm）
├── db/          引擎 / Session / ORM 模型
├── schemas/     Pydantic 出入参
├── services/    业务层（口径唯一实现）
├── rag/         政策检索与问答
└── agent/       意图路由 / Guardrails / Prompt / SSE
```
