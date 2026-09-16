#!/usr/bin/env bash
# 在**你自己的电脑上**运行：把代码推到服务器并重启服务。
#
#   bash deploy/push.sh
#
# 为什么要中转一次：应用目录属主是服务器上的 astock 账号（不可登录），
# 你用自己的登录名登录直接 rsync 进 /opt/astock 会撞权限。所以先同步到你家目录下的
# 暂存区，再用 sudo 从暂存区同步过去。这样全程不需要给 rsync 配 NOPASSWD。
set -euo pipefail

HOST="${ASTOCK_HOST:-aliyun_ecs}"     # ~/.ssh/config 里的 Host 别名
APP_DIR=/opt/astock
STAGE='~/astock-staging'

EXCLUDES=(--exclude .git --exclude data --exclude .venv --exclude __pycache__
          --exclude '*.pyc' --exclude .env --exclude .obsidian --exclude .claude
          --exclude watchlist.json --exclude portfolio.json --exclude ai_cache.json)

echo "▸ 同步到 $HOST 的暂存区"
rsync -az --delete "${EXCLUDES[@]}" ./ "$HOST:$STAGE/"

echo "▸ 落到 $APP_DIR 并重启"
# 服务器侧也要 --delete（清掉已删除的文件），但 data/.env/.venv 必须排除，
# 否则一次推送就把所有人的持仓和 API key 抹了。
ssh "$HOST" "bash -s" <<REMOTE
set -euo pipefail
sudo rsync -a --delete \
  --exclude data --exclude .env --exclude .venv --exclude __pycache__ \
  $STAGE/ $APP_DIR/
sudo chown -R astock:astock $APP_DIR
sudo chmod 700 $APP_DIR/data 2>/dev/null || true
sudo chmod 600 $APP_DIR/.env 2>/dev/null || true
sudo systemctl restart astock-web astock-scheduler
sleep 3
for s in astock-web astock-scheduler; do
  if systemctl is-active --quiet \$s; then echo "  ✓ \$s 运行中"
  else echo "  ✗ \$s 没起来：journalctl -u \$s -n 50 --no-pager"; fi
done
curl -fsS http://127.0.0.1:5000/healthz >/dev/null && echo "  ✓ /healthz 可达"
REMOTE

echo "▸ 完成"
