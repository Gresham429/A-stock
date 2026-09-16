# 部署到阿里云 · 多人共用

这份文档配套 `deploy/` 下的脚本。先说清楚在防什么，再说怎么做，不然每一步
都只是"照着敲"，出了问题不知道哪层塌了。

---

## 一、原样上线会出什么事

改造前这个项目的形态是：Flask 自带开发服务器监听 `127.0.0.1:5000`，零鉴权，
所有 API 裸露，所有数据全局单份。搬到公网 IP 上会发生四件事：

| 风险 | 具体是什么 | 这套方案怎么挡 |
|---|---|---|
| **API key 被烧光** | 每日推荐、全市场选股、`/api/agents/run_all` 这些接口都能无限制触发 DeepSeek。`v4-pro` 是推理模型，一个循环脚本一夜能把余额清零，而且你第二天才发现 | `ratelimit.py`：每人日预算 + 全站日预算 + 最小调用间隔。计数落 sqlite，多 worker 下依然精确。计费点在真实的 DeepSeek 调用处，不在 HTTP 路由上（见「额度怎么调」） |
| **任何人可读可写** | 阿里云公网 IP 上线几分钟内就会被扫到，5000 是重点端口。持仓、笔记、模拟盘全部可读可删 | `auth.py`：`before_request` 全局闸门，默认全关、白名单只有 `/login` `/static` `/healthz`。新增路由不会因为忘了加装饰器而裸奔 |
| **数据串台** | `watchlist.json`、`portfolio.json`、`data/*.db` 都是全局一份，朋友加的自选股会出现在你的看板，他的持仓成本你看得见 | `userctx.py`：个人数据隔离到 `data/users/<用户名>/`，公共数据（行情/新闻/全市场池/因子/复盘）继续共享 |
| **进程被拖死** | Werkzeug 开发服务器无并发控制、无超时、暴露版本号；后台线程跟着 web 进程跑，多 worker 会重复决策、重复下单 | gunicorn（gthread）+ 定时任务拆成独立的 `scheduler.py` 单实例进程 |

再叠一层网络隔离：服务不上公网，走 Tailscale 私有组网。公网上这台机器只有
22 端口，看板本身从互联网上完全扫不到。

> 组网和登录是两层，不能互相替代。组网挡的是陌生人；登录挡的是"朋友的笔记本
> 丢了"和"tailnet 里某台设备被入侵"。少哪层都不行。

---

## 二、多用户版是怎么做的

多用户改造已经提交在代码里（分支 `multiuser`，之后合入 `main`），不再有单独的
补丁步骤。相关模块：

```
userctx.py       当前用户上下文 + 个人数据目录解析 + 跨线程/线程池传播 + 舰队站长
auth.py          账号 / 密码 / 服务端会话 / 全局闸门 / 安全响应头
ratelimit.py     按人限流 + AI 日预算（计费点在 llm._chat）+ token 记账
scheduler.py     独立调度进程（复盘 / 公共数据回填 / 清理；agent 自动跑默认关）
wsgi.py          gunicorn 入口
astockctl.py     账号与用量管理命令行
templates/login.html
```

各个人数据 store（自选股、持仓、画像、笔记、规则、模拟盘、agent）的库路径按当前
登录用户解析；公共 store 路径不变、连接走 `userctx.open_db` 开 WAL。

### 为什么用"一人一个 sqlite"而不是"表里加 user_id"

加 `user_id` 列要改每一条 SQL、每一个函数签名、每一个调用点，在这个体量的
既有代码上风险很高，而且很容易漏掉一两处 `WHERE`，那种漏法恰恰是最糟的
（数据串台且无声）。改路径解析只动了每个 store 里算路径的那几行。

在几个人的规模下，一人一个文件顺带还是优点：互不锁表、可以单独备份某个人、
删号就是删一个目录。

### 公共 vs 个人

| | 内容 | 位置 |
|---|---|---|
| 公共 | 新闻库、全市场池、因子回测、提示词模板、每日复盘、账号与用量 | `data/` |
| 个人 | 自选股、持仓、投资画像、私域笔记、交易规则、模拟盘 | `data/users/<用户名>/` |
| 舰队 | 20 个 agent、模拟账户、教训、journal | `data/users/<站长>/`，全站只此一套 |

全市场池按人复制既费盘也没意义，而且重复抓取会更快吃到东财的 IP 风控。

### agent 舰队 = 站长的数据

全站只有一套 agent 舰队，它属于「站长」这个账号：

- 站长由 `.env` 里的 `ASTOCK_FLEET_OWNER` 指定；留空则取 users 表里创建最早的管理员。
  一般就是你自己，`adduser <你> --admin` 之后不用再配：服务先起来也没关系，进程最多
  1 分钟内会自动认出新建的管理员，不用重启。站长一旦选定就不变（停用他也不换），
  要换只能填 `ASTOCK_FLEET_OWNER` 再重启；填了不存在的账号，启动日志会报 error。
- 所有登录用户都能看舰队的战绩、教训、house-view（GET 路由）；建/改/删 agent、
  手动跑 `run` / `run_all` 只允许管理员或站长本人，其他人返回 403。
- 舰队代码路径（agent 日循环、教训块、regime-view）永远在站长上下文里跑，其他账号
  自己目录里的 agents.db 保持为空。

### 服务器上 agent 不自动跑

本地 `python3 app.py` 维持原来的行为：开着 app 就按盘中时段桶自动跑舰队。

服务器上默认不跑（保留「不开 app 就不炒股」）：`scheduler.py` 只做复盘、公共数据
回填和清理；舰队只在站长手动点 `run` / `run_all` 时跑。真要让服务器自动跑，在
`.env` 里设 `ASTOCK_AGENT_AUTO=1` 并重启 `astock-scheduler`。

### 时区

限流的日期键、交易日判断、复盘落盘文件名都按北京时间算。两个 systemd unit 里
钉了 `Environment=TZ=Asia/Shanghai`，`deploy.sh` 也会把系统时区设成
`Asia/Shanghai`（`timedatectl` 不可用时跳过，服务进程仍由 unit 里的 TZ 兜底）。

---

## 三、本地也要初始化一次

多用户版在本地同样要登录。第一次跑之前敲两条命令，建自己的账号并把现有的
自选股/持仓/笔记迁到自己的目录：

```bash
python3 astockctl.py adduser <你的用户名> --admin
python3 deploy/migrate_to_multiuser.py <你的用户名>
python3 app.py                      # 之后 http://127.0.0.1:5000 会先跳到 /login
```

顺序不能反：migrate 要在第一次登录之前跑。登录（或调度进程的清理任务）会在你的目录里
先建出一批空库，migrate 看到目标已存在就跳过，你的舰队和笔记会被空库顶掉。真发生了，
加 `--force` 重跑一次就行（脚本在全部跳过时会以非零退出并提示）。`ASTOCK_FLEET_OWNER`
也等 migrate 跑完再填。

本地裸 http 调试时 cookie 不能带 Secure 标记：本地 `.env` 不设 `ASTOCK_ENV=production`
即可（默认不是）。

---

## 四、部署步骤

### 1. 买机器

2 核 2G 够用，系统选 Ubuntu 22.04（20.04 也行，deploy.sh 会自动装 python3.10）。两个 systemd unit 的内存上限是 web 1200M +
scheduler 600M，合计不超过 2G，就是按这个机型配的；换 4G 机器的话可以把两个
`MemoryMax` 都放大一倍。盘至少 40G：新闻库和板块日线是按天累积的。

**安全组只放行 22，且限制来源 IP**（阿里云控制台，实例，安全组，入方向）：

| 方向 | 端口 | 授权对象 | 说明 |
|---|---|---|---|
| 入 | 22 | 你的家宽 IP/32 | 不要填 0.0.0.0/0 |
| 入 | 其余 | 全部拒绝 | 5000 不要开 |
| 出 | 全部 | 允许 | 要抓行情、调 DeepSeek、Tailscale 打洞 |

家宽 IP 会变，变了就去控制台改一下；嫌麻烦可以等 Tailscale 装好后改走
`tailscale ssh`，那样 22 也可以对公网完全关闭。

### 2. 传代码并部署

先在 `~/.ssh/config` 里配好别名（下面所有命令都用它）：

```sshconfig
Host aliyun_ecs
    HostName <你的公网IP>
    User <你的登录名>
    Port 22
    # IdentityFile ~/.ssh/id_ed25519     确认自己的密钥文件名后解开这两行
    # IdentitiesOnly yes
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 10m
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

一条命令走完（推荐）：

```bash
mkdir -p ~/.ssh/sockets && chmod 700 ~/.ssh/sockets   # ControlPath 需要
bash deploy/bootstrap.sh
```

它会依次：确认本地是多用户版代码、跑离线测试、测 ssh 连通、rsync 代码（问你要不要
连 `data/` 一起传）、可选传 `.env`、在服务器上跑 `deploy.sh`。全程日志在 `deploy/logs/`。

要把本地的舰队、笔记、规则、模拟盘、画像迁到服务器，`data/` 必须一起传：那 5 个个人库
（`data/agents.db` 等）和 `watchlist.json`、`portfolio.json` 就是步骤 3 里 migrate 的来源。
只传代码的话服务器上从零开始，migrate 会打印 5 行「不存在，跳过」。

手动做的话等价于：

```bash
ssh aliyun_ecs 'sudo mkdir -p /opt/astock && sudo chown $USER /opt/astock'
rsync -av --exclude .git --exclude .venv --exclude __pycache__ \
      --exclude deploy/logs ./ aliyun_ecs:/opt/astock/     # 只传代码就再加 --exclude data
ssh aliyun_ecs 'sudo bash /opt/astock/deploy/deploy.sh'
```

事后想补传个人库（`deploy.sh` 跑完后 `/opt/astock` 代码归 root、`data/` 归 `astock`，不能直接 rsync 进去）：

```bash
scp data/agents.db aliyun_ecs:~/ \
  && ssh aliyun_ecs 'sudo mv ~/agents.db /opt/astock/data/ && sudo chown astock:astock /opt/astock/data/agents.db'
# 其余 4 个库和 watchlist.json / portfolio.json 同理，传完再跑步骤 3 的 migrate
```

机器上如果还跑着别的服务（nginx、k3s、游戏服之类，`ss -ltnp` 能看到它们监听公网端口），
不要让 `deploy.sh` 启用 ufw，否则会把它们一起挡掉：`ASTOCK_UFW=0 sudo -E bash deploy/deploy.sh`。
这种情况下外围防线是阿里云安全组和 Tailscale。

`deploy.sh` 会装依赖（`requirements.txt` + `deploy/requirements-server.txt`）、
设时区、建不可登录的 `astock` 系统账号、建 venv、生成 `ASTOCK_SECRET_KEY`、
设权限、装 systemd 服务（web、scheduler，以及每天五次触发 `fetch_news.py` 的 `astock-news.timer`，
时刻同本地 launchd：08:40 / 11:40 / 14:00 / 15:30 / 20:30，非交易日只有晚间那次真抓）、配 ufw、自检。
幂等，可重复跑。系统 python3 低于 3.10（Ubuntu 20.04 是 3.8）时会从 deadsnakes 装 python3.10 单独建 venv，不动系统 python。

权限规则集中在 `deploy/fix_perms.sh`，`deploy.sh`、`push.sh` 和 MCP 的 `astock_push`
都调它，三处不会各改各的：

| 路径 | 属主 | 权限 | 为什么 |
|---|---|---|---|
| `/opt/astock` 代码 | `root:astock` | 去掉组/其他写位 | 应用进程改不了自己的代码；被攻破也无法植入后门等下次重启 |
| `data/` | `astock:astock` | 700 | 所有人的持仓和笔记，只有应用账号能进 |
| `ai_cache.json` | `astock:astock` | 600 | AI 输出缓存，应用要写；不存在会先 touch |
| `.env` | `root:astock` | 640 | systemd 以 root 读 `EnvironmentFile`，`config.py` 由 `astock` 进程读，其他账号读不到。不能是 600：那样 `astock` 进程读不到 key |
| `deploy/` | `root:root` | 755 | 里面的脚本会被 root 执行，切断「应用被攻破后改 deploy.sh 再等 root 执行」这条提权链 |
| `/var/backups/astock` | `astock:astock` | 700 | `backup.sh` 以 `astock` 身份跑 |

两个 systemd unit 的 `ReadWritePaths` 也只放 `/opt/astock/data` 和 `/opt/astock/ai_cache.json`，
文件系统其余部分对进程只读。

跑完之后你不能再直接 rsync 进 `/opt/astock` 了。后续更新一律用
`deploy/push.sh`（见「日常运维」），它走暂存区中转，不需要给 rsync 配免密 sudo。

如果 bootstrap 时没传 `.env`，把本地的 key 补上去：

```bash
# 不用 scp：它先按远端 umask 落盘再改权限，中间有一瞬其他账号可读
ssh aliyun_ecs 'umask 077 && cat > ~/astock.env' < .env
ssh aliyun_ecs 'sudo mv ~/astock.env /opt/astock/.env \
  && sudo chown root:astock /opt/astock/.env && sudo chmod 640 /opt/astock/.env \
  && sudo systemctl restart astock-web astock-scheduler'
```

服务器上的 `.env` 要确认三项：`ASTOCK_ENV=production`（cookie 带 Secure）、
`ASTOCK_AGENT_AUTO=0`（默认）、`ASTOCK_FLEET_OWNER`（可留空）。注释必须单独占一行，
写在值后面会被当成值的一部分。

### 2.5 部署完先探一遍东财端点

东财按端点封 IP，家里能通的服务器上不一定通，反过来也一样。`deploy.sh` 跑完（`astock`
账号和 `.venv` 都是它建的，所以这一步只能放在它后面）先在服务器上跑一遍下面这些，
把结果记下来，这决定了复盘和板块归属两块功能能不能用：

```bash
cd /opt/astock && sudo -u astock .venv/bin/python3 - <<'EOF'
from review import fetch
d = fetch.resolve_trade_date()
print("交易日", d)
for name, fn in [("涨停池", fetch.zt_pool), ("炸板池", fetch.zb_pool),
                 ("跌停池", fetch.dt_pool), ("昨日定稿", fetch.yzt_pool),
                 ("题材串", fetch.theme_reasons)]:
    r = fn(d); print(name, "通" if r else "不通/空", len(r or []))
r = fetch.dragon_tiger(fetch.to_dash(d)); print("龙虎榜", "通" if r else "不通/空")
r = fetch.sector_flow(); print("板块资金流 clist", "通" if r else "不通（复盘降级为 4 角色）")
import datasources as ds
print("个股板块 slist", ds.concept_tags("600519") or "不通")
print("研报 reportapi", "通" if ds.eastmoney_reports("600519") else "不通")
print("个股新闻", "通" if ds.stock_news("600519") else "不通")
EOF
```

逐项对照：

| 端点 | 用在哪 | 不通的后果 |
|---|---|---|
| 打板四池 + 题材串（push2ex） | 复盘硬指标 | 复盘出不了 |
| 龙虎榜（datacenter） | 复盘游资分析师、深挖 | 该角色缺席 |
| 板块资金流 clist | 复盘资金面分析师 | 自动降级为 4 角色，可接受 |
| 个股板块 slist | 全市场池板块归属回填 | 板块面板空 |
| 研报 reportapi / 个股新闻 | 深挖 | 深挖里对应块为空 |

家里 IP 上 clist 是时通时封的，服务器上再测一遍才算数。

### 3. 建账号并迁移你的数据

```bash
ssh aliyun_ecs
cd /opt/astock
sudo -u astock .venv/bin/python3 astockctl.py adduser <你的用户名> --admin
sudo -u astock .venv/bin/python3 deploy/migrate_to_multiuser.py <你的用户名>
```

第一个管理员就是舰队站长，服务已经在跑也不用重启（最多 1 分钟内认出）。迁移脚本用
sqlite 的在线备份接口复制库（直接 `cp` 一个开着 WAL 的库会漏掉未 checkpoint 的事务，
拿到旧快照），原文件一个都不删。

两条命令紧挨着跑，中间不要先去登录：登录会在你的目录里建出空库，migrate 就会跳过它们
（脚本全部跳过时以非零退出并提示加 `--force`）。`ASTOCK_FLEET_OWNER` 也等 migrate
跑完再填。

给朋友开号。没有注册页面，这是故意的：注册入口是纯粹的攻击面，几个人的
规模下 ssh 敲一条命令就够了：

```bash
sudo -u astock .venv/bin/python3 astockctl.py adduser xiaoming --name 小明
# 直接回车会自动生成一个强密码并打印出来，只显示这一次
```

密码用微信发明文不太好。可以用一次性链接（如 privnote），或者当面告诉对方。
改密码走 `astockctl.py passwd`（没有自助改密页面）。

### 3.5 关掉 SSH 密码登录

公网开着 22 却允许密码登录，是比看板裸奔更常见的入侵路径：扫描器整天在爆破
常见用户名。做这一步之前，先确认密钥能免密登录：

```bash
ssh-copy-id aliyun_ecs     # 若还没传过公钥
ssh aliyun_ecs             # 必须能免密直接进来
```

然后在服务器上：

```bash
sudo bash /opt/astock/deploy/harden_ssh.sh
```

脚本有三层防呆，最坏情况也就是等十分钟自己恢复：

1. 改之前逐项体检：authorized_keys 是空的、或者当前这条连接是用密码进来的，
   直接拒绝执行并告诉你缺什么；
2. 改完布下倒计时撤销：用 `systemd-run` 起一个十分钟后自动还原的定时器，
   你验证通过后手动取消它才算数；
3. 用 reload 而不是 restart：当前这条连接不会被踢掉，你始终有个活口。

配置写在 `/etc/ssh/sshd_config.d/00-astock-hardening.conf`。文件名以 `00-` 开头
是因为 sshd 对同一选项取第一次出现的值，云镜像自带的 `50-cloud-init.conf` 常写着
`PasswordAuthentication yes`，我们的必须排在它前面。脚本检测到那个文件会把内容
打出来给你看，最后用 `sshd -T` 读实际生效值核实 `passwordauthentication no`，
文件写对但被压住的情况会直接报出来。

按提示另开一个终端验证 `ssh aliyun_ecs` 能进，再回去执行
`sudo systemctl stop astock-ssh-revert.timer` 确认。连不上就什么都别做，
等自动还原；实在等不及用阿里云控制台的 VNC 进去。

### 4. 接 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                        # 浏览器扫码登录（用 GitHub/Google 账号即可）
tailscale serve --bg 5000           # 给它一个真正的 https 证书
tailscale status                    # 记下 https://<机器名>.<tailnet>.ts.net
```

`tailscale serve` 会用 `*.ts.net` 的 Let's Encrypt 证书终止 TLS 再转给本机
5000。是真证书，浏览器不报警告，也不需要域名和备案。这一条同时解决了
"没备案"和"自签证书一堆警告"两个麻烦。

朋友那边：手机/电脑装 Tailscale，你在管理后台发邀请，他接受后直接访问那个
`ts.net` 地址。手机上可以"添加到主屏幕"，用起来跟 App 一样。

装好后建议把安全组的 22 也关掉，改用 `tailscale ssh`，那样这台机器在公网上
就是一个完全不响应的黑洞。

部署后验证一次：通过 `ts.net` 地址登录，然后做一个 POST（比如加一只自选股），
不得 403。若 403，说明反代改写了 `Host`，`auth` 的同源校验会读 `X-Forwarded-Host`，
检查反代有没有把它带上。

### 5. 备份

```bash
echo '30 23 * * * sudo -u astock /opt/astock/deploy/backup.sh >> /var/log/astock-backup.log 2>&1' \
  | sudo crontab -
```

以 `astock` 身份跑：它只读得到 `data/` 和 `.env`，备份脚本本身被攻破也拿不到 root。
备份目录 `/var/backups/astock` 由 `deploy.sh` 建好并归 `astock`（700）。
备份包里有 `.env`（含 API key）和所有人的持仓，所以文件权限是 600。
本机快照挡不住"机器被删"和"误删整个目录"，有条件请配 OSS 异地：

```bash
# 装 ossutil 并配好 config 后，在 .env 里加：
ASTOCK_OSS_BUCKET=你的bucket名
```

---

## 五、日常运维

```bash
# 看日志
journalctl -u astock-web -f
journalctl -u astock-scheduler -f

# 账号
sudo -u astock .venv/bin/python3 astockctl.py users
sudo -u astock .venv/bin/python3 astockctl.py passwd xiaoming    # 改密码并踢掉所有会话
sudo -u astock .venv/bin/python3 astockctl.py disable xiaoming   # 停用（数据保留）
sudo -u astock .venv/bin/python3 astockctl.py kick xiaoming      # 只踢会话（手机丢了用这个）

# 用量：谁花了多少
sudo -u astock .venv/bin/python3 astockctl.py usage --days 7

# 更新代码（本地跑，一条命令搞定同步+重启+健康检查）
# 重启会中断正在跑的 agent 轮次和复盘，挑它们不在跑的时候推
bash deploy/push.sh
```

`push.sh` 推送前会检查本地 `app.py` 含 `import userctx`，不是多用户版代码就拒绝
推送，免得把没有登录闸门的旧版推上去。

### 额度怎么调

`.env` 里改，改完 `systemctl restart astock-web astock-scheduler`。建议先按人头宽松设，
观察一周 `astockctl.py usage` 的真实分布再收紧：

```
ASTOCK_AI_PER_USER_DAY=40    # 每人每天
ASTOCK_AI_GLOBAL_DAY=150     # 全站每天（余额的最后一道保险）
```

计数单位是「一次真实的 DeepSeek 调用」，不是 HTTP 请求次数。门设在 `llm._chat`
发请求之前（`ratelimit.allow_llm`），成功返回后才记账（`ratelimit.record_ai_call`），
所以：

- `run_all` 跑 20 个 agent 就计 20 次，不是 1 次；
- 缓存命中、请求失败、超时都不计；
- 走 GET 的 AI 路由（比如大盘研判）一样受约束；
- 没有用户上下文的调用（调度器、复盘）记在 `system` 名下；舰队的调用（`ASTOCK_AGENT_AUTO=1`
  的自动跑，以及站长手动点的 `run` / `run_all`，都在站长上下文里跑）记在 `fleet` 名下。
  这两个名字都只受全站日预算约束，不占任何人的个人额度。20 个 agent 一天两桶就是几十次
  调用，`ASTOCK_AI_GLOBAL_DAY` 要把这部分算进去。

HTTP 层还有一道按请求计的门：每人每分钟总请求数（`ASTOCK_REQ_PER_MIN`）、AI 调用最小
间隔（`ASTOCK_AI_MIN_INTERVAL`）、重任务日上限（`ASTOCK_HEAVY_PER_USER_DAY`，全市场池
刷新/回测这类）。

---

## 六、这套方案没覆盖的

说清楚边界，免得产生虚假的安全感。

1. **朋友本人是可信的**。登录之后他在自己的数据范围内能做任何事。如果要防
   "朋友故意刷爆你的 key"，现有的日限只能限制损失规模，不能归零。真要彻底
   隔离就得每人填自己的 key，本轮不做。

2. **没有审计日志**。`data/usage.db` 记了谁在什么时候调了哪个接口、烧了多少 token，
   但读操作没记。几个人的规模下够用。

3. **没做 2FA**。加 TOTP 大约几十行，但在 Tailscale 之后收益有限：攻击者得
   先进你的 tailnet。

4. **数据源风控**。服务器 IP 固定且请求密集，比家里更容易被东财限流。深挖偶发
   "无数据"是正常的，`ASTOCK_HEAVY_PER_USER_DAY` 就是为了让它不恶化。三个进程
   共享同一个东财最小间隔（`data/.em_last_call` 文件锁）。

5. **合规**。给朋友提供 AI 荐股结果，在国内可能触及证券投资咨询的持牌范围。
   项目 README 里的免责声明建议在登录页和每日推荐结果处都显式展示一次，
   登录页已经加了。真要扩大范围请先弄清楚这件事。

6. **`tailscale serve` 之外没有 nginx**。少一层意味着没有边缘限流和请求缓冲，
   但也少一个要维护、要配错的组件。tailnet 内不存在匿名扫描，这个取舍是划算的。
   真想加，在 gunicorn 前面按常规配一个反代即可，`ASTOCK_BIND` 不用改。

---

## 七、回滚

没有数据库 schema 变更（个人库只是换了路径），所以回滚是干净的：

```bash
git checkout <改造前的 commit 或分支>     # 代码回到改造前
# 数据：data/users/<你>/ 下的文件拷回原位即可
cp data/users/<你的用户名>/watchlist.json .
cp data/users/<你的用户名>/portfolio.json .
cp data/users/<你的用户名>/*.db data/
```

因为迁移脚本用的是复制而非移动，原文件一直都在，只要你还没手动删。
