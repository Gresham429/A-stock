# 部署到阿里云 · 多人共用

这份文档配套 `deploy/` 下的脚本。先说清楚在防什么，再说怎么做——不然每一步
都只是"照着敲"，出了问题不知道哪层塌了。

---

## 一、原样上线会出什么事

改造前这个项目的形态是：Flask 自带开发服务器监听 `127.0.0.1:5000`，**零鉴权**，
70 个 API 全部裸露，所有数据全局单份。搬到公网 IP 上会发生四件事：

| 风险 | 具体是什么 | 这套方案怎么挡 |
|---|---|---|
| **API key 被烧光** | `/api/recommend/daily`、`/api/agents/run_all` 等 10 个接口无限制触发 DeepSeek。`v4-pro` 是推理模型，一个循环脚本一夜能把余额清零，而且你第二天才发现 | `ratelimit.py`：每人日限 + **全站日预算** + 最小调用间隔。计数落 sqlite，多 worker 下依然精确 |
| **任何人可读可写** | 阿里云公网 IP 上线几分钟内就会被扫到，5000 是重点端口。持仓、笔记、模拟盘全部可读可删 | `auth.py`：`before_request` 全局闸门，**默认全关**、白名单只有 `/login` `/static` `/healthz`。新增路由不会因为忘了加装饰器而裸奔 |
| **数据串台** | `watchlist.json`、`portfolio.json`、`data/*.db` 都是全局一份，朋友加的自选股会出现在你的看板，他的持仓成本你看得见 | `userctx.py`：个人数据隔离到 `data/users/<用户名>/`，公共数据（行情/新闻/全市场池/因子）继续共享 |
| **进程被拖死** | Werkzeug 开发服务器无并发控制、无超时、暴露版本号；四个后台线程跟着 web 进程跑，多 worker 会重复决策、重复下单 | gunicorn（gthread）+ 定时任务拆成独立的 `scheduler.py` 单实例进程 |

再叠一层网络隔离：**服务不上公网**，走 Tailscale 私有组网。公网上这台机器只有
22 端口，看板本身从互联网上完全扫不到。

> 组网和登录是两层，不能互相替代。组网挡的是陌生人；登录挡的是"朋友的笔记本
> 丢了"和"tailnet 里某台设备被入侵"。少哪层都不行。

---

## 二、改造做了什么

新增 7 个文件，改动 16 个：

```
新增
  userctx.py       当前用户上下文 + 个人数据目录解析 + 跨线程传播
  auth.py          账号 / 密码 / 服务端会话 / 全局闸门 / 安全响应头
  ratelimit.py     按人限流 + AI 日预算 + token 记账
  scheduler.py     独立调度进程（agent 日循环 / 复盘 / 数据回填）
  wsgi.py          gunicorn 入口
  astockctl.py     账号与用量管理命令行
  templates/login.html

改动（由 deploy/patch_multiuser.py 自动完成）
  notes_store / paper_store / rules_store / agent_store / profile_store
      库路径改成按当前登录用户解析，保留 DB_PATH 作为测试覆盖钩子
  store.py / portfolio.py
      watchlist.json / portfolio.json 同样按人隔离
  news_store / universe_store / template_store / factor_lab
      路径不变（公共数据），连接改走 userctx.open_db 以开启 WAL
  app.py
      挂登录闸门和限流；个人库懒建表；agent 调度移出 web 进程；
      threading.Thread → userctx.Thread（会传递用户上下文）
  llm.py
      记录每次调用真实消耗的 token，便于归因
  templates/index.html · review.html
      页头加「当前账号 / 今日 AI 余额 / 退出」
  tests/test_agent_gates.py
      补上临时库隔离（顺带修掉它往真实 agents.db 写哨兵行的副作用）
```

### 为什么用"一人一个 sqlite"而不是"表里加 user_id"

加 `user_id` 列要改每一条 SQL、每一个函数签名、每一个调用点——在这个体量的
既有代码上风险很高，而且很容易漏掉一两处 `WHERE`，那种漏法恰恰是最糟的
（数据串台且无声）。改路径解析只动了 5 个模块的 2 行。

在几个人的规模下，一人一个文件顺带还是优点：互不锁表、可以单独备份某个人、
删号就是删一个目录。

### 公共 vs 个人

| | 内容 | 位置 |
|---|---|---|
| 公共 | 新闻库、全市场池（40MB+）、因子回测、提示词模板、每日复盘 | `data/` |
| 个人 | 自选股、持仓、投资画像、私域笔记、交易规则、模拟盘、agent 与教训 | `data/users/<用户名>/` |

全市场池按人复制既费盘也没意义，而且重复抓取会更快吃到东财的 IP 风控。

---

## 三、部署步骤

### 0. 先在本地打补丁并验证

```bash
cd <仓库路径>
git checkout -b multiuser            # 一定要开分支
git add -A && git commit -m "改造前存档"

python3 deploy/patch_multiuser.py --check   # 先预演，看会改哪些文件
python3 deploy/patch_multiuser.py           # 真的改
git diff                                    # 逐处过一遍

for f in tests/test_*.py; do python3 "$f" >/dev/null || echo "失败: $f"; done
```

补丁是幂等的，重复跑会跳过已改过的文件。任何一个锚点对不上它会直接中止、
什么都不改——宁可不动，也不能改错一半。

### 1. 买机器

2 核 2G 够用（40MB 的 universe.db 加上回测，1G 会紧张）。系统选 Ubuntu 22.04。
盘至少 40G：`data/` 现在约 45MB，但新闻库和板块日线是按天累积的。

**安全组只放行 22，且限制来源 IP**（阿里云控制台 → 实例 → 安全组 → 入方向）：

| 方向 | 端口 | 授权对象 | 说明 |
|---|---|---|---|
| 入 | 22 | **你的家宽 IP/32** | 不要填 0.0.0.0/0 |
| 入 | 其余 | 全部拒绝 | 5000 **不要**开 |
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
    # IdentityFile ~/.ssh/id_ed25519     ← 确认自己的密钥文件名后解开这两行
    # IdentitiesOnly yes
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 10m
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

```bash
mkdir -p ~/.ssh/sockets && chmod 700 ~/.ssh/sockets   # ControlPath 需要

# 首次：建目录并把属主暂时给自己，好让 rsync 推得进去
ssh aliyun_ecs 'sudo mkdir -p /opt/astock && sudo chown $USER /opt/astock'
rsync -av --exclude .git --exclude data --exclude .venv --exclude __pycache__ \
      ./ aliyun_ecs:/opt/astock/
ssh aliyun_ecs 'sudo bash /opt/astock/deploy/deploy.sh'
```

`deploy.sh` 跑完会把属主改成不可登录的 `astock` 账号，之后你就**不能**再直接
rsync 进去了。后续更新一律用 `deploy/push.sh`（见下文「日常运维」），它会走
暂存区中转，不需要给 rsync 配免密 sudo。

`deploy.sh` 会装依赖、建不可登录的 `astock` 系统账号、建 venv、生成
`ASTOCK_SECRET_KEY`、设权限（`data/` 700、`.env` 600）、装两个 systemd 服务、
配 ufw，最后自检。幂等，可重复跑。

然后把本地 `.env` 里的 key 填到服务器的 `/opt/astock/.env`：

```bash
scp .env aliyun_ecs:~/astock.env
ssh aliyun_ecs 'sudo mv ~/astock.env /opt/astock/.env \
  && sudo chown astock:astock /opt/astock/.env && sudo chmod 600 /opt/astock/.env \
  && sudo systemctl restart astock-web astock-scheduler'
```

（先落到家目录再 sudo 搬过去——`/opt/astock` 属主已经是 astock 账号了。）

### 3. 建账号并迁移你的数据

```bash
ssh aliyun_ecs
cd /opt/astock
sudo -u astock .venv/bin/python3 astockctl.py adduser <你的用户名> --admin
sudo -u astock .venv/bin/python3 deploy/migrate_to_multiuser.py <你的用户名>
```

迁移脚本用 sqlite 的**在线备份接口**复制库（直接 `cp` 一个开着 WAL 的库会漏掉
未 checkpoint 的事务，拿到旧快照），原文件一个都不删。

给朋友开号——**没有注册页面，这是故意的**。注册入口是纯粹的攻击面，几个人的
规模下 ssh 敲一条命令就够了：

```bash
sudo -u astock .venv/bin/python3 astockctl.py adduser xiaoming --name 小明
# 直接回车会自动生成一个强密码并打印出来，只显示这一次
```

密码用微信发明文不太好。可以用一次性链接（如 privnote），或者当面告诉对方，
让他登录后自己改。

### 3.5 关掉 SSH 密码登录

公网开着 22 却允许密码登录，是比看板裸奔更常见的入侵路径——扫描器整天在爆破
常见用户名。**做这一步之前，先确认密钥能免密登录**：

```bash
ssh-copy-id aliyun_ecs     # 若还没传过公钥
ssh aliyun_ecs             # 必须能免密直接进来
```

然后在服务器上：

```bash
sudo bash /opt/astock/deploy/harden_ssh.sh
```

脚本有三层防呆，最坏情况也就是等十分钟自己恢复：

1. **改之前逐项体检**——authorized_keys 是空的、或者当前这条连接是用密码进来
   的，直接拒绝执行并告诉你缺什么；
2. **改完布下倒计时撤销**——用 `systemd-run` 起一个十分钟后自动还原的定时器，
   你验证通过后手动取消它才算数；
3. **用 reload 而不是 restart**——当前这条连接不会被踢掉，你始终有个活口。

按提示**另开一个终端**验证 `ssh aliyun_ecs` 能进，再回去执行
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
5000——**是真证书，浏览器不报警告，也不需要域名和备案**。这一条同时解决了
"没备案"和"自签证书一堆警告"两个麻烦。

朋友那边：手机/电脑装 Tailscale → 你在管理后台发邀请 → 他接受后直接访问那个
`ts.net` 地址。手机上可以"添加到主屏幕"，用起来跟 App 一样。

装好后建议把安全组的 22 也关掉，改用 `tailscale ssh`，那样这台机器在公网上
就是一个完全不响应的黑洞。

### 5. 备份

```bash
echo '30 23 * * * /opt/astock/deploy/backup.sh >> /var/log/astock-backup.log 2>&1' \
  | sudo crontab -
```

备份包里有 `.env`（含 API key）和所有人的持仓，所以文件权限是 600。
**本机快照挡不住"机器被删"和"误删整个目录"**，有条件请配 OSS 异地：

```bash
# 装 ossutil 并配好 config 后，在 .env 里加：
ASTOCK_OSS_BUCKET=你的bucket名
```

---

## 四、日常运维

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
bash deploy/push.sh
```

### 额度怎么调

`.env` 里改，改完 `systemctl restart astock-web`。建议先按人头宽松设，
观察一周 `astockctl.py usage` 的真实分布再收紧：

```
ASTOCK_AI_PER_USER_DAY=40    # 每人每天
ASTOCK_AI_GLOBAL_DAY=150     # 全站每天（余额的最后一道保险）
```

全站预算**同时约束定时任务**——`scheduler.py` 自动跑 agent 前也要过这道门。
否则就成了"限流只管人不管机器"，半夜 20 个 agent 自动跑照样能把余额掏空。

---

## 五、这套方案没覆盖的

说清楚边界，免得产生虚假的安全感。

1. **朋友本人是可信的**。登录之后他在自己的数据范围内能做任何事。如果要防
   "朋友故意刷爆你的 key"，现有的日限只能限制损失规模，不能归零——真要彻底
   隔离就得每人填自己的 key。

2. **没有审计日志**。`data/usage.db` 的 `calls` 表记了谁在什么时候调了哪个
   接口，但读操作没记。几个人的规模下够用。

3. **没做 2FA**。加 TOTP 大约几十行，但在 Tailscale 之后收益有限——攻击者得
   先进你的 tailnet。

4. **数据源风控**。服务器 IP 固定且请求密集，比家里更容易被东财限流。深挖偶发
   "无数据"是正常的，`ASTOCK_HEAVY_PER_USER_DAY` 就是为了让它不恶化。

5. **合规**。给朋友提供 AI 荐股结果，在国内可能触及证券投资咨询的持牌范围。
   项目 README 里的免责声明建议在登录页和每日推荐结果处都显式展示一次——
   登录页已经加了。真要扩大范围请先弄清楚这件事。

6. **`tailscale serve` 之外没有 nginx**。少一层意味着没有边缘限流和请求缓冲，
   但也少一个要维护、要配错的组件。tailnet 内不存在匿名扫描，这个取舍是划算的。
   真想加，在 gunicorn 前面按常规配一个反代即可，`ASTOCK_BIND` 不用改。

---

## 六、回滚

补丁全部作用在 git 工作区，没有数据库 schema 变更（个人库只是换了路径），
所以回滚是干净的：

```bash
git checkout main                 # 代码回到改造前
# 数据：data/users/<你>/ 下的文件拷回原位即可
cp data/users/<你的用户名>/watchlist.json .
cp data/users/<你的用户名>/portfolio.json .
cp data/users/<你的用户名>/*.db data/
```

因为迁移脚本用的是复制而非移动，原文件一直都在——只要你还没手动删。
