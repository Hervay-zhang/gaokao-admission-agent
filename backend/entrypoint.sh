#!/bin/sh
# ============================================================
#  T29 · API 容器入口：迁移 →（可选）幂等导入 → 起服务
#  原则：
#   1. 迁移失败**必须响亮退出**（绝不 `|| true` 掩盖——表结构不对宁可不起服务）；
#   2. 数据导入是**可选**步骤（IMPORT_DATA=1 才执行，且要求卷里有 CSV）；
#   3. 导入失败也响亮退出（宁缺毋错，不把半导入状态当成功启动）；
#   4. 最终以 exec 交接给 uvicorn（让 uvicorn 成为 PID 1，信号可直达）。
# ============================================================
set -e

echo "[entrypoint] 等待数据库就绪后执行迁移…"
alembic upgrade head
echo "[entrypoint] 迁移完成（alembic current 见上）"

if [ "${IMPORT_DATA:-0}" = "1" ]; then
    echo "[entrypoint] IMPORT_DATA=1，执行幂等数据导入…"
    python scripts/import_data.py
    echo "[entrypoint] 数据导入完成"
else
    echo "[entrypoint] 跳过数据导入（IMPORT_DATA=${IMPORT_DATA:-0}，如需首灌置 1）"
fi

# RAG 切片 + FULLTEXT 索引（幂等：只补缺失切片；索引随迁移 0005 已建则跳过）。
# 必须步骤而非可选——否则政策问答（RAG 链路）在全新环境不可用。
echo "[entrypoint] 构建 RAG 切片索引（幂等）…"
python scripts/build_rag_index.py --only-missing
echo "[entrypoint] RAG 切片索引完成"

echo "[entrypoint] 启动 uvicorn…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
