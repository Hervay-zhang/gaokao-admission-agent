# 高考升学志愿填报 AI 助手

> 输入高考分数 / 位次，基于**近三年真实投档数据**给出「冲、稳、保」三档院校志愿方案，并解答志愿填报政策——**全部答案带数据依据和引用来源，不编造、不承诺**。

> 公开版包含应用源码、导入脚本及数据来源索引。运行完整推荐功能前，按 `docs/data_caliber.md` 准备当地招生数据。
> 当前进度：**T01–T31 已完成**，并已增补社区、用户主页、演示登录、新版用户中心与底部快捷链接。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python **3.12.8** + FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2（uv 管理，ruff line-length 120） |
| 数据库 | **MySQL 8**（utf8mb4 / InnoDB） |
| 缓存 | **Redis 7**（缓存 / 会话热态 / 限流 / SSE 缓冲） |
| 大模型 | **deepseek-v4-flash**（OpenAI 兼容；`LLM_MODELS` 多模型降级链，防单模型停服） |
| 前端 | 原生 **HTML / CSS / JS + ES Modules**（无框架、零构建、移动优先，≥1024px 兼容 PC） |
| 流式 | **SSE**（打字机效果 + 断点续写 + 降级「专家规则模式」） |
| RAG | MySQL `FULLTEXT(ngram)` + jieba + 向量 JSON 余弦重排（**不引向量库**） |
| 容器 | Dockerfile + docker-compose.yml 一键拉起（api + mysql + redis + nginx web） |

---

## 快速开始

### 1. 准备配置

```bash
cp .env.example .env      # 按本机情况修改（数据库密码 / LLM key）
```

### 2. 启动基础设施

```bash
docker compose up -d mysql redis      # 推荐：仅基础设施
docker compose ps                     # 期望 mysql / redis 均 Up (healthy)
```

> 本机已装 MySQL80（占用 3306）时，**可复用本机 MySQL**，只起 Redis 容器；
> 若走容器 MySQL，`docker-compose.yml` 已把映射改为 `3307:3306` 避让。
> Redis 映射 `6380`（避开 6379 潜在冲突）。

### 3. 后端

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
# 健康检查：http://127.0.0.1:8000/health
# 模型链状态：http://127.0.0.1:8000/api/health/llm
```

### 4. 前端（原生，无需构建）

```bash
cd frontend
python -m http.server 8080
# 打开 http://127.0.0.1:8080
```

### 5. 一键全量（含 api）

```bash
docker compose up -d --wait
```

| 服务 | 端口 |
|---|---|
| 前端 | `8080` |
| 后端 API | `8000` |
| Redis | `6380` |
| MySQL | 本机 `3306` / 容器 `3307` |

### 演示登录账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `student` | `student123` | 考生 |
| `parent` | `parent123` | 家长 |
| `admin` | `admin123` | 管理员 |

> 登录态用于前端原型角色演示。`MOCK_MODE=true` 时管理员演示账号可直接查看后台；
> 生产模式（`MOCK_MODE=false`）仍必须使用后端 `ADMIN_TOKEN` / `X-Admin-Token` 鉴权。

---

## 目录结构

```
升学志愿助手/
├── backend/     FastAPI 后端（app/api · core · db · services · rag · agent）
├── frontend/    原生前端（css · js/components · js/pages，ES Modules）
├── data/        数据资产（raw 留档 / cleaned 清洗 / eval 评测）
├── docker/      容器化辅助（mysql init）
└── docs/        PRD · 任务卡 · 数据口径 · 目录树 · 开发约定
```

详见 `docs/升学志愿助手-项目目录树.md`。

---

## 测试与检查

```bash
cd backend
uv run pytest          # 冒烟测试（T01 对应 J1–J8）
uv run ruff check .    # 代码规范检查
```

---

## 开发约定（要点）

- **一张任务卡 = 一次会话 = 一个 commit**，提交格式 `Txx: 描述`；
- 业务代码**禁止硬编码模型名**，一律注入 `LLMClient`（多模型 failover）；
- **RAG 必须且只能基于检索到的 Context** 生成，无依据走兜底；
- 所有 AI 输出**必带免责声明**，**禁止输出录取概率数字**；
- 敏感字段（分数 / 位次）脱敏落库（`score_hash` + `score_bucket`），**日志禁明文**；
- `.env` 全面接管配置，**绝不入库**。

完整规范见 `docs/CONVENTIONS.md`。

---

## 当前进度

- [x] **T01** 工程初始化与开发约定（含 LLM 多模型底座）
- [x] **P0** 地基与数据口径：T02–T06
- [x] **P1** 推荐引擎与时效：T07–T11
- [x] **P2** 政策 RAG：T12–T15
- [x] **P3** Agent 与流式对话：T16–T22
- [x] **P4** 产品闭环与 UI：T23–T28
- [x] **P5** 工程化与交付：T29–T31
- [x] **增补** 社区、帖子详情、用户主页、案例收藏与快捷链接管理

---

*所有推荐仅供参考，不作为最终填报承诺。*

## CI 配置

`ci/github-actions.example.yml` 保留了 GitHub Actions 配置模板。
