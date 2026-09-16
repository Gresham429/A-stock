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

零第三方依赖，不用 pip install。改完配置重启 Claude 桌面端，六个工具就会出现在
工具列表里，名字以你 MCP 配置里的 server 名为前缀，例如 `mcp__astock-deploy__astock_check`；
若经聚合层接入，前缀按那一层的规则。

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
| `astock_check` | 只读 | 一次 ssh 看完：主机信息、磁盘内存、是否多用户版代码、`.env` 权限、`data/` 大小、有哪些用户、两个服务状态、5000 端口、Tailscale、SSH 密码登录关没关 |
| `astock_logs` | 只读 | 两个服务的 journalctl，可按正则过滤（模式不能以 `-` 开头） |
| `astock_users` | 只读 | 账号列表 + 最近 7 天各人 AI 用量和 token |
| `astock_push` | 写 | 本地代码同步到服务器，重启，报健康检查。排除 `data/`、`.env`、`.venv`；本地 `app.py` 没有 `import userctx` 时拒绝推送；推完跑 `deploy/fix_perms.sh` 收权限（代码 root、数据 astock、`deploy/` root）。重启会中断正在跑的 agent 轮次/复盘 |
| `astock_deploy` | 写 | 服务器上跑 `deploy.sh`。幂等 |
| `astock_adduser` | 写 | 建账号；密码写到你本机 `~/astock-credentials/<uid>.txt`（以 0600 创建，不经过默认 umask），**不返回到对话里**，发给对方后删掉该文件。服务器侧建号失败则不落文件、原样返回输出 |

## 安全边界（这是重点）

整个项目的起点是你担心安全，那这个 server 本身就不能是「给 Claude 一个
root shell」。所以：

- **目标主机不是工具参数**。它来自启动 server 进程时的环境变量 `ASTOCK_MCP_HOST`
  （默认 `aliyun_ecs`，即 `~/.ssh/config` 里的别名）。Claude 在对话里改不了打哪台
  机器；能改的只有你自己的 MCP 配置。ssh 命令里主机名前有 `--`，别名本身也不会
  被当成 ssh 选项。
- **没有任意命令执行工具**。每个工具对应一个具体动作，参数受 enum / 长度 /
  字符集约束。唯一进入 shell 的自由文本是 `astock_logs` 的 `grep`：它走
  `shlex.quote`（`x; rm -rf /` 和 `$(whoami)` 都被封在单引号里当字面量），
  前面加了 `-e` 让它只能是模式、不能变成 grep 的选项，以 `-` 开头的直接拒绝。
- `astock_adduser` 的 `uid` 用与 `userctx.UID_RE` 相同的正则校验；`admin` 只认
  JSON 布尔 `true`，字符串 `"true"` 或数字 1 都不会开管理员。
- **读多写少**，六个工具里三个是纯只读。
- **审计日志**：`~/.astock-mcp-audit.log` 以 0600 创建。每次调用写两行：执行前一行
  `start`（进程中途被杀也留得下痕迹），执行后一行 `done` 带 rc，参数原样记录。
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

不想给这么多的话，把三个写工具删掉，排查问题一样够用：`HANDLERS` 是一个 dict
字面量，六个键分散写在几行里，要删的是 `astock_push`／`astock_deploy`／
`astock_adduser` 这三个键值对，不是整行删。同时要把 `TOOLS` 列表（`tools/list`
实际返回给 Claude 的那份）里对应的三个条目也删掉，否则光删 `HANDLERS` 的键，
Claude 仍然在工具列表里看得到这三个工具、还是能调用（调用时才会报错）。
