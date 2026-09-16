#!/usr/bin/env bash
# /opt/astock 的属主与权限，单一事实源。deploy.sh、push.sh、MCP 的 astock_push
# 三处都调这一份，避免三套 chown/chmod 各改各的漂移。
#
#   sudo bash /opt/astock/deploy/fix_perms.sh
#
# 原则：应用账号 astock 只能写数据，不能写代码。
#   代码目录      root:astock，去掉组/其他的写位。应用被攻破也改不了自己的代码。
#   data/         astock:astock 700，所有人的持仓和笔记都在这里。
#   AI 输出缓存 ai_cache.json 在 data/ 下，随 data/ 一起归 astock。
#   .env          root:astock 640。systemd 以 root 读 EnvironmentFile，config.py 以
#                 astock 进程读，所以属主 root、组 astock 可读、其他人不可读。
#   deploy/       root:root 755。里面的脚本会被 root 执行（deploy.sh、cron），
#                 应用账号能写它就等于能借 root 之手提权。
#   备份目录      astock:astock 700，backup.sh 以 astock 身份跑。
set -euo pipefail

APP_DIR="${ASTOCK_DIR:-/opt/astock}"
APP_USER="astock"
BACKUP_DIR="${ASTOCK_BACKUP_DIR:-/var/backups/astock}"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }
[ -d "$APP_DIR" ] || { echo "$APP_DIR 不存在"; exit 1; }

chown -R "root:$APP_USER" "$APP_DIR"
chmod -R g-w,o-w "$APP_DIR"
chmod 750 "$APP_DIR"

mkdir -p "$APP_DIR/data/users"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

# ai_cache.json 已搬到 data/ 下（沙箱只放行 data/）；根目录若有旧文件留作只读迁移源

if [ -f "$APP_DIR/.env" ]; then
  chown "root:$APP_USER" "$APP_DIR/.env"
  chmod 640 "$APP_DIR/.env"
fi

chown -R root:root "$APP_DIR/deploy"
chmod -R 755 "$APP_DIR/deploy"

mkdir -p "$BACKUP_DIR"
chown "$APP_USER:$APP_USER" "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "  [ok] 代码 root:$APP_USER 无组写；data/（含 ai_cache.json）归 $APP_USER；.env 640 root:$APP_USER；deploy/ root 755；$BACKUP_DIR 700 $APP_USER"
