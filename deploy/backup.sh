#!/usr/bin/env bash
# 每日备份。以 astock 身份跑（它只读得到 data/ 和 .env，攻破备份脚本也拿不到 root），
# 备份目录 /var/backups/astock 由 deploy.sh（fix_perms.sh）建好并归 astock 700。装成 cron：
#   sudo crontab -e
#   30 23 * * * sudo -u astock /opt/astock/deploy/backup.sh >> /var/log/astock-backup.log 2>&1
#
# 为什么不直接 tar 整个 data/：几个库都开着 WAL，服务又一直在跑，
# 直接打包会拿到撕裂的快照——恢复时可能是个坏库。sqlite3 .backup 是
# 在线备份接口，拿到的一定是一致的。
set -euo pipefail

APP_DIR="${ASTOCK_DIR:-/opt/astock}"
DEST="${ASTOCK_BACKUP_DIR:-/var/backups/astock}"
KEEP_DAYS="${ASTOCK_BACKUP_KEEP:-14}"
STAMP=$(date +%Y%m%d-%H%M)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"    # 正常情况下 deploy.sh 已建好；这里兜底（属主就是当前身份）
cd "$APP_DIR"
# cron 给的环境几乎是空的，.env 只有 systemd 单元和 config.py 会读；这里自己 source 一遍，
# 否则 README 让写进 .env 的 ASTOCK_OSS_BUCKET 在这里永远读不到、OSS 上传静默跳过。
# .env 是每行 KEY=value、注释单独占一行的格式，可以直接 source。
[ -f .env ] && set -a && . ./.env && set +a

echo "[$(date '+%F %T')] 开始备份 -> $DEST/astock-$STAMP.tar.gz"

# 1) 所有 sqlite 用在线备份接口拷出来（含公共库和每个人的个人库）
# 文件名含单引号会破坏 .backup 的引号、含换行会破坏逐行读取，这两类直接跳过并报出来。
NL=$'\n'
find data -type f -name '*.db' \( -name "*'*" -o -name "*${NL}*" \) \
  -exec sh -c 'printf "  ! 跳过（文件名含单引号或换行）：%s\n" "$1"' _ {} \;
find data -type f -name '*.db' ! -name "*'*" ! -name "*${NL}*" | while read -r db; do
  out="$WORK/$db"
  mkdir -p "$(dirname "$out")"
  sqlite3 "$db" ".backup '$out'" || { echo "  ! $db 备份失败"; continue; }
  echo "  · $db"
done

# 2) 非 db 的数据文件（每人的 watchlist/portfolio、复盘产出）
find data -type f ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' \
  -exec sh -c 'mkdir -p "$2/$(dirname "$1")" && cp "$1" "$2/$1"' _ {} "$WORK" \;

# 3) 配置。.env 里有 API key，单独说明一下它进了备份包
[ -f .env ] && cp .env "$WORK/.env"

tar -czf "$DEST/astock-$STAMP.tar.gz" -C "$WORK" .
chmod 600 "$DEST/astock-$STAMP.tar.gz"      # 包里有 .env 和所有人的持仓，别让别人读

SIZE=$(du -h "$DEST/astock-$STAMP.tar.gz" | cut -f1)
echo "[$(date '+%F %T')] 完成：astock-$STAMP.tar.gz ($SIZE)"

# 4) 只留最近 N 天
find "$DEST" -type f -name 'astock-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
echo "  保留最近 $KEEP_DAYS 天，当前共 $(ls -1 "$DEST"/astock-*.tar.gz 2>/dev/null | wc -l) 个备份"

# 5) 可选：传到阿里云 OSS（装了 ossutil 并配好 config 才会执行）
# 本机快照挡不住「机器被删」和「误删整个目录」这两种真实事故，
# 有条件的话强烈建议开这一步，异地才算备份。
# ossutil 1.x 的可执行名是 ossutil64，2.x 是 ossutil，两个都认。
OSSUTIL="$(command -v ossutil64 || command -v ossutil || true)"
if [ -n "$OSSUTIL" ] && [ -n "${ASTOCK_OSS_BUCKET:-}" ]; then
  "$OSSUTIL" cp "$DEST/astock-$STAMP.tar.gz" "oss://$ASTOCK_OSS_BUCKET/astock/" \
    && echo "  已上传 OSS: $ASTOCK_OSS_BUCKET"
elif [ -n "${ASTOCK_OSS_BUCKET:-}" ]; then
  echo "  ! 配了 ASTOCK_OSS_BUCKET 但没找到 ossutil/ossutil64，跳过 OSS 上传"
fi
