# 多用户 + 服务器部署：接线与修复计划（2026-09-16）

状态：执行中。分支 `multiuser`。本文是这轮改造的唯一设计文档，做完后精简为「机制说明」并入 CLAUDE.md。

## 背景

2026-09-11 写了一套「登录 + 按人隔离个人数据 + 限流 + gunicorn + 独立调度进程 + 阿里云部署脚本」，
以未跟踪文件形式放在工作区（auth.py / userctx.py / ratelimit.py / scheduler.py / astockctl.py / wsgi.py /
templates/login.html / deploy/），并用 deploy/patch_multiuser.py 描述对 16 个 tracked 文件的改动。
2026-09-16 接手时用两个隔离 worktree 验证：补丁锚点全部匹配、打补丁前后 14 个离线测试全过、
import app/wsgi/scheduler 零网络零报错。但读图发现两个 P0 和一批 P1（见「问题清单」），
且它静默推翻了 CLAUDE.md 里三条用户决定。

## 用户 2026-09-16 的三个决定

1. 继续推进：开 multiuser 分支，正式提交 16 个文件的改动（不再以补丁脚本形态存在），修完 P0 再合入 main。
2. agent 舰队全站只有一套，由站长（owner）管理，其他账号只读它的战绩、教训、house-view。
3. 服务器上 agent 不自动跑（保留「不开 app 就不炒股」）。服务器只跑 web、复盘、公共数据回填；
   agent 只在站长手动触发时跑。本地 `python3 app.py` 维持原来的「开着 app 就每桶自动跑」。

## 设计

### D1 舰队 = 站长的数据，所有舰队代码路径在站长上下文里跑

不把 agents.db/paper.db/profiles.db 挪到公共目录。原因：一次舰队运行还要读 profile_store（agent 档位
1/8/9/10 的费率）、rules_store.for_ai()（站长的交易规则）、_lesson_block()，把它们全挪成公共会让朋友的
规则/画像改动影响站长的舰队。改为：

- `userctx.fleet_uid()`：站长 uid。来源优先级：环境变量 `ASTOCK_FLEET_OWNER`，否则 `auth.init()` 时
  取 users 表里 created_at 最早的管理员并 `userctx.set_fleet_uid()`。都没有则返回空串。
- `userctx.as_fleet()`：`as_user(fleet_uid())` 的快捷方式。fleet_uid 为空时个人 store 会抛 RuntimeError，
  三个只读视图捕获后返回空块（与「journal 空时空块」一致）。
- 用 `as_fleet()` 包住的地方：
  - ai_blocks：`_lesson_block`、`_stock_house_view`、`_regime_view`（只读，任何用户的请求里都看站长舰队）。
  - app.py：所有 `/api/agents...` 路由；GET 对所有登录用户开放，POST（建/改/删/run/run_all）只允许
    管理员或 uid == fleet_uid（返回 403）。run/run_all 起后台线程前先进入 as_fleet，userctx.Thread 会带过去。
  - app.py `_agent_tick`（本地开发模式的盘中调度器）。
  - scheduler.py：agent 自动跑（默认关）与 agent 库清理。
- 其它账号自己目录里的 agents.db 保持为空，永远不被舰队代码读到。

### D2 线程池传上下文（P0 之一）

`userctx.ctx_map(pool, fn, items)`：对每个 item 在调用线程 `contextvars.copy_context()` 后 submit，
返回结果列表（顺序同 items，异常向外抛，与 `list(ex.map())` 语义一致）。
替换 agent_loop.py 两处 `ex.map`（run_all 的 agent 池、debate 的 bull/bear 池）。
app.py 的 6 个池只调 datasources，不改。

### D3 计费点挪到真实 LLM 调用（P0 之二 + 三个 P1 一起解决）

现状按「HTTP 请求次数」在 before_request 里扣，导致 run_all 计 1 却跑 20 次、GET 路由不计、
缓存命中也扣、调度器预扣。改为「1 单位 = 1 次真实 DeepSeek 调用」：

- `ratelimit.check()` 对 ai 桶只做门（每分钟总请求、最小间隔、全站与个人日预算是否已满），不再 `_bump`；
  heavy 桶维持按请求计数。
- `ratelimit.allow_llm(uid) -> tuple[bool, str]`：全站日预算 + 个人日预算（uid 为空视作 `system`，只受
  全站预算约束）。`llm._chat` 发请求前调用，不放行则抛 `LLMError`。这让预算对调度器、agent、GET 路由
  全部生效，README-deploy 里「全站预算同时约束定时任务」从此为真。
- `ratelimit.record_ai_call(uid, model)`：成功返回后 `_bump(day, uid or "system", "ai", model)`；
  与 token 记账一起在 `llm._record_usage` 里做。失败/超时/缓存命中不计。
- 删除 `ratelimit.consume()`；scheduler 不再预扣。
- `_today()` 与所有日期键改用 `ZoneInfo("Asia/Shanghai")`（与 agent_loop 一致）。
- AI_PREFIXES 去掉 `/api/rules/scenario`（零 LLM），`/api/news/deepen` 挪到 HEAVY_PREFIXES。
- `astockctl.py` 开头 `import config` 以加载 .env（否则 usage/status 打印的是代码默认值）。

### D4 ai_cache 键带用户

`ai_cache._key` 加入 `userctx.get_uid() or "-"`。提示词里注入了当前用户的画像/笔记/费率，缓存不能跨人命中。
文件仍是一份 ai_cache.json。

### D5 auth 加固（全部小改）

- POST /login 也做 Origin/Referer 校验（把校验挪到 PUBLIC 早退之前，只对非 GET 生效）。
- 登录失败计数同时按 uid 和按 `ip:<ip>` 两个键锁定（同阈值），不存在的用户名只计 ip 键。
- 账号停用时先做哈希再返回通用文案「用户名或密码不对」，不再单独暴露「已停用」。
- sessions.token 存 sha256 十六进制，cookie 里是原 token；查询按哈希。
- `session_user` 的滑动续期只在 last_seen 超过 10 分钟时才 UPDATE。
- `teardown_request` 里 `userctx.set_uid("")`，避免 gthread 线程复用把 uid 带到下一个请求。
- `COOKIE_SECURE` 默认值改为 `ASTOCK_ENV == "production"`；deploy/env.example 设 `ASTOCK_ENV=production`。
- 前端：注入 index.html/review.html 的角标脚本里包一层 `window.fetch`，遇到 401 且 code 为 auth_required
  跳 `/login?next=`，遇到 429 在角标处显示原因 5 秒。

### D6 公共任务在无用户上下文里的两处崩点

- news_store.backfill / fetch_incremental 用到自选股：新增 `store.load_all_watchlists()`（遍历
  data/users/*/watchlist.json 取并集）。news_store 在 `userctx.get_uid()` 为空时用并集，否则用当前用户的。
- scheduler housekeeping 的 agent_store.purge 只在 `as_fleet()` 里做一次，不遍历账号。

### D7 复盘跨进程互斥

`_review_job` 是进程内字典，生产拓扑（2 gunicorn worker + scheduler）互不可见。
review/pipeline.py 增加文件锁 `data/review/.running-<date>`（`os.open` 加 `O_CREAT|O_EXCL`，
内容写 pid 和时间，超过 30 分钟视为陈旧可覆盖，结束时删除）。`pipeline.is_running(date)` 供
app.py 的 /api/review/status 合并显示。两个进程同时起步时后者拿不到锁直接返回「已在生成」。

### D8 scheduler.py 收敛

保留：公共预热（news 并集回填、_universe_boot、_review_boot）、每日 housekeeping（会话/用量清理 +
as_fleet 下 agent_store.purge）。agent 自动跑改为 `ASTOCK_AGENT_AUTO=1` 才启用，且只在 `as_fleet()` 里
跑一遍、不遍历账号、不预扣额度（预算由 D3 在调用点管）。默认关。主循环空转等待，systemd 以进程存活判定。

### D9 本地开发模式

app.py `__main__`：在 `as_fleet()` 里起 `_agent_scheduler` 守护线程（恢复「开着 app 就自动跑」）。
`_universe_boot` 里不再起它（补丁已删，保持），这样 scheduler.py 复用 `_universe_boot` 时不会顺带跑 agent。
本地首次使用需要两条命令（写进 README）：
`python3 astockctl.py adduser <你> --admin` 和 `python3 deploy/migrate_to_multiuser.py <你>`。

### D10 东财节流跨进程

datasources.em_get 的节流时间戳从模块变量改成 `data/.em_last_call` 文件加 fcntl 排他锁，
让 3 个进程共享同一个最小间隔。逻辑不变，只是状态落盘。

### D11 deploy/ 与 MCP

- 泄露清理：公网 IP、登录名、本机绝对路径全部改成占位符（`<你的公网IP>`、`<你的登录名>`、`<仓库路径>`）。
  在提交任何新文件之前做，保证泄露不进 git 历史。
- 两个 systemd unit 加 `EnvironmentFile=-/opt/astock/.env` 与 `Environment=TZ=Asia/Shanghai`；
  deploy.sh 加 `timedatectl set-timezone Asia/Shanghai`；部署完成后 `chown -R root:root /opt/astock/deploy`
  并 755，切断「应用被攻破后改 deploy.sh 再以 root 执行」的提权链。
- deploy.sh 依赖改为 `pip install -r requirements.txt -r deploy/requirements-server.txt`（后者只有 gunicorn）。
- harden_ssh.sh：改完后用 `sshd -T | grep -i passwordauthentication` 校验生效值；drop-in 文件名改为
  `00-astock-hardening.conf`，并在存在 `50-cloud-init.conf` 时提示。
- push.sh：推送前检查 `grep -q "import userctx" app.py`，不通过拒绝推送。
- bootstrap.sh：删除「本地打补丁」步骤（改动已提交）。
- deploy/patch_multiuser.py 应用后删除；migrate_to_multiuser.py 保留。
- README-deploy.md：删掉补丁步骤、手写的路由数/文件数/磁盘数；补充舰队站长、`ASTOCK_AGENT_AUTO`、时区、
  本地两条初始化命令、部署前东财端点探测清单；把装饰箭头改成文字。内存建议改为「2 核 4G」或把两个
  unit 的 MemoryMax 调到合计不超过 2G，二选一并说明。
- MCP server：grep 参数前加 `-e`，且拒绝以 `-` 开头；`admin` 只有 JSON 布尔 true 才加 `--admin`；
  ssh 命令里 HOST 前加 `--`；审计日志以 0600 创建、执行前先写一条；adduser rc 非零不落密码文件；
  docstring 里的绝对路径改占位符。

### D12 测试（离线、零依赖，沿用 `python3 tests/xxx.py`）

- tests/test_userctx.py：as_user 嵌套恢复、ctx_map 传播、user_path 拒绝路径穿越、fleet_uid 三级回退。
- tests/test_auth.py：临时 DB_PATH；建用户/登录/错密码锁定（uid 键与 ip 键）/停用账号通用文案/
  会话哈希落库/teardown 后 uid 清空/跨站 POST /login 被 403/`/api/*` 未登录 401。
- tests/test_ratelimit.py：临时 DB_PATH；check 对 ai 桶不计数、record_ai_call 计数、allow_llm 全站与
  个人上限、system 只受全站约束、日期键为 Asia/Shanghai。
- tests/test_review_lock.py：锁获取/陈旧覆盖/释放。

### D13 文档

CLAUDE.md 重写文件地图、部署节、冒烟测试（curl 需先登录拿 cookie）、当前状态；README.md 部署节加
本地两条初始化命令；framework.md 加一句多用户与进程拓扑。旧的「本地零配置」叙述删除，不保留历史。

## 明确不做（本轮）

- 每人各自的 DeepSeek key。
- 2FA、自助改密页面（改密走 `astockctl.py passwd`）。
- 服务器上真机验证东财端点（需要用户的 ECS，列为部署前检查项）。
- datasources.py 808 行超 800 硬限（存量，另开任务）。
- 把 .venv 换成 conda：服务器 systemd 单元用 .venv 更省事，作为对 conda 硬偏好的明确例外记入 CLAUDE.md，
  用户可否决。

## 问题清单（来源：2026-09-16 读图工作流，8 个 agent）

P0
- agent_loop.py:1019 与 :159 裸 ThreadPoolExecutor 丢用户上下文，补丁后每个 agent 都失败。
- scheduler.py:76 每 5 分钟无条件预扣 20 次 AI 额度，35 分钟耗尽全站预算。
- deploy/mcp/README.md:73-74、deploy/README-deploy.md:122、deploy/push.sh:7 含真实公网 IP 与登录名；
  四处本机绝对路径。

P1
- ai_cache 键不含用户；GET /api/market/overview 不计费；/login 无 CSRF 校验；登录锁只按用户名；
  news_store.backfill 在无用户上下文里崩；复盘可跨进程双跑；ratelimit 用本地时区；push.sh 无守卫；
  MCP grep 选项注入与 root 提权链；.env 的 ASTOCK_* 在 systemd 下不生效。

## 提交顺序

1. `chore(deploy): 新增多用户/部署脚本（已清理泄露信息，未接线）`
2. `refactor(multiuser): 应用 patch_multiuser 到 16 个文件`
3. `feat(multiuser): 舰队站长上下文 + 线程池传播 + 调用点计费`（D1 D2 D3 D4）
4. `fix(auth): 登录 CSRF/IP 锁/会话哈希/teardown`（D5）
5. `fix(multiuser): 公共任务无用户上下文 + 复盘跨进程锁 + 东财跨进程节流`（D6 D7 D10）
6. `refactor(scheduler): 收敛为 web 外定时任务，agent 自动跑默认关`（D8 D9）
7. `chore(deploy): 部署脚本/单元/MCP 修复，删除补丁脚本`（D11）
8. `test(multiuser): auth/userctx/ratelimit/review 锁 离线测试`（D12）
9. `docs: CLAUDE.md/README/framework 同步多用户与进程拓扑`（D13）

## 验证

- 全部 tests/test_*.py 通过（含新增 4 个）。
- `python3 -c "import app, wsgi, scheduler"` 零网络。
- 本机 `python3 app.py`（5001 端口）：未登录 GET / 302 到 /login；adduser 后登录拿 cookie；
  带 cookie 的 /api/config、/api/agents 正常；跨站 POST /login 403。
- 本机 `gunicorn -c deploy/gunicorn.conf.py wsgi:application` 起两 worker + `python3 scheduler.py`，
  同样的登录闭环，且 /api/review/status 能看到 scheduler 侧的运行状态。
- `python3 -c "import agent_loop as al, userctx; ..."` 在 as_fleet 下跑 `run_day(18, dry_run=True, force=True)`
  不再报「当前上下文没有用户」（需要 DeepSeek key；无 key 时只验证到达 LLM 调用点）。
