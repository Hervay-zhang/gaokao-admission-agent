#!/usr/bin/env bash
# ============================================================
#  服务器端：由 .env.prod.example 生成生产 .env
#  密钥/密码通过环境变量传入（脚本本身不含任何敏感值，可安全入库）
#
#  用法：
#    export GAOKAO_MYSQL_PWD='MySQL密码'
#    export GAOKAO_LLM_KEY='sk-xxx'      # 可留空，全部模型不可用时会走专家规则模式
#    bash deploy/init_env.sh
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GAOKAO_MYSQL_PWD:?未设置 GAOKAO_MYSQL_PWD}"
GAOKAO_LLM_KEY="${GAOKAO_LLM_KEY:-}"

[[ -f .env.prod.example ]] || { echo "✗ 缺少 .env.prod.example"; exit 1; }
cp -n .env.prod.example .env

SALT=$(openssl rand -hex 32)
AUTH=$(openssl rand -hex 32)
ADM=$(openssl rand -hex 16)
DBURL="mysql+pymysql://root:${GAOKAO_MYSQL_PWD}@mysql:3306/gaokao?charset=utf8mb4"

sed -i \
  -e "s|^MYSQL_ROOT_PASSWORD=.*|MYSQL_ROOT_PASSWORD=${GAOKAO_MYSQL_PWD}|" \
  -e "s|^DATABASE_URL=.*|DATABASE_URL=${DBURL}|" \
  -e "s|^MOCK_MODE=.*|MOCK_MODE=false|" \
  -e "s|^IMPORT_DATA=.*|IMPORT_DATA=1|" \
  -e "s|^LLM_API_KEYS=.*|LLM_API_KEYS=${GAOKAO_LLM_KEY},,|" \
  -e "s|^SCORE_HASH_SALT=.*|SCORE_HASH_SALT=${SALT}|" \
  -e "s|^AUTH_SECRET=.*|AUTH_SECRET=${AUTH}|" \
  -e "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=${ADM}|" \
  -e "s|^RATE_LIMIT_WHITELIST_IPS=.*|RATE_LIMIT_WHITELIST_IPS=|" \
  .env

chmod 600 .env
echo "==> .env 关键项（密钥已隐藏）"
grep -E "^(MYSQL_ROOT_PASSWORD|MOCK_MODE|IMPORT_DATA|DATABASE_URL|LLM_MODEL|CORS_ORIGINS)=" .env
echo "  SCORE_HASH_SALT / AUTH_SECRET / ADMIN_TOKEN = 已生成 32/32/16 字节随机值"
echo "  LLM_API_KEYS = $(grep -c '^LLM_API_KEYS=sk-' .env) 个已配置 Key"
