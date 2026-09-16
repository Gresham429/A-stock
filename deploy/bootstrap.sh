#!/usr/bin/env bash
# 在**你自己的 Mac 上**、仓库根目录运行：
#
#   bash deploy/bootstrap.sh
#
# 把「打补丁 → 跑测试 → 建远端目录 → 推代码 → 执行部署」串成一条命令。
# 每一步失败就地停下，不会把你留在半吊子状态。需要你做判断的地方（填 key、
# 建账号、关 SSH 密码登录）它不碰，跑完会明确告诉你接下来敲什么。
set -euo pipefail

HOST="${ASTOCK_HOST:-aliyun_ecs}"
APP_DIR=/opt/astock
ASSUME_YES="${ASTOCK_YES:-0}"
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[0;31m✗ %s\033[0m\n\n' "$*"; exit 1; }
ask()  {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "  $1 [y/N] " a
  [ "$a" = "y" ] || [ "$a" = "Y" ]
}

[ -f app.py ] && [ -d deploy ] || die "请在仓库根目录运行：cd <仓库路径>"

# ── 自动留全量日志 ───────────────────────────────────────────────────────────
# 把 stdout/stderr 同时抄一份到 deploy/logs/，这样出问题时有完整原文可查，
# 不用靠回滚终端缓冲区或者手动复制粘贴。
LOG_DIR="$(pwd)/deploy/logs"
mkdir -p "$LOG_DIR"
# 日志里可能有主机名、路径之类，不进 git
[ -f "$LOG_DIR/.gitignore" ] || printf '*\n!.gitignore\n' > "$LOG_DIR/.gitignore"
LOG="$LOG_DIR/bootstrap-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
printf '日志：%s\n' "$LOG"

# ── 0. 本地前置 ──────────────────────────────────────────────────────────────
say "0/5  本地前置检查"

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  warn "当前在 $BRANCH 分支上。补丁会改 16 个文件，强烈建议先开分支。"
  ask "仍然继续？" || die "已退出。先跑：git checkout -b multiuser"
fi
[ -n "$BRANCH" ] && ok "分支：$BRANCH"

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warn "工作区有未提交的改动——打完补丁就分不清哪些是你的、哪些是补丁的了。"
  ask "仍然继续？" || die "已退出。先跑：git add -A && git commit -m '改造前存档'"
fi

command -v rsync >/dev/null || die "本机没有 rsync"
ok "rsync 就位"

# ── 1. 打补丁 ────────────────────────────────────────────────────────────────
say "1/5  多用户改造补丁"
if grep -q "^import userctx" app.py 2>/dev/null; then
  ok "已经打过补丁，跳过"
else
  python3 deploy/patch_multiuser.py --check >/dev/null || die "补丁预演失败，源文件和补丁预期的版本不一致"
  ok "预演通过（所有锚点都对得上）"
  ask "执行补丁？" || die "已退出，什么都没改。"
  python3 deploy/patch_multiuser.py
fi

# ── 2. 测试 ──────────────────────────────────────────────────────────────────
say "2/5  跑一遍原有测试"
FAILED=""
for f in tests/test_*.py; do
  python3 "$f" >/dev/null 2>&1 || FAILED="$FAILED $f"
done
if [ -n "$FAILED" ]; then
  die "以下测试没过，先别上服务器：$FAILED
单独跑一个看详情：python3 <文件名>"
fi
ok "$(ls tests/test_*.py | wc -l | tr -d ' ') 个测试文件全绿"

# ── 3. 连通性 ────────────────────────────────────────────────────────────────
say "3/5  服务器连通性"
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null; then
  die "ssh $HOST 连不上，或者需要输密码。
先确认这条能免密进去：  ssh $HOST
没配过密钥的话：        ssh-copy-id $HOST
改了 Host 别名的话：    ASTOCK_HOST=<你的别名> bash deploy/bootstrap.sh"
fi
ok "ssh $HOST 免密可达"
REMOTE_USER=$(ssh "$HOST" 'echo $USER')
ssh "$HOST" 'sudo -n true' 2>/dev/null && ok "$REMOTE_USER 有免密 sudo" \
  || warn "$REMOTE_USER 的 sudo 需要输密码，下面几步会提示你输"

# ── 4. 推代码 ────────────────────────────────────────────────────────────────
say "4/5  推送到 $HOST:$APP_DIR"

# 公共市场数据值得一起传：universe.db 已经 40MB，服务器从零回填要很久，
# 而且新 IP 猛抓东财很容易直接吃到风控。个人库随后由 migrate 脚本归位。
SEED_DATA=0
if [ -d data ]; then
  SZ=$(du -sh data 2>/dev/null | cut -f1)
  echo "  本地 data/ 有 $SZ（新闻库 / 全市场池 / 因子 / 复盘）。"
  echo "  一起传过去可以省掉服务器从零回填的时间，也避免新 IP 猛抓被东财风控。"
  ask "把 data/ 一起传？" && SEED_DATA=1
fi

EXC=(--exclude .git --exclude .venv --exclude __pycache__ --exclude '*.pyc'
     --exclude .env --exclude .obsidian --exclude .claude --exclude .vscode)
[ "$SEED_DATA" = 1 ] || EXC+=(--exclude data)

ssh "$HOST" "sudo mkdir -p $APP_DIR && sudo chown \$USER $APP_DIR"
rsync -az --info=progress2 "${EXC[@]}" ./ "$HOST:$APP_DIR/"
ok "代码已同步"

if [ -f .env ]; then
  echo "  本地有 .env（含 DeepSeek key）。服务器上必须有它才能起服务。"
  if ask "传过去？"; then
    scp -q .env "$HOST:~/astock.env"
    ssh "$HOST" "sudo mv ~/astock.env $APP_DIR/.env && sudo chmod 600 $APP_DIR/.env"
    ok ".env 已传（权限 600）"
  else
    warn "跳过了。服务器上没有 .env 服务起不来，记得手动补。"
  fi
fi

# ── 5. 执行部署 ──────────────────────────────────────────────────────────────
say "5/5  在服务器上执行部署"
echo "  deploy.sh 会：装依赖、建不可登录的 astock 账号、建 venv、设权限、"
echo "  装两个 systemd 服务、配 ufw。幂等，可重复跑。"
ask "开始？" || die "已退出。代码已经在服务器上了，你可以随时手动跑：
  ssh $HOST 'sudo bash $APP_DIR/deploy/deploy.sh'"

ssh -t "$HOST" "sudo bash $APP_DIR/deploy/deploy.sh"

cat <<TIP

═══════════════════════════════════════════════════════
  自动能做的到此为止。剩下的都需要你做判断，按顺序来：

  ① 建你的账号，并把现有自选股/持仓迁过去
       ssh $HOST
       cd $APP_DIR
       sudo -u astock .venv/bin/python3 astockctl.py adduser <你的名字> --admin
       sudo -u astock .venv/bin/python3 deploy/migrate_to_multiuser.py <你的名字>

  ② 接 Tailscale，让看板从公网消失
       curl -fsSL https://tailscale.com/install.sh | sh
       sudo tailscale up
       sudo tailscale serve --bg 5000
       tailscale status          # 记下 https://<机器名>.<tailnet>.ts.net

  ③ 关掉 SSH 密码登录（先确认 ~/.ssh/*.pub 只有一个，否则先在
     ~/.ssh/config 里把 IdentityFile 指明——加固脚本会设 MaxAuthTries 3）
       sudo bash $APP_DIR/deploy/harden_ssh.sh

  ④ 挂上每日备份
       echo '30 23 * * * $APP_DIR/deploy/backup.sh >> /var/log/astock-backup.log 2>&1' | sudo crontab -

  上面这几步如果想留下完整日志好排查，在本地这样跑（不用 ssh 进去）：
       ssh aliyun_ecs 'cd /opt/astock && sudo -u astock .venv/bin/python3 astockctl.py users' \
         2>&1 | tee deploy/logs/step1.log

  以后更新代码：本地跑  bash deploy/push.sh

  本次日志：见 deploy/logs/ 下最新的那个文件
═══════════════════════════════════════════════════════
TIP
