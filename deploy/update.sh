#!/usr/bin/env bash
# ============================================================
#  分类更新线上服务（本机 Git Bash 执行）
#
#  为什么分类型：全量重传 + 重建镜像要 5~15 分钟，改一行样式没必要。
#  按改动对象选最小动作，几十秒就能生效。
#
#  用法：
#    export GAOKAO_SSH_PASSWORD='服务器root密码'
#    bash deploy/update.sh web      # 前端（页面/样式/交互）→ 本地 build → 传 dist，秒级生效
#    bash deploy/update.sh api      # 后端（接口/算法/提示词）→ 传 backend → 重建镜像并重启
#    bash deploy/update.sh conf     # nginx 配置 → 传配置 → 校验并 reload
#    bash deploy/update.sh data     # 数据 CSV → 传 data/cleaned（导入动作需手动确认）
#    bash deploy/update.sh all      # 全量：重新打包 → 上传 → 重建全部
#
#  可选参数：
#    --no-build   更新前端时跳过 npm run build（已手动 build 过）
#    --recreate   web 更新后强制重建容器（默认保留目录 inode，一般不需要）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

HOST="${GAOKAO_HOST:-39.107.44.42}"
USER="${GAOKAO_USER:-root}"
PORT="${GAOKAO_PORT:-22}"
REMOTE="/opt/gaokao"

TYPE="${1:-}"
shift || true
DO_BUILD=1
DO_RECREATE=0
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --recreate) DO_RECREATE=1 ;;
  esac
done

[[ -n "${GAOKAO_SSH_PASSWORD:-}" ]] || { echo "✗ 未设置 GAOKAO_SSH_PASSWORD"; exit 1; }

ASKPASS="$(mktemp /tmp/gaokao_askpass.XXXXXX.sh)"
printf '#!/bin/sh\necho "$GAOKAO_SSH_PASSWORD"\n' > "$ASKPASS"
chmod 700 "$ASKPASS"
trap 'rm -f "$ASKPASS"' EXIT
export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o NumberOfPasswordPrompts=1 -P ${PORT}"

# --- 小工具 -------------------------------------------------
log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
TMP="/tmp/gaokao-update.tgz"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

# --- 前端 ---------------------------------------------------
update_web() {
  if [[ $DO_BUILD -eq 1 ]]; then
    log "本地构建前端"
    (cd "${ROOT}/frontend" && npm run build 2>&1 | tail -3)
  fi
  [[ -d "${ROOT}/frontend/dist" ]] || { echo "✗ 没有 frontend/dist，先 build"; exit 1; }

  log "打包并上传 dist"
  rm -f "$TMP"
  tar -czf "$TMP" -C "${ROOT}/frontend" dist
  scp $SSH_OPTS "$TMP" "${USER}@${HOST}:/tmp/gaokao-web.tgz"

  # 关键：不要删 dist 目录本身（bind mount 绑 inode，删了容器仍指向旧目录）
  log "远端替换（保留目录 inode）"
  if [[ $DO_RECREATE -eq 1 ]]; then
    ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE}/frontend && rm -rf dist/* && tar -xzf /tmp/gaokao-web.tgz \
      && cd ${REMOTE} && docker compose -f docker-compose.prod.yml up -d --force-recreate web 2>&1 | tail -2"
  else
    ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE}/frontend && rm -rf dist/* && tar -xzf /tmp/gaokao-web.tgz"
  fi
  sleep 2

  log "校验线上产物"
  REMOTE_JS=$(ssh $SSH_OPTS "${USER}@${HOST}" "curl -s http://127.0.0.1:8081/ | grep -oE '/assets/index-[A-Za-z0-9_-]+\.js' | head -1")
  LOCAL_JS=$(grep -oE '/assets/index-[A-Za-z0-9_-]+\.js' "${ROOT}/frontend/dist/index.html" | head -1)
  echo "  本地：${LOCAL_JS}"
  echo "  线上：${REMOTE_JS}"
  [[ -n "$REMOTE_JS" && "$REMOTE_JS" == "$LOCAL_JS" ]] && echo "  ✓ 线上已是最新（刷新页面请 Ctrl+F5）" \
    || echo "  ✗ 不一致，可加 --recreate 重试"
}

# --- 后端 ---------------------------------------------------
update_api() {
  log "打包并上传 backend"
  rm -f "$TMP"
  tar -czf "$TMP" -C "${ROOT}" \
    --exclude='backend/__pycache__' \
    --exclude='backend/.venv' \
    --exclude='backend/.pytest_cache' \
    --exclude='backend/**/__pycache__' \
    --exclude='backend/htmlcov' \
    --exclude='backend/.coverage' \
    backend
  scp $SSH_OPTS "$TMP" "${USER}@${HOST}:/tmp/gaokao-api.tgz"

  log "远端覆盖代码（先备份上一版）"
  ssh $SSH_OPTS "${USER}@${HOST}" "
    cd ${REMOTE}
    rm -rf /opt/gaokao-backup-api && mkdir -p /opt/gaokao-backup-api
    cp -r backend/app backend/scripts backend/migrations /opt/gaokao-backup-api/ 2>/dev/null || true
    rm -rf backend/app backend/scripts backend/migrations
    tar -xzf /tmp/gaokao-api.tgz
    ls backend
  "

  log "重建镜像并重启 api（依赖层有缓存，通常 1~3 分钟）"
  ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE} && docker compose -f docker-compose.prod.yml build api 2>&1 | tail -5 \
    && docker compose -f docker-compose.prod.yml up -d api 2>&1 | tail -3"

  log "等待 api 健康（最多 5 分钟）"
  ssh $SSH_OPTS "${USER}@${HOST}" "for i in \$(seq 1 60); do
      S=\$(docker inspect -f '{{.State.Health.Status}}' gaokao_api 2>/dev/null)
      ST=\$(docker inspect -f '{{.State.Status}}' gaokao_api 2>/dev/null)
      if [ \"\$S\" = 'healthy' ]; then echo \"  ✓ api healthy\"; break; fi
      if [ \"\$ST\" = 'exited' ]; then echo '  ✗ api 退出，日志：'; docker logs --tail 40 gaokao_api; exit 1; fi
      sleep 5
    done
    curl -s -m 8 http://127.0.0.1:8081/health; echo"
}

# --- nginx 配置 ---------------------------------------------
update_conf() {
  log "上传 nginx 配置并校验"
  scp $SSH_OPTS "${ROOT}/docker/nginx/prod.conf" "${USER}@${HOST}:${REMOTE}/docker/nginx/prod.conf"
  ssh $SSH_OPTS "${USER}@${HOST}" "docker exec gaokao_web nginx -t 2>&1 | tail -2 \
    && docker exec gaokao_web nginx -s reload && echo '  ✓ 已 reload' \
    && sleep 2 && curl -s -o /dev/null -w '  首页 HTTP %{http_code}\n' http://127.0.0.1:8081/"
}

# --- 数据 CSV -----------------------------------------------
update_data() {
  log "上传 data/cleaned"
  rm -f "$TMP"
  tar -czf "$TMP" -C "${ROOT}/data" cleaned
  scp $SSH_OPTS "$TMP" "${USER}@${HOST}:/tmp/gaokao-data.tgz"
  ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE}/data && rm -rf cleaned/* && tar -xzf /tmp/gaokao-data.tgz && wc -l cleaned/*.csv"
  log "数据已就位（尚未导入数据库）"
  echo "  如需重跑导入（会按唯一约束幂等 upsert）："
  echo "    ssh ${USER}@${HOST}"
  echo "    cd ${REMOTE} && sed -i 's/^IMPORT_DATA=0/IMPORT_DATA=1/' .env"
  echo "    docker compose -f docker-compose.prod.yml up -d --force-recreate api"
  echo "    # 导入完成后改回 0 并重启"
}

# --- 全量 ---------------------------------------------------
update_all() {
  log "全量：打包 → 上传 → 解压 → 重建"
  bash "${ROOT}/deploy/pack.sh" --build 2>&1 | tail -3
  bash "${ROOT}/deploy/upload.sh" 2>&1 | tail -4
  ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE} && tar -xzf /opt/gaokao-deploy.tgz"
  ssh $SSH_OPTS "${USER}@${HOST}" "cd ${REMOTE} && docker compose -f docker-compose.prod.yml build api 2>&1 | tail -3 \
    && docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -5"
  log "容器状态"
  ssh $SSH_OPTS "${USER}@${HOST}" "docker ps --filter name=gaokao --format '{{.Names}}\t{{.Status}}'"
}

case "$TYPE" in
  web)  update_web ;;
  api)  update_api ;;
  conf) update_conf ;;
  data) update_data ;;
  all)  update_all ;;
  *)    usage ;;
esac

log "完成"
