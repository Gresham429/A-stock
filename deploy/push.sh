#!/usr/bin/env bash
# 在**你自己的电脑上**运行：把代码推到服务器并重启服务。
#
#   bash deploy/push.sh
#
# 为什么要中转一次：应用目录属主是服务器上的 root（代码）和 astock（数据），
# 你用自己的登录名登录直接 rsync 进 /opt/astock 会撞权限。所以先同步到你家目录下的
# 暂存区，再用 sudo 从暂存区同步过去。这样全程不需要给 rsync 配 NOPASSWD。
set -euo pipefail

HOST="${ASTOCK_HOST:-aliyun_ecs}"     # ~/.ssh/config 里的 Host 别名
APP_DIR=/opt/astock
STAGE='~/astock-staging'

# 守卫：只允许推多用户版代码。推了没有登录闸门的旧版 app.py 上去，
# 服务器上所有 API 就裸奔了，而且个人数据路径也对不上。
[ -f app.py ] || { echo "请在仓库根目录运行：cd <仓库路径>"; exit 1; }
grep -q "import userctx" app.py || {
  echo "拒绝推送：app.py 里没有 import userctx，这不是多用户版代码（当前分支不对？）"
  exit 1
}

EXCLUDES=(--exclude .git --exclude data --exclude .venv --exclude __pycache__
          --exclude '*.pyc' --exclude .env --exclude .obsidian --exclude .claude
          --exclude .vscode --exclude deploy/logs
          --exclude watchlist.json --exclude portfolio.json --exclude ai_cache.json)

echo "-> 同步到 $HOST 的暂存区"
rsync -az --delete "${EXCLUDES[@]}" ./ "$HOST:$STAGE/"

echo "-> 落到 $APP_DIR 并重启"
echo "   提醒：重启会中断正在跑的 agent 轮次和复盘（它们在 worker 的后台线程里）。"
# 服务器侧也要 --delete（清掉已删除的文件），但 data/.env/.venv 必须排除，
# 否则一次推送就把所有人的持仓和 API key 抹了。
# 权限统一由 deploy/fix_perms.sh 收口（与 deploy.sh、MCP astock_push 同一份）。
ssh "$HOST" "bash -s" <<REMOTE
set -euo pipefail
sudo rsync -a --delete \
  --exclude data --exclude .env --exclude .venv --exclude __pycache__ \
  --exclude ai_cache.json \
  $STAGE/ $APP_DIR/
sudo bash $APP_DIR/deploy/fix_perms.sh
sudo systemctl restart astock-web astock-scheduler
sleep 3
for s in astock-web astock-scheduler; do
  if systemctl is-active --quiet \$s; then echo "  [ok] \$s 运行中"
  else echo "  [x] \$s 没起来：journalctl -u \$s -n 50 --no-pager"; fi
done
curl -fsS http://127.0.0.1:5000/healthz >/dev/null && echo "  [ok] /healthz 可达"
REMOTE

echo "-> 完成"
