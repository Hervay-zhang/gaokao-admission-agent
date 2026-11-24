#!/usr/bin/env bash
# ============================================================
#  上传部署包到服务器（本机 Git Bash 执行）
#  认证方式：SSH_ASKPASS 免交互（密码只存在于环境变量 GAOKAO_SSH_PASSWORD，不落盘）
#
#  用法：
#    export GAOKAO_SSH_PASSWORD='服务器root密码'
#    bash deploy/upload.sh                  # 只上传 + 校验
#    bash deploy/upload.sh --extract        # 上传 + 解压到 /opt/gaokao + 列目录
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

HOST="${GAOKAO_HOST:-39.107.44.42}"
USER="${GAOKAO_USER:-root}"
PORT="${GAOKAO_PORT:-22}"
PKG="${ROOT}/../gaokao-deploy.tgz"
REMOTE_DIR="/opt"
REMOTE_PKG="${REMOTE_DIR}/gaokao-deploy.tgz"
DO_EXTRACT=0
[[ "${1:-}" == "--extract" ]] && DO_EXTRACT=1

[[ -f "$PKG" ]] || { echo "✗ 找不到部署包：$PKG（先跑 bash deploy/pack.sh --build）"; exit 1; }
[[ -n "${GAOKAO_SSH_PASSWORD:-}" ]] || { echo "✗ 未设置 GAOKAO_SSH_PASSWORD"; exit 1; }

# 免交互密码：askpass 脚本只引用环境变量，不写明文
ASKPASS="$(mktemp /tmp/gaokao_askpass.XXXXXX.sh)"
printf '#!/bin/sh\necho "$GAOKAO_SSH_PASSWORD"\n' > "$ASKPASS"
chmod 700 "$ASKPASS"
cleanup() { rm -f "$ASKPASS"; }
trap cleanup EXIT

export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o NumberOfPasswordPrompts=1 -P ${PORT}"

echo "==> 本机包信息"
LOCAL_SIZE=$(stat -c%s "$PKG")
LOCAL_SHA=$(sha256sum "$PKG" | awk '{print $1}')
echo "  路径：$PKG"
echo "  大小：${LOCAL_SIZE} 字节"
echo "  SHA256：${LOCAL_SHA}"

echo
echo "==> 创建远端目录 ${REMOTE_DIR}"
ssh $SSH_OPTS "${USER}@${HOST}" "mkdir -p ${REMOTE_DIR} && echo OK"

echo
echo "==> 上传中…（2MB，通常 10 秒内完成）"
scp $SSH_OPTS "$PKG" "${USER}@${HOST}:${REMOTE_PKG}"

echo
echo "==> 远端校验"
ssh $SSH_OPTS "${USER}@${HOST}" "stat -c%s ${REMOTE_PKG} ; sha256sum ${REMOTE_PKG} | awk '{print \$1}'" \
  | { read -r R_SIZE; read -r R_SHA;
      echo "  远端大小：${R_SIZE} 字节"
      echo "  远端 SHA256：${R_SHA}"
      if [[ "$R_SIZE" == "$LOCAL_SIZE" && "$R_SHA" == "$LOCAL_SHA" ]]; then
        echo "  ✓ 校验一致（大小 + SHA256 完全匹配）"
      else
        echo "  ✗ 校验不一致，请重传"; exit 1
      fi; }

if [[ $DO_EXTRACT -eq 1 ]]; then
  echo
  echo "==> 解压到 /opt/gaokao"
  ssh $SSH_OPTS "${USER}@${HOST}" "mkdir -p /opt/gaokao && tar -xzf ${REMOTE_PKG} -C /opt/gaokao && ls /opt/gaokao"
fi

echo
echo "完成。远端文件：${REMOTE_PKG}"
