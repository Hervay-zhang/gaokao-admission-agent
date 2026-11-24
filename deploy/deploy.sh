#!/usr/bin/env bash
# ============================================================
#  服务器端部署脚本（在 /opt/gaokao 目录执行）
#  用法：
#    cd /opt/gaokao && bash deploy/deploy.sh
#  流程：环境自检 → 起 mysql/redis → 构建并起 api（首灌数据）→ 起 web → 自检
#  注意：首次运行前必须先 cp .env.prod.example .env 并填好密钥/密码
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say() { echo; echo "==> $*"; }

# ---------- 0. 环境自检 ----------
say "0/5 环境自检"
command -v docker >/dev/null || { echo "✗ 未安装 docker"; exit 1; }
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose -f docker-compose.prod.yml"
elif command -v docker-compose >/dev/null; then
  COMPOSE="docker-compose -f docker-compose.prod.yml"
else
  echo "✗ 未安装 docker compose"; exit 1
fi
echo "  使用：$COMPOSE"

[[ -f .env ]] || { echo "✗ 缺少 .env：请先 cp .env.prod.example .env 并修改密钥/密码"; exit 1; }
[[ -f frontend/dist/index.html ]] || { echo "✗ 缺少 frontend/dist/index.html（前端未构建或未上传）"; exit 1; }
[[ -d data/cleaned ]] && ls data/cleaned/*.csv >/dev/null 2>&1 || { echo "✗ 缺少 data/cleaned/*.csv（首灌数据需要）"; exit 1; }

PORT_CHECK=':8081 |:3307 '
if command -v ss >/dev/null; then
  BUSY=$(ss -lnt | grep -E ':8081 |:3307 ' || true)
else
  BUSY=$(netstat -lnt 2>/dev/null | grep -E ':8081 |:3307 ' || true)
fi
if [[ -n "$BUSY" ]]; then
  echo "✗ 端口被占用（8081/3307 必须空闲）："; echo "$BUSY"; exit 1
fi
echo "  ✓ 端口 8081 / 3307 空闲"

# 内存体检（2G 小内存服务器极易在构建/灌数阶段 OOM）
AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
echo "  可用内存：${AVAIL_MB}MB ｜ Swap：${SWAP_MB}MB"
if (( AVAIL_MB < 1200 && SWAP_MB < 1024 )); then
  echo "  ⚠️ 内存偏紧且无 Swap，建议先加 2GB Swap（否则导入数据/RAG 切片时可能 OOM）："
  echo "     dd if=/dev/zero of=/swapfile bs=1M count=2048 && chmod 600 /swapfile"
  echo "     mkswap /swapfile && swapon /swapfile"
  echo "     echo '/swapfile swap swap defaults 0 0' >> /etc/fstab"
  read -r -p " 是否仍继续？(y/N) " GO
  [[ "${GO:-N}" == "y" || "${GO:-N}" == "Y" ]] || { echo "已中止"; exit 1; }
fi

# ---------- 1. 基础设施 ----------
say "1/5 启动 MySQL + Redis"
$COMPOSE up -d mysql redis

wait_healthy() {  # $1=容器名  $2=超时秒
  local name=$1 timeout=${2:-120} i=0 st
  while (( i < timeout )); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)
    [[ "$st" == "healthy" ]] && { echo "  ✓ $name healthy"; return 0; }
    [[ "$st" == "exited" || "$st" == "dead" ]] && { echo "✗ $name 退出"; docker logs --tail 40 "$name"; return 1; }
    sleep 5; i=$((i+5))
  done
  echo "✗ $name 超时未 healthy"; docker logs --tail 40 "$name"; return 1
}
wait_healthy gaokao_mysql 150
wait_healthy gaokao_redis 60

# ---------- 2. 构建并启动 API（首灌数据）----------
say "2/5 构建并启动 API（首次会灌数据，约 1–3 分钟）"
$COMPOSE up -d --build api

# ---------- 3. 等待 API 就绪 ----------
say "3/5 等待 API 进入 healthy（最多 10 分钟）"
if ! wait_healthy gaokao_api 600; then
  echo; echo "排查：docker compose -f docker-compose.prod.yml logs --tail 100 api"
  exit 1
fi

# 首灌完成后关掉导入开关，避免每次重启重复导入
if grep -q '^IMPORT_DATA=1' .env; then
  sed -i 's/^IMPORT_DATA=1/IMPORT_DATA=0/' .env
  echo "  ✓ IMPORT_DATA 已改回 0"
fi

# ---------- 4. 启动前端 ----------
say "4/5 启动前端 nginx"
$COMPOSE up -d web
for i in $(seq 1 30); do
  if curl -fsS -m 3 http://127.0.0.1:8081/ >/dev/null 2>&1; then
    echo "  ✓ gaokao_web ready"
    break
  fi
  sleep 2
done

# ---------- 5. 自检 ----------
say "5/5 自检"
$COMPOSE ps
echo
echo "-- /health --"
curl -sS -m 10 http://127.0.0.1:8081/health || echo "✗ 健康检查失败"
echo
echo "-- 数据量核对（期望 3503 / 15868 / 1095 / 157 / >0）--"
source .env
docker compose -f docker-compose.prod.yml exec -T mysql \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -e \
  "SELECT (SELECT COUNT(*) FROM gaokao.score_segments),(SELECT COUNT(*) FROM gaokao.admission_lines),(SELECT COUNT(*) FROM gaokao.universities),(SELECT COUNT(*) FROM gaokao.policy_docs),(SELECT COUNT(*) FROM gaokao.policy_chunks);" \
  || echo "（数据量核对失败，手动检查）"
echo
echo "部署完成。请在本机浏览器打开：http://39.107.44.42:8081"
echo "（若打不开，先确认阿里云防火墙已放行 TCP:8081）"
