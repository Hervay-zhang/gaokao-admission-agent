#!/usr/bin/env bash
# ============================================================
#  本机打包脚本（Windows 下用 Git Bash 执行）
#  产出：项目上级目录的 gaokao-deploy.tgz，用 MobaXterm 拖到服务器 /opt/
#
#  用法：
#    bash deploy/pack.sh            # 只打包（前端用现有 dist）
#    bash deploy/pack.sh --build    # 先 npm run build 再打包（改过前端必须加）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
OUT="${ROOT}/../gaokao-deploy.tgz"

if [[ "${1:-}" == "--build" ]]; then
  echo "[1/2] 构建前端产物…"
  (cd frontend && npm run build)
else
  echo "[1/2] 跳过前端构建（如需重新构建：bash deploy/pack.sh --build）"
fi

echo "[2/2] 打包到 ${OUT} …"
tar -czf "${OUT}" \
  --exclude='backend/.venv' \
  --exclude='backend/tests' \
  --exclude='backend/.pytest_cache' \
  --exclude='backend/.ruff_cache' \
  --exclude='backend/*.log' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/_nm_to_delete' \
  --exclude='frontend/js' \
  --exclude='frontend/css' \
  --exclude='frontend/vendor' \
  --exclude='frontend/scripts' \
  --exclude='data/cleaned/*.bak*' \
  --exclude='.git' \
  --exclude='.workbuddy' \
  backend docker deploy frontend/dist data/cleaned data/cases \
  docker-compose.prod.yml .env.prod.example

ls -lh "${OUT}"
echo
echo "完成。下一步：用 MobaXterm 把 gaokao-deploy.tgz 上传到服务器 /opt/ 目录"
echo "预期大小 10–20MB；若大于 100MB，说明 node_modules/.venv 没排除干净"
