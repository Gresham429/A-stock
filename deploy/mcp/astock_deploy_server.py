#!/usr/bin/env python3
"""astock-deploy —— 让 Claude 能操作阿里云那台机器的 MCP server。

跑在你自己的 Mac 上（普通 macOS 进程，不是 Claude 那个隔离沙箱），所以它有
你的网络、你的 ~/.ssh、你的 rsync。Claude 通过 MCP 调它，它 ssh 过去执行。

# 安全设计：窄接口，不是裸 shell

这个 server **故意不提供**「执行任意命令」的工具。理由很简单：整个项目的起点
就是你担心安全，那么解决方案本身不能是「给 Claude 一个 root shell」。所以：

  · 目标主机写死在代码里（HOST 常量），不是参数——Claude 改不了打哪台机器
  · 每个工具对应一个具体动作，参数受约束，不拼接任意命令
  · 读操作和写操作分开，读操作占多数
  · 所有调用都写审计日志到本地，你随时能查 Claude 都做过什么

**故意没做的事**（这些留给你亲手做）：
  · 动 sshd 配置 —— harden_ssh.sh 需要你另开终端验证，自动化反而危险
  · 读 .env 内容 —— Claude 不需要看你的 API key
  · 删除任何东西 —— 删库删文件一律不提供
  · 任意命令执行 —— 见上

# 装法

加到你配 labmot 的那个 MCP 配置里：

    "astock-deploy": {
      "command": "python3",
      "args": ["<仓库路径>/deploy/mcp/astock_deploy_server.py"]
    }

零第三方依赖，不用 pip install 任何东西（和主项目一个路子）。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

# ── 写死的常量。Claude 无法通过参数改变这些。 ────────────────────────────────
HOST = os.environ.get("ASTOCK_MCP_HOST", "aliyun_ecs")   # ~/.ssh/config 里的别名
APP_DIR = "/opt/astock"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_PY = f"{APP_DIR}/.venv/bin/python3"
AUDIT = os.path.join(os.path.expanduser("~"), ".astock-mcp-audit.log")
CRED_DIR = os.path.join(os.path.expanduser("~"), "astock-credentials")

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", HOST]
MAX_OUT = 20000      # 单次返回给 Claude 的输出上限，防止把日志整本灌进对话


def audit(tool: str, args: dict, rc: int | None = None) -> None:
    """每次调用都留痕。你要能事后查清 Claude 到底做了什么。"""
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{tool}\t"
                    f"{json.dumps(args, ensure_ascii=False)}\trc={rc}\n")
    except OSError:
        pass


def run(cmd: list[str], timeout: int = 600, cwd: str | None = None) -> tuple[int, str]:
    """执行并合并 stdout/stderr。超时算失败，不挂死。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"[超时 {timeout}s] 命令未在限定时间内结束：{' '.join(cmd[:3])}…"
    except FileNotFoundError as e:
        return 127, f"[找不到命令] {e}"
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n…[输出被截断，原长 {len(out)} 字符]"
    return p.returncode, out


def ssh_run(remote_cmd: str, timeout: int = 600) -> tuple[int, str]:
    return run(SSH + [remote_cmd], timeout=timeout)


# ── 工具实现 ─────────────────────────────────────────────────────────────────

def t_check(_: dict) -> str:
    """只读体检。一次 ssh 把该看的都看了，省往返。"""
    script = f"""
echo "=== 主机 ==="
hostname; uname -srm; echo "当前用户: $USER"
echo
echo "=== 系统资源 ==="
free -m 2>/dev/null | head -2 || vm_stat | head -3
df -h {APP_DIR} / 2>/dev/null | head -3
echo
echo "=== 应用目录 ==="
if [ -d {APP_DIR} ]; then
  ls -ld {APP_DIR} {APP_DIR}/data 2>/dev/null
  echo "代码文件数: $(ls {APP_DIR}/*.py 2>/dev/null | wc -l)"
  echo "userctx.py 在不在（判断补丁打没打）: $([ -f {APP_DIR}/userctx.py ] && echo 是 || echo 否)"
  echo ".env 存在: $([ -f {APP_DIR}/.env ] && echo 是 || echo 否)  权限: $(stat -c %a {APP_DIR}/.env 2>/dev/null || echo -)"
  echo "venv 存在: $([ -x {VENV_PY} ] && echo 是 || echo 否)"
  du -sh {APP_DIR}/data 2>/dev/null
  echo "用户数据目录:"; ls {APP_DIR}/data/users 2>/dev/null | sed 's/^/  /' || echo "  （无）"
else
  echo "{APP_DIR} 不存在 —— 还没部署过"
fi
echo
echo "=== systemd 服务 ==="
for s in astock-web astock-scheduler; do
  printf '%-20s %s\\n' "$s" "$(systemctl is-active $s 2>/dev/null || echo 未安装)"
done
echo
echo "=== 本机 5000 端口 ==="
curl -fsS -m 5 http://127.0.0.1:5000/healthz 2>&1 | head -2 || echo "不通"
echo
echo "=== Tailscale ==="
command -v tailscale >/dev/null && (tailscale status 2>&1 | head -5) || echo "未安装"
echo
echo "=== SSH 密码登录是否已关 ==="
sudo -n sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|maxauthtries)' \
  || echo "（需要 sudo 才能看，跳过）"
"""
    rc, out = ssh_run(script)
    return out if out.strip() else f"（无输出，rc={rc}）"


def t_logs(args: dict) -> str:
    svc = args.get("service", "astock-web")
    if svc not in ("astock-web", "astock-scheduler"):
        raise ValueError("service 只能是 astock-web 或 astock-scheduler")
    n = max(10, min(int(args.get("lines", 80)), 400))
    grep = args.get("grep", "")
    cmd = f"journalctl -u {shlex.quote(svc)} -n {n} --no-pager"
    if grep:
        # grep 关键字过一遍 shlex.quote，不让它逃出引号拼出别的命令
        cmd += f" | grep -iE {shlex.quote(grep)}"
    rc, out = ssh_run(cmd, timeout=60)
    return out or f"（无匹配日志，rc={rc}）"


def t_push(_: dict) -> str:
    """本地代码 → 服务器，然后重启。就是 push.sh 那套，走暂存区中转。"""
    if not os.path.isfile(os.path.join(REPO, "app.py")):
        raise ValueError(f"本地仓库路径不对：{REPO} 下没有 app.py")
    excludes = [".git", "data", ".venv", "__pycache__", "*.pyc", ".env",
                ".obsidian", ".claude", ".vscode", "deploy/logs",
                "watchlist.json", "portfolio.json", "ai_cache.json"]
    cmd = ["rsync", "-az", "--delete"]
    for e in excludes:
        cmd += ["--exclude", e]
    cmd += ["./", f"{HOST}:~/astock-staging/"]
    rc, out = run(cmd, timeout=900, cwd=REPO)
    if rc != 0:
        return f"[rsync 失败 rc={rc}]\n{out}"

    remote = f"""
set -e
sudo rsync -a --delete --exclude data --exclude .env --exclude .venv \
  --exclude __pycache__ ~/astock-staging/ {APP_DIR}/
sudo chown -R astock:astock {APP_DIR}
sudo chmod 700 {APP_DIR}/data 2>/dev/null || true
sudo chmod 600 {APP_DIR}/.env 2>/dev/null || true
sudo systemctl restart astock-web astock-scheduler
sleep 3
for s in astock-web astock-scheduler; do
  printf '%-20s %s\\n' "$s" "$(systemctl is-active $s)"
done
curl -fsS -m 5 http://127.0.0.1:5000/healthz && echo " <- healthz 正常"
"""
    rc2, out2 = ssh_run(remote, timeout=300)
    return f"[本地 rsync 完成]\n{out}\n[服务器侧 rc={rc2}]\n{out2}"


def t_deploy(_: dict) -> str:
    """首次部署：跑服务器上的 deploy.sh。幂等，重复跑安全。"""
    rc, out = ssh_run(f"sudo bash {APP_DIR}/deploy/deploy.sh", timeout=900)
    return f"[deploy.sh rc={rc}]\n{out}"


def t_users(_: dict) -> str:
    rc, out = ssh_run(
        f"cd {APP_DIR} && sudo -u astock {VENV_PY} astockctl.py status "
        f"&& echo && sudo -u astock {VENV_PY} astockctl.py users "
        f"&& echo && sudo -u astock {VENV_PY} astockctl.py usage --days 7", timeout=90)
    return out or f"（无输出 rc={rc}）"


def t_adduser(args: dict) -> str:
    """建账号。密码**不返回给 Claude**——写到你本地一个文件里。

    Claude 不需要知道你朋友的密码，对话记录里也不该留着它。
    """
    uid = (args.get("uid") or "").strip()
    if not uid.replace("_", "").replace("-", "").isalnum() or not uid[:1].isalnum():
        raise ValueError("uid 只能是字母数字和 _- ，且首字符是字母数字")
    if len(uid) < 2 or len(uid) > 32:
        raise ValueError("uid 长度要在 2-32 之间")
    name = (args.get("display_name") or "").strip()[:32]
    admin = "--admin" if args.get("admin") else ""

    # 密码在服务器上生成，直接落到本地文件，不经过对话
    remote = (f"cd {APP_DIR} && printf '\\n' | sudo -u astock {VENV_PY} astockctl.py "
              f"adduser {shlex.quote(uid)} {admin}")
    if name:
        remote += f" --name {shlex.quote(name)}"
    rc, out = ssh_run(remote, timeout=90)

    os.makedirs(CRED_DIR, exist_ok=True)
    os.chmod(CRED_DIR, 0o700)
    path = os.path.join(CRED_DIR, f"{uid}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {datetime.now().isoformat(timespec='seconds')}  账号 {uid}\n{out}\n")
    os.chmod(path, 0o600)

    # 把输出里的密码行抹掉再交给 Claude
    safe = "\n".join("（密码已写入本地文件，不在此显示）"
                     if "密码" in ln and ("生成" in ln or ":" in ln) else ln
                     for ln in out.splitlines())
    return (f"[adduser rc={rc}]\n{safe}\n\n"
            f"密码原文在你本机：{path}（权限 600）\n"
            f"用安全方式发给对方，别走微信明文。")


TOOLS = [
    {"name": "astock_check",
     "description": "只读体检阿里云那台机器：主机信息、磁盘内存、应用目录状态（补丁打没打、.env 权限、data 大小、有哪些用户）、两个 systemd 服务是否在跑、本机 5000 端口通不通、Tailscale 状态、SSH 密码登录关没关。不改任何东西。排查问题先调这个。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "astock_logs",
     "description": "拉 astock-web 或 astock-scheduler 的 journalctl 日志。服务起不来、报 500、agent 没跑时看这个。",
     "inputSchema": {"type": "object", "properties": {
         "service": {"type": "string", "enum": ["astock-web", "astock-scheduler"],
                     "description": "哪个服务，默认 astock-web"},
         "lines": {"type": "integer", "minimum": 10, "maximum": 400,
                   "description": "取多少行，默认 80"},
         "grep": {"type": "string", "description": "只保留匹配这个正则的行（可选）"}}}},
    {"name": "astock_push",
     "description": "把本地仓库的代码同步到服务器并重启两个服务，然后报告服务状态和健康检查结果。排除 data/、.env、.venv——不会动服务器上的数据和密钥。改完代码用这个。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "astock_deploy",
     "description": "在服务器上执行 deploy.sh（装依赖、建 astock 系统账号、建 venv、设权限、装 systemd 服务、配 ufw）。幂等，重复跑安全。首次部署或改了 systemd 配置后用。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "astock_users",
     "description": "只读：列出配置概览、所有账号（含是否停用、在线会话数、最后登录）、最近 7 天各人的 AI 用量和 token 消耗。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "astock_adduser",
     "description": "给服务器上新建一个看板账号。密码由服务器随机生成并写入用户本机的 ~/astock-credentials/<uid>.txt，不会返回到对话里。",
     "inputSchema": {"type": "object", "properties": {
         "uid": {"type": "string", "description": "用户名，小写字母数字和 _-，2-32 位"},
         "display_name": {"type": "string", "description": "显示昵称（可选）"},
         "admin": {"type": "boolean", "description": "是否设为管理员"}},
         "required": ["uid"]}},
]

HANDLERS = {"astock_check": t_check, "astock_logs": t_logs, "astock_push": t_push,
            "astock_deploy": t_deploy, "astock_users": t_users,
            "astock_adduser": t_adduser}


# ── MCP stdio 协议（行分隔 JSON-RPC 2.0，零依赖手写）────────────────────────

def reply(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue

        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "astock-deploy", "version": "1.0.0"},
                "instructions": (
                    f"操作 {HOST} 上的 A股观察台部署。目标主机写死在 server 里，不可通过参数改变。"
                    f"不提供任意命令执行；不动 sshd 配置；不读 .env 内容；不删除任何东西。"
                    f"所有调用记审计日志到 {AUDIT}。排查问题请先调 astock_check。")}})

        elif method == "tools/list":
            reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = HANDLERS.get(name)
            if not fn:
                audit(str(name), args, rc=-1)
                reply({"jsonrpc": "2.0", "id": rid,
                       "result": {"content": [{"type": "text",
                                               "text": f"未知工具：{name}"}],
                                  "isError": True}})
                continue
            t0 = time.time()
            try:
                text = fn(args)
                err = False
            except ValueError as e:          # 参数不合法：调用方姿势不对
                text, err = f"参数错误：{e}", True
            except Exception as e:  # noqa: BLE001 任何异常都要变成正常回包，不能崩掉 server
                text, err = f"工具执行出错：{type(e).__name__}: {e}", True
            audit(name, args, rc=1 if err else 0)
            reply({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": f"{text}\n\n[{name} 耗时 {time.time()-t0:.1f}s]"}],
                "isError": err}})

        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass   # 通知类消息无需回包

        elif method == "ping":
            reply({"jsonrpc": "2.0", "id": rid, "result": {}})

        elif rid is not None:
            reply({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"未实现的方法: {method}"}})


if __name__ == "__main__":
    main()
