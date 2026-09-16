# astock-deploy · MCP server

让 Claude 能操作阿里云那台机器。跑在你自己的 Mac 上（普通 macOS 进程），
所以它有你的网络、你的 `~/.ssh`、你的 `rsync`——这三样恰好是 Claude 那两个
沙箱都没有的东西。

## 装

加到你配 `labmot` 的同一个 MCP 配置文件里：

```json
{
  "mcpServers": {
    "astock-deploy": {
      "command": "python3",
      "args": ["<仓库路径>/deploy/mcp/astock_deploy_server.py"]
    }
  }
}
```

零第三方依赖，不用 pip install。改完配置重启 Claude 桌面端，
`mcp__remote-devices__astock_deploy__*` 就会出现在工具列表里。

自测（不用 Claude）：

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | python3 deploy/mcp/astock_deploy_server.py
```

## 六个工具

| 工具 | 性质 | 干什么 |
|---|---|---|
| `astock_check` | 只读 | 一次 ssh 看完：主机信息、磁盘内存、补丁打没打、`.env` 权限、`data/` 大小、有哪些用户、两个服务状态、5000 端口、Tailscale、SSH 密码登录关没关 |
| `astock_logs` | 只读 | 两个服务的 journalctl，可按正则过滤 |
| `astock_users` | 只读 | 账号列表 + 最近 7 天各人 AI 用量和 token |
| `astock_push` | 写 | 本地代码 → 服务器 → 重启 → 报健康检查。排除 `data/`、`.env`、`.venv` |
| `astock_deploy` | 写 | 服务器上跑 `deploy.sh`。幂等 |
| `astock_adduser` | 写 | 建账号；密码写到你本机 `~/astock-credentials/<uid>.txt`，**不返回到对话里** |

## 安全边界（这是重点）

整个项目的起点是你担心安全，那这个 server 本身就不能是「给 Claude 一个
root shell」。所以：

- **目标主机写死在代码里**（`HOST` 常量），不是参数。Claude 改不了打哪台机器。
- **没有任意命令执行工具**。每个工具对应一个具体动作，参数受 enum / 长度 /
  字符集约束。唯一进入 shell 的自由文本是 `astock_logs` 的 `grep`，它走
  `shlex.quote`——已实测 `x; rm -rf /` 和 `$(whoami)` 都被封在单引号里当字面量。
- **读多写少**，六个工具里三个是纯只读。
- **审计日志**：每次调用连参数一起写到 `~/.astock-mcp-audit.log`，你随时能查
  Claude 都做过什么。
- **密码不入对话**：`astock_adduser` 生成的密码落到本机 600 权限的文件，
  返回给 Claude 的文本里那行被抹掉。

### 故意没做的事

| 不做 | 为什么 |
|---|---|
| 动 sshd 配置 | `harden_ssh.sh` 要你另开终端验证新连接，自动化反而更容易把你锁在门外 |
| 读 `.env` 内容 | Claude 不需要知道你的 DeepSeek key |
| 删除文件 / 删库 | 没有任何撤销机制的操作，不给 |
| 任意命令执行 | 见上面「安全边界」 |

这四件事留给你亲手做。

### 你实际授予了什么

装上这个 server，等于允许 Claude 在会话期间用你的 SSH 密钥、以 `<你的登录名>` 身份、
在 `<你的公网IP>` 上执行上表那六个动作（其中三个会改服务器状态）。
密钥本身 Claude 看不到也拿不走——`ssh` 进程在你机器上，密钥从不离开。

不想给这么多的话，把 `HANDLERS` 里的写操作删掉只留三个只读工具，
排查问题一样够用，代码只需要删三行。
