#!/bin/bash
# 服务器侧安装/更新脚本（幂等，不删除任何东西：旧代码、旧配置都挪到带时间戳的备份目录）。
# 由 tools/deploy_server.sh 上传后执行：bash /tmp/fds-remote-install.sh
set -euo pipefail
APP=/opt/flex-date-saver
PKG=/tmp/fds-update.tar.gz
TS=$(date +%Y%m%d-%H%M%S)
OLD=$APP/.releases/$TS

echo "== 1 依赖"
command -v python3.11 >/dev/null || dnf install -y python3.11
id flex-date-saver >/dev/null 2>&1 || useradd --system --home-dir $APP/data --shell /sbin/nologin flex-date-saver
mkdir -p $APP/data $OLD

echo "== 2 停服并备份数据"
systemctl stop flex-date-saver 2>/dev/null || true
if [ -d $APP/data/jobs ]; then tar -czf $OLD/data-jobs.tar.gz -C $APP data/jobs; fi

echo "== 3 旧代码挪到 $OLD，解包新代码（不碰 data/）"
for d in server skill web deploy; do [ -e $APP/$d ] && mv $APP/$d $OLD/; done
tar -xzf $PKG -C $APP

echo "== 4 flyai-cli 1.0.16（装在应用目录，不动全局）"
if [ ! -x $APP/node_modules/.bin/flyai ] || ! $APP/node_modules/.bin/flyai --help 2>/dev/null | grep -q search-train; then
  cd $APP
  [ -f package.json ] || echo '{"name":"fds-runtime","private":true}' > package.json
  npm install --no-audit --no-fund @fly-ai/flyai-cli@1.0.16
fi
chown -R flex-date-saver:flex-date-saver $APP

echo "== 5 环境变量文件"
if [ -f /tmp/fds.env ]; then
  [ -f /etc/flex-date-saver.env ] && mv /etc/flex-date-saver.env $OLD/flex-date-saver.env
  mv /tmp/fds.env /etc/flex-date-saver.env
  chown root:root /etc/flex-date-saver.env
  chmod 600 /etc/flex-date-saver.env $OLD/flex-date-saver.env 2>/dev/null || true
fi
test -f /etc/flex-date-saver.env

echo "== 6 systemd"
install -m 644 $APP/deploy/flex-date-saver.service /etc/systemd/system/flex-date-saver.service
systemctl daemon-reload
systemctl enable flex-date-saver >/dev/null 2>&1
systemctl start flex-date-saver

echo "== 7 nginx（独立 conf，改前备份、nginx -t 通过才 reload，失败自动挪走新配置）"
CONF=/etc/nginx/conf.d/flex-date-saver.conf
if ! cmp -s $APP/deploy/nginx-flex-date-saver.conf $CONF 2>/dev/null; then
  [ -f $CONF ] && cp $CONF $OLD/nginx-flex-date-saver.conf
  cp $APP/deploy/nginx-flex-date-saver.conf $CONF
  if nginx -t; then
    systemctl reload nginx
  else
    echo "nginx -t 失败，新配置已挪到 $OLD/nginx.failed.conf"
    mv $CONF $OLD/nginx.failed.conf
    [ -f $OLD/nginx-flex-date-saver.conf ] && cp $OLD/nginx-flex-date-saver.conf $CONF
    exit 1
  fi
fi

echo "== 8 验证"
sleep 2
curl -s http://127.0.0.1:8092/healthz; echo
curl -s -o /dev/null -w "nginx 8088 -> %{http_code}\n" http://127.0.0.1:8088/
curl -s -o /dev/null -w "friends-activity healthz -> %{http_code}\n" http://127.0.0.1:8090/healthz
systemctl is-active flex-date-saver friends-activity nginx
