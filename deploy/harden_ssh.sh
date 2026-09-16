#!/usr/bin/env bash
# 服务器端 SSH 加固：关掉密码登录，只认密钥。
#
#   sudo bash /opt/astock/deploy/harden_ssh.sh
#
# 公网开着 22 却允许密码登录，是比看板裸奔更常见的入侵路径——扫描器整天
# 在爆破 root 和常见用户名。这个脚本关掉密码认证，只留密钥。
#
# ── 为什么这个脚本这么啰嗦 ──
# 改 sshd 配置最大的风险是把自己关在门外。所以它做三件事来兜底：
#   1. 改之前逐项体检，任何一项不过就拒绝执行，并告诉你缺什么；
#   2. 改完自动布一个「倒计时撤销」——你不在限定时间内确认，配置自动还原；
#   3. 用 reload 而不是 restart，当前这条连接不会被踢掉。
# 三层加起来，最坏情况也就是等几分钟自己恢复。
set -euo pipefail

DROPIN=/etc/ssh/sshd_config.d/99-astock-hardening.conf
MAIN=/etc/ssh/sshd_config
REVERT_UNIT=astock-ssh-revert
REVERT_MIN="${ASTOCK_SSH_REVERT_MIN:-10}"

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$*"; }
die()  { printf '\n\033[0;31m已中止，配置一个字没动。\033[0m\n%s\n\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash $0"

# sshd 的服务名在不同发行版上不一样（Ubuntu 叫 ssh，CentOS 系叫 sshd）
SVC=""
for s in ssh sshd; do
  systemctl list-unit-files "$s.service" >/dev/null 2>&1 && systemctl cat "$s.service" >/dev/null 2>&1 && SVC="$s" && break
done
[ -n "$SVC" ] || die "找不到 sshd 的 systemd 服务名，这个脚本不适用于你的系统。"

say "体检（全过才动手）"

# ① 谁在跑这个脚本——要拿到真实的登录用户，不是 root
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  TARGET_USER=root
  HOME_DIR=/root
else
  HOME_DIR=$(getent passwd "$TARGET_USER" | cut -d: -f6)
fi
ok "目标用户：$TARGET_USER（家目录 $HOME_DIR）"

# ② authorized_keys 必须存在且非空。这是关掉密码后唯一的进门方式，
#    它要是空的，改完就等于把门焊死。
AK="$HOME_DIR/.ssh/authorized_keys"
if [ ! -s "$AK" ]; then
  bad "$AK 不存在或是空的"
  die "关掉密码登录前，必须先让密钥能用。在你自己的电脑上跑：
    ssh-copy-id aliyun_ecs
然后**新开一个终端**验证 ssh aliyun_ecs 能免密进来，再回来重跑这个脚本。"
fi
NKEYS=$(grep -cvE '^\s*(#|$)' "$AK" || true)
ok "$AK 有 $NKEYS 个公钥"

# ③ 当前这条连接是不是用密钥进来的。如果你是用密码登录的，说明密钥路
#    还没验证过，此时关密码是在赌。
AUTH_OK=0
if [ -n "${SSH_CONNECTION:-}" ]; then
  PPID_CHAIN=$$
  for _ in 1 2 3 4 5 6; do
    PPID_CHAIN=$(ps -o ppid= -p "$PPID_CHAIN" 2>/dev/null | tr -d ' ') || break
    [ -z "$PPID_CHAIN" ] && break
    if journalctl _PID="$PPID_CHAIN" -u "$SVC" --since "-12h" 2>/dev/null | grep -q "Accepted publickey"; then
      AUTH_OK=1; break
    fi
    if journalctl _PID="$PPID_CHAIN" -u "$SVC" --since "-12h" 2>/dev/null | grep -q "Accepted password"; then
      AUTH_OK=2; break
    fi
  done
fi
case "$AUTH_OK" in
  1) ok "当前这条 SSH 连接是用密钥认证进来的" ;;
  2) bad "当前这条连接是用**密码**登录的"
     die "先确认密钥能用再来。在你自己的电脑上新开一个终端：
    ssh-copy-id aliyun_ecs      # 若还没传过公钥
    ssh aliyun_ecs             # 必须能免密直接进
确认免密成功后，用那条密钥连接重新跑这个脚本。" ;;
  *) printf '  \033[0;33m?\033[0m 判断不出当前连接的认证方式（日志里没找到）\n'
     printf '    倒计时撤销仍然会布上，所以最坏情况是 %s 分钟后自动还原。\n' "$REVERT_MIN"
     read -r -p "    确认你已经验证过密钥能免密登录？输入 yes 继续：" a
     [ "$a" = "yes" ] || die "没确认，先去验证密钥登录。" ;;
esac

# ④ 主配置有没有 Include 那个 .d 目录；没有的话 drop-in 不会生效
USE_DROPIN=1
if ! grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' "$MAIN"; then
  USE_DROPIN=0
  printf '  \033[0;33m?\033[0m %s 没有 Include sshd_config.d，改为直接追加到主配置\n' "$MAIN"
else
  ok "支持 sshd_config.d drop-in"
fi

say "备份"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/etc/ssh/sshd_config.astock-backup-$STAMP"
cp -a "$MAIN" "$BACKUP"
ok "主配置已备份到 $BACKUP"

say "写入加固项"
CONF='# 由 deploy/harden_ssh.sh 写入。删掉本文件并 reload sshd 即可完全还原。
PasswordAuthentication no          # 只认密钥，彻底断掉密码爆破
KbdInteractiveAuthentication no    # 键盘交互也是密码的一种入口，一并关掉
PermitRootLogin prohibit-password  # root 只能用密钥，不能用密码
MaxAuthTries 3                     # 单连接试错次数，拖慢批量尝试
LoginGraceTime 20                  # 连上却不认证的连接尽快踢掉，防 slowloris 占满 slot
X11Forwarding no                   # 这台机器不需要，关掉少一个面
ClientAliveInterval 300
ClientAliveCountMax 2'

if [ "$USE_DROPIN" = 1 ]; then
  mkdir -p /etc/ssh/sshd_config.d
  printf '%s\n' "$CONF" > "$DROPIN"
  chmod 644 "$DROPIN"
  ok "已写入 $DROPIN"
  REVERT_CMD="rm -f $DROPIN && systemctl reload $SVC"
else
  printf '\n# ── astock hardening %s ──\n%s\n' "$STAMP" "$CONF" >> "$MAIN"
  ok "已追加到 $MAIN"
  REVERT_CMD="cp -a $BACKUP $MAIN && systemctl reload $SVC"
fi

say "语法校验"
if ! sshd -t 2>/tmp/sshd-test.err; then
  bad "sshd 配置语法有错，正在还原："
  cat /tmp/sshd-test.err
  eval "$REVERT_CMD" || true
  die "已自动还原，sshd 未受影响。"
fi
ok "sshd -t 通过"

say "布下倒计时撤销（$REVERT_MIN 分钟）"
systemctl stop "$REVERT_UNIT.timer" 2>/dev/null || true
if systemd-run --on-active="${REVERT_MIN}min" --unit="$REVERT_UNIT" \
     --description="astock: 自动还原 SSH 加固配置" \
     /bin/bash -c "$REVERT_CMD" >/dev/null 2>&1; then
  ok "已布下：$REVERT_MIN 分钟内不确认，配置自动还原"
  ARMED=1
else
  printf '  \033[0;33m?\033[0m systemd-run 不可用，没有自动撤销兜底\n'
  printf '    请务必在关掉当前终端前验证新连接，手动还原命令：\n      %s\n' "$REVERT_CMD"
  ARMED=0
fi

say "生效（用 reload，当前连接不会断）"
systemctl reload "$SVC"
ok "$SVC 已 reload"

cat <<TIP

───────────────────────────────────────────────────────
  ⚠  别关这个终端。现在去你自己的电脑上**另开一个终端**验证：

        ssh aliyun_ecs

  能正常进来，说明密钥路是通的，回到这里执行确认：
TIP
if [ "$ARMED" = 1 ]; then
cat <<TIP
        sudo systemctl stop $REVERT_UNIT.timer

  不执行确认的话，$REVERT_MIN 分钟后配置自动还原，一切如常。
TIP
else
cat <<TIP
        （本机没有自动撤销，验证失败时手动还原：）
        sudo $REVERT_CMD
TIP
fi
cat <<TIP

  如果新终端连不上：什么都别做，等 $REVERT_MIN 分钟自动还原；
  实在等不及，用阿里云控制台的 VNC 远程连接进去执行还原命令。
───────────────────────────────────────────────────────
TIP
