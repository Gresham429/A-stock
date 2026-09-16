#!/usr/bin/env bash
# 服务器一键部署（Ubuntu / Debian / 阿里云 Linux 都能跑）。
#
#   # 先把代码传上去
#   rsync -av --exclude .git --exclude data --exclude .venv \
#         ./ root@<服务器IP>:/opt/astock/
#   ssh root@<服务器IP> 'bash /opt/astock/deploy/deploy.sh'
#
# 这个脚本是幂等的，改完配置重跑一遍即可。它不会碰 data/ 里的任何数据。
set -euo pipefail

APP_DIR="${ASTOCK_DIR:-/opt/astock}"
APP_USER="astock"
PY="${APP_DIR}/.venv/bin/python3"

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;31m!\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }
[ -f "$APP_DIR/app.py" ] || { echo "$APP_DIR 下没有 app.py —— 先把代码传上去"; exit 1; }

say "1/8 系统依赖"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl rsync >/dev/null
else
  yum install -y -q python3 python3-pip sqlite curl rsync >/dev/null
fi
ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2) / sqlite3 $(sqlite3 --version | cut -d' ' -f1)"

say "2/8 专用系统账号（应用绝不以 root 身份运行）"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  ok "已创建用户 $APP_USER（不可登录）"
else
  ok "用户 $APP_USER 已存在"
fi

say "3/8 Python 虚拟环境"
if [ ! -x "$PY" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q flask gunicorn
ok "flask + gunicorn 就绪"

say "4/8 配置文件"
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$APP_DIR/deploy/env.example" ]; then
    cp "$APP_DIR/deploy/env.example" "$APP_DIR/.env"
    warn "已生成 $APP_DIR/.env —— 现在必须填进去 DEEPSEEK_API_KEY 再启动"
  else
    warn "缺少 .env，请手动创建并填 DEEPSEEK_API_KEY"
  fi
fi
# 每台机器一个独立的 secret，别跨机器复用
if ! grep -q "^ASTOCK_SECRET_KEY=" "$APP_DIR/.env" 2>/dev/null; then
  echo "ASTOCK_SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$APP_DIR/.env"
  ok "已生成 ASTOCK_SECRET_KEY"
fi

say "5/8 目录与权限"
mkdir -p "$APP_DIR/data/users"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 700 "$APP_DIR/data"        # 所有人的持仓和笔记都在这里，只有应用账号能进
chmod 600 "$APP_DIR/.env"        # API key，只有属主能读
ok "data/ 700, .env 600, 属主 $APP_USER"

say "6/8 systemd 服务"
cp "$APP_DIR/deploy/astock-web.service"       /etc/systemd/system/
cp "$APP_DIR/deploy/astock-scheduler.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now astock-web astock-scheduler >/dev/null 2>&1 || true
sleep 3
for s in astock-web astock-scheduler; do
  if systemctl is-active --quiet "$s"; then ok "$s 运行中"
  else warn "$s 未启动 —— 看日志：journalctl -u $s -n 50 --no-pager"; fi
done

say "7/8 本机防火墙"
# 真正的防线是阿里云安全组（见 README-deploy.md）。这里再关一道，
# 防的是「安全组规则被误改」这种事——两层都得破才暴露。
if command -v ufw >/dev/null; then
  ufw --force default deny incoming >/dev/null
  ufw --force default allow outgoing >/dev/null
  ufw allow 22/tcp >/dev/null
  ufw allow in on tailscale0 >/dev/null 2>&1 || true
  ufw --force enable >/dev/null
  ok "ufw：入方向默认拒绝，仅放行 22 和 tailscale0"
else
  warn "没装 ufw，请确保阿里云安全组只放行 22"
fi

say "8/8 自检"
cd "$APP_DIR"
curl -fsS http://127.0.0.1:5000/healthz >/dev/null 2>&1 \
  && ok "本机 /healthz 可达" \
  || warn "本机 /healthz 不通，看 journalctl -u astock-web -n 50 --no-pager"
sudo -u "$APP_USER" "$PY" astockctl.py status 2>/dev/null || true

cat <<'TIP'

───────────────────────────────────────────────────────
接下来还有三步（必须做完才算能用）：

  1. 建你自己的账号（顺手迁移现有数据）
       cd /opt/astock
       sudo -u astock .venv/bin/python3 astockctl.py adduser <你的名字> --admin
       sudo -u astock .venv/bin/python3 deploy/migrate_to_multiuser.py <你的名字>

  2. 接入 Tailscale，让服务只在私有网络里可达
       curl -fsSL https://tailscale.com/install.sh | sh
       tailscale up
       tailscale serve --bg 5000
       tailscale status        # 记下 https://<机器名>.<你的tailnet>.ts.net

  3. 挂上每日备份
       echo '30 23 * * * /opt/astock/deploy/backup.sh >> /var/log/astock-backup.log 2>&1' \
         | sudo crontab -

给朋友开号：
       sudo -u astock .venv/bin/python3 astockctl.py adduser <朋友的名字>
查用量：
       sudo -u astock .venv/bin/python3 astockctl.py usage --days 7
───────────────────────────────────────────────────────
TIP
