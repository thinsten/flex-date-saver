#!/bin/bash
# 本地一键部署到个人服务器（数据保留式）。
# 用法：tools/deploy_server.sh            # 只更新代码
#       tools/deploy_server.sh --env      # 同时上传本地生成的 /tmp/fds.env（首次部署或换密钥时）
# 密钥只从本机文件读取，不打印、不进 git。
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${FDS_SSH:-root@47.102.103.226}
tar --no-xattrs --exclude='__pycache__' --exclude='*.pyc' --exclude='cache' -czf /tmp/fds-update.tar.gz server skill web deploy
scp -O -q /tmp/fds-update.tar.gz $HOST:/tmp/fds-update.tar.gz
scp -O -q deploy/remote-install.sh $HOST:/tmp/fds-remote-install.sh
if [ "${1:-}" = "--env" ]; then
  test -f /tmp/fds.env || { echo "缺少 /tmp/fds.env"; exit 1; }
  scp -O -q /tmp/fds.env $HOST:/tmp/fds.env
fi
ssh $HOST 'bash /tmp/fds-remote-install.sh'
