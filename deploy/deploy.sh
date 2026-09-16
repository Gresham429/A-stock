#!/usr/bin/env bash
# 服务器一键部署（Ubuntu / Debian / 阿里云 Linux 都能跑）。
#
#   首次部署：在本机仓库根目录  bash deploy/bootstrap.sh（推代码后自动调本脚本）
#   更新代码：在本机仓库根目录  bash deploy/push.sh
#   单独重跑：ssh <别名> 'sudo bash /opt/astock/deploy/deploy.sh'
#
# 这个脚本是幂等的，改完配置重跑一遍即可。它不会碰 data/ 里的任何数据。
set -euo pipefail

APP_DIR="${ASTOCK_DIR:-/opt/astock}"
APP_USER="astock"
PY="${APP_DIR}/.venv/bin/python3"

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;31m!\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }
[ -f "$APP_DIR/app.py" ] || { echo "$APP_DIR 下没有 app.py —— 先把代码传上去"; exit 1; }

say "1/9 系统依赖"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  # 机器上常有过期的第三方源（比如 docker 的 focal 源没了 Release 文件），
  # update 报错不代表官方源坏了，只警告不中止；真装不上下面一行会报。
  apt-get update -qq || warn "apt-get update 有源失败（多半是过期的第三方源），继续用已有索引"
  apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl rsync >/dev/null
else
  yum install -y -q python3 python3-pip sqlite curl rsync >/dev/null
fi
# 代码用了 zoneinfo 与 3.10 语法。Ubuntu 20.04 自带 3.8，这种情况用 Miniconda 在
# $APP_DIR/.venv 建一个 python3.10 环境（目录布局和 venv 一样：bin/python3、bin/pip，
# systemd 单元和其它脚本不用改）。不走 deadsnakes PPA：launchpad 在国内机器上
# 经常拉不到索引。Miniconda 装在 /opt/miniconda3，只用来建这一个环境。
PYBIN=python3
USE_CONDA=0
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2) / sqlite3 $(sqlite3 --version | cut -d' ' -f1)"
elif command -v python3.10 >/dev/null; then
  PYBIN=python3.10
  ok "系统 python3 过旧，用已有的 python3.10 / sqlite3 $(sqlite3 --version | cut -d' ' -f1)"
else
  USE_CONDA=1
  CONDA_DIR="${ASTOCK_CONDA_DIR:-/opt/miniconda3}"
  if [ ! -x "$CONDA_DIR/bin/conda" ]; then
    # 国内机器先试清华镜像，不通再回官方
    for u in https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh \
             https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh; do
      curl -fsSL -m 600 "$u" -o /tmp/miniconda.sh && break
    done
    [ -s /tmp/miniconda.sh ] || { echo "Miniconda 安装包下载失败"; exit 1; }
    bash /tmp/miniconda.sh -b -p "$CONDA_DIR" >/dev/null
    rm -f /tmp/miniconda.sh
  fi
  ok "系统 python3 $(python3 -V 2>&1 | cut -d' ' -f2) 过旧，用 Miniconda（$CONDA_DIR）建 3.10 环境 / sqlite3 $(sqlite3 --version | cut -d' ' -f1)"
fi

say "2/9 时区"
# 限流日预算、交易日判断、复盘文件名都按北京时间算。systemd unit 里已钉了
# TZ=Asia/Shanghai，这里把系统时区也对齐，方便 journalctl/cron 的时间对得上。
# 容器或精简系统里可能没有 timedatectl，失败不致命。
if timedatectl set-timezone Asia/Shanghai 2>/dev/null; then
  ok "系统时区 Asia/Shanghai"
else
  warn "timedatectl 设时区失败（服务进程仍由 unit 里的 TZ=Asia/Shanghai 兜底）"
fi

say "3/9 专用系统账号（应用绝不以 root 身份运行）"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  ok "已创建用户 $APP_USER（不可登录）"
else
  ok "用户 $APP_USER 已存在"
fi

say "4/9 Python 虚拟环境"
# venv 若是旧解释器建的（比如系统 3.8），推倒重建；pip 缓存不进 venv，代价只是重装 flask+gunicorn
if [ -x "$PY" ] && ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  warn "现有 venv 是 $("$PY" -V 2>&1)，重建为 $($PYBIN -V 2>&1)"
  rm -rf "$APP_DIR/.venv"
fi
if [ ! -x "$PY" ]; then
  if [ "$USE_CONDA" = 1 ]; then
    "$CONDA_DIR/bin/conda" create -y -q -p "$APP_DIR/.venv" python=3.10 \
      --override-channels -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main >/dev/null 2>&1 \
      || "$CONDA_DIR/bin/conda" create -y -q -p "$APP_DIR/.venv" python=3.10 >/dev/null
  else
    "$PYBIN" -m venv "$APP_DIR/.venv"
  fi
fi
# pip 源可用 ASTOCK_PIP_INDEX 指定（国内机器填 https://pypi.tuna.tsinghua.edu.cn/simple）
PIP_OPTS=()
[ -n "${ASTOCK_PIP_INDEX:-}" ] && PIP_OPTS=(-i "$ASTOCK_PIP_INDEX")
"$APP_DIR/.venv/bin/pip" install -q "${PIP_OPTS[@]}" --upgrade pip
# 依赖以仓库里的两个清单为准：requirements.txt 是应用本身的，
# deploy/requirements-server.txt 只有服务器才需要的（gunicorn）。
"$APP_DIR/.venv/bin/pip" install -q "${PIP_OPTS[@]}" -r "$APP_DIR/requirements.txt" -r "$APP_DIR/deploy/requirements-server.txt"
ok "依赖就绪（requirements.txt + deploy/requirements-server.txt）"

say "5/9 配置文件"
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

say "6/9 目录与权限"
# 规则集中在 deploy/fix_perms.sh（push.sh 和 MCP 推送后也调它）：
# 代码归 root:astock 且无组写，只有 data/ 和 ai_cache.json 归 astock；
# .env 归 root:astock 640；备份目录 /var/backups/astock 归 astock 700。
bash "$APP_DIR/deploy/fix_perms.sh"

say "7/9 systemd 服务"
cp "$APP_DIR/deploy/astock-web.service"       /etc/systemd/system/
cp "$APP_DIR/deploy/astock-scheduler.service" /etc/systemd/system/
cp "$APP_DIR/deploy/astock-news.service"      /etc/systemd/system/
cp "$APP_DIR/deploy/astock-news.timer"        /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now astock-web astock-scheduler >/dev/null 2>&1 || true
# 新闻库增量抓取：timer 每天五次触发 oneshot 单元（时刻同本地 launchd）
systemctl enable --now astock-news.timer >/dev/null 2>&1 || true
sleep 3
for s in astock-web astock-scheduler astock-news.timer; do
  if systemctl is-active --quiet "$s"; then ok "$s 运行中"
  else warn "$s 未启动 —— 看日志：journalctl -u $s -n 50 --no-pager"; fi
done

say "8/9 本机防火墙"
# 真正的防线是阿里云安全组（见 README-deploy.md）。这里再关一道，
# 防的是「安全组规则被误改」这种事——两层都得破才暴露。
# 机器上还跑着别的服务（nginx / k3s / 游戏服 等）时不能这么做：默认拒绝入站会把它们
# 一起关掉。那种情况用 ASTOCK_UFW=0 跳过，只靠安全组 + Tailscale。
if [ "${ASTOCK_UFW:-1}" != 1 ]; then
  warn "ASTOCK_UFW=0：跳过 ufw（共用机器，别的服务还在监听公网端口），防线只剩安全组 + Tailscale"
elif command -v ufw >/dev/null; then
  ufw --force default deny incoming >/dev/null
  ufw --force default allow outgoing >/dev/null
  ufw allow 22/tcp >/dev/null
  ufw allow in on tailscale0 >/dev/null 2>&1 || true
  ufw --force enable >/dev/null
  ok "ufw：入方向默认拒绝，仅放行 22 和 tailscale0"
else
  warn "没装 ufw，请确保阿里云安全组只放行 22"
fi

say "9/9 自检"
cd "$APP_DIR"
curl -fsS http://127.0.0.1:5000/healthz >/dev/null 2>&1 \
  && ok "本机 /healthz 可达" \
  || warn "本机 /healthz 不通，看 journalctl -u astock-web -n 50 --no-pager"
sudo -u "$APP_USER" "$PY" astockctl.py status 2>/dev/null || true

# deploy/ 收归 root：这里的脚本会被 root 执行（deploy.sh），如果应用账号能写它，
# 应用一旦被攻破就能改脚本再借 root 之手提权。755 保证 astock 仍能读
# deploy/gunicorn.conf.py，也能以自己的身份执行 backup.sh。
# 第 6 步的 fix_perms.sh 已做过一遍，这里再收一次是因为 pip/自检可能在 deploy/ 下留文件。
chown -R root:root "$APP_DIR/deploy"
chmod -R 755 "$APP_DIR/deploy"
ok "deploy/ 属主 root，权限 755"

cat <<'TIP'

───────────────────────────────────────────────────────
接下来还有三步（必须做完才算能用）：

  1. 建你自己的账号（顺手迁移现有数据）。第一个管理员就是舰队站长，服务不用重启；
     想显式指定的话等 migrate 跑完再在 .env 里填 ASTOCK_FLEET_OWNER=<你的名字> 并重启服务。
     两条紧挨着跑，中间别先去登录，否则 migrate 会跳过已建出的空库，得加 --force 重跑。
       cd /opt/astock
       sudo -u astock .venv/bin/python3 astockctl.py adduser <你的名字> --admin
       sudo -u astock .venv/bin/python3 deploy/migrate_to_multiuser.py <你的名字>

  2. 接入 Tailscale，让服务只在私有网络里可达
       curl -fsSL https://tailscale.com/install.sh | sh
       tailscale up
       tailscale serve --bg 5000
       tailscale status        # 记下 https://<机器名>.<你的tailnet>.ts.net

  3. 挂上每日备份（以 astock 身份跑，备份目录 /var/backups/astock 已建好并归它）
       echo '30 23 * * * sudo -u astock /opt/astock/deploy/backup.sh >> /var/log/astock-backup.log 2>&1' \
         | sudo crontab -

给朋友开号：
       sudo -u astock .venv/bin/python3 astockctl.py adduser <朋友的名字>
查用量：
       sudo -u astock .venv/bin/python3 astockctl.py usage --days 7
───────────────────────────────────────────────────────
TIP
