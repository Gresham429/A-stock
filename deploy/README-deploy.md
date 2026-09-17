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
| **任何人可读可写** | 阿里云公网 IP 上线几分钟内就会被扫到，5000 是重点端口。持仓、笔记、模拟盘全部可读可删 | `auth.py`：`before_request` 全局闸门，默认全关、白名单只有 `/login` `/logout` `/healthz` 三个路径，另加 `/static/` 前缀。新增路由不会因为忘了加装饰器而裸奔 |
| **数据串台** | `watchlist.json`、`portfolio.json`、`data/*.db` 都是全局一份，朋友加的自选股会出现在你的看板，他的持仓成本你看得见 | `userctx.py`：个人数据隔离到 `data/users/<用户名>/`，公共数据（行情/新闻/全市场池/因子/复盘）继续共享 |
| **进程被拖死** | Werkzeug 开发服务器无并发控制、无超时、暴露版本号；后台线程跟着 web 进程跑，多 worker 会重复决策、重复下单 | gunicorn（gthread）+ 定时任务拆成独立的 `scheduler.py` 单实例进程 |

再叠一层网络隔离：服务不上公网，走 Tailscale 私有组网。公网上这台机器只有
22 端口，看板本身从互联网上完全扫不到。

> 组网和登录是两层，不能互相替代。组网挡的是陌生人；登录挡的是"朋友的笔记本
> 丢了"和"tailnet 里某台设备被入侵"。少哪层都不行。

---

## 二、多用户版是怎么做的

机制（用户隔离、舰队归属、计费点、时区）见仓库根 `CLAUDE.md`「多用户与部署」节。
这里只列数据边界：

| | 内容 | 位置 |
|---|---|---|
| 公共 | 新闻库、全市场池、因子回测、提示词模板、每日复盘、账号与用量、三周期选股账本 `picks_public.db` | `data/` |
| 个人 | 自选股、持仓、投资画像、私域笔记、交易规则、模拟盘、自选股买卖点账本 `picks.db` | `data/users/<用户名>/` |

全市场池按人复制既费盘也没意义，而且重复抓取会更快吃到东财的 IP 风控。

---

## 三、本地也要初始化一次

本地用法见仓库根 `README.md`「部署（本地 macOS · 四步）」节。

---

## 四、部署步骤

### 1. 买机器

2 核 2G 够用，系统选 Ubuntu 22.04（20.04 也行，deploy.sh 会自动装 python3.10）。三个 systemd
service 各自的内存上限（`deploy/*.service` 里的 `MemoryMax`）：web 1200M、scheduler 600M、
news 400M（news 是 oneshot，每天五次短跑，不常与另外两个同时顶格）。换 4G 机器的话可以把
这几个 `MemoryMax` 都放大一倍。盘至少 40G：新闻库和板块日线是按天累积的。

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
设权限、装 systemd 单元（三个 service 加一个 timer）、配 ufw、自检：

- `astock-web`：gunicorn。
- `astock-scheduler`：公共预热（新闻库/全市场池/复盘）、每日 housekeeping（过期会话/旧用量/
  舰队库清理）、常驻的三周期选股与各账号自选股买卖点定时批（`picks_pipeline.loop_forever`，
  交易日 16:00 全量、09:05 公共短线；公共三周期跑在站长上下文、记 `fleet` 桶）；
  agent 自动跑默认关（`ASTOCK_AGENT_AUTO=0`）。
- `astock-news.service`（oneshot）+ `astock-news.timer`：定时触发 `fetch_news.py` 做新闻库增量抓取，
  时刻同本地 launchd：08:40 / 11:40 / 14:00 / 15:30 / 20:30，非交易日只有晚间那次真抓。

幂等，可重复跑。系统 python3 低于 3.10（Ubuntu 20.04 是 3.8）时用 Miniconda（清华镜像）在 .venv 位置建 3.10 环境，不动系统 python；pip 源用 ASTOCK_PIP_INDEX 指定。

权限规则集中在 `deploy/fix_perms.sh` 头注释，`deploy.sh`、`push.sh` 和 MCP 的 `astock_push`
都调这一份脚本，三处不会各改各的。

三个 service 单元（web / scheduler / news）的 `ReadWritePaths` 都只放 `/opt/astock/data`
（AI 缓存 `ai_cache.json` 也在 data/ 下），文件系统其余部分对进程只读。

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

第一个管理员就是舰队站长，服务已经在跑也不用重启（最多 1 分钟内认出）。站长尚未确定时
（`ASTOCK_FLEET_OWNER` 留空且还没建出管理员），`/api/agents` 相关路由返回 503 而不是
500，建号后最多 60 秒恢复。迁移脚本用
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
sudo tailscale up --hostname astock      # 浏览器打开它打印的链接授权（用 GitHub/Google 账号即可）
sudo tailscale set --accept-dns=false    # 阿里云必做，见下
sudo tailscale set --netfilter-mode=off  # 阿里云必做，见下
sudo tailscale serve --bg 5000           # 首次会提示去后台开启 Serve/HTTPS，点一下再重跑
tailscale status                         # 记下 https://astock.<tailnet>.ts.net
```

阿里云上有两个必须改的默认值（2026-09-16 实测，不改的话整机 DNS 断掉、复盘和 AI 全部失败）：
阿里云的内网 DNS、元数据、镜像源都在 `100.100.x.x`，落在 Tailscale 认领的 CGNAT 段 `100.64.0.0/10` 里。
Tailscale 默认会在 iptables 加一条「来自 100.64.0.0/10 但不是从 tailscale0 进来的包一律丢弃」的防伪造规则，
阿里云 DNS 的应答正好被它丢掉。`--netfilter-mode=off` 让 Tailscale 不碰 iptables（本机也不用 ufw，见上），
`--accept-dns=false` 让服务器继续用系统解析器（服务器自己不需要解析 `*.ts.net`）。
这两个设置写进 tailscaled 的状态文件，重启不丢；`tailscale debug prefs` 里 `CorpDNS: false`、`NetfilterMode: 0` 即生效。

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

以 `astock` 身份跑的原因、备份目录、文件权限见 `deploy/backup.sh` 头注释。
默认保留最近 14 天、目录 `/var/backups/astock`，可在 `.env` 里用
`ASTOCK_BACKUP_KEEP`／`ASTOCK_BACKUP_DIR` 覆盖。本机快照挡不住"机器被删"和
"误删整个目录"，有条件请配 OSS 异地：

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
  调用，`ASTOCK_AI_GLOBAL_DAY` 要把这部分算进去。三周期选股每个交易日固定 4 次 DeepSeek
  调用记在 `fleet` 名下（公共池 16:00 全量三周期 3 次 + 09:05 早盘短线 1 次，跑在站长上下
  文），另外每个活跃用户每天 2 次记在自己名下（16:00 和 09:05 各一次自选股买卖点提示词，
  占个人日额度而不是全站预算）；手动点刷新若刚好撞上大盘研判缓存冷启动，还会多算一次 `market_overview`。
  16:00 和 09:05 这两次到点批跑本身也要读一次大盘研判（在站长上下文里取，供选股参考），
  5 分钟的大盘缓存冷了的话同样会多算一次 `market_overview`，记 `fleet` 名下、只吃全站预算。

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
