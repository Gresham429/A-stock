# CLAUDE.md — A股观察台 / A-Share Watchdesk

> 给未来会话（换 session）的项目上下文。读完这份即可无缝接手，不必重新摸索。
> **想先看整体框架/知识库导航** → 根 `framework.md`（一页架构总入口）。本文=开发权威细节。

## 这是什么

一个**本地运行**的 A 股看板：多股对比 + 点击深挖（含多周期行情图：分时+K线蜡烛(可选 MA5/10/20/60/120/240)）+ 顶部大盘研判条 + 全市场两级选股 + 持仓盈亏 + DeepSeek AI 推荐/建议（结果落盘缓存带时间戳）+ 近1年新闻/政策库 + 私域笔记 + 交易规则库（价格行为体系 + A股制度特性，可增删改、注入 AI）+ **投资画像本金分级玩法** + **公司叙事(做过/在做/要做)** + **AI 溯源与依据校验** + **全球宏观→板块指向** + 模拟盘 + 名词解释。
纯本地 Flask 后端代理各数据源，前端零构建（HTML+CSS+原生 JS）。**为什么不是托管网页**：深挖/加股票/调 AI/联网搜索都要实时外部请求，而托管型 Artifact 的 CSP 禁止一切外部请求，做不到。

2026-09-16 起（分支 `multiuser`）支持多人共用：登录、个人数据按人隔离到 `data/users/<uid>/`、
AI 日预算、gunicorn + 独立调度进程、阿里云部署脚本。本地单人用法不变，只多一次建号。
机制见下方「多用户与部署」节；部署步骤见 `deploy/README-deploy.md`。

## 复盘自动化模块（`/review`）

本会话(2026-08-15)新增的**第二个前端路由**，与选股看板 `/` 分栏、独立页面、顶部切换、**不共用主页**。
定位：**A股短线情绪「每日复盘」**——收盘后自动出一份盘面复盘，加速每日盘面研判。

- **现状 = 端到端可跑**：`review/` 包（`fetch`/`metrics`/`store`/`llm_review`/`pipeline`/`run_daily`/`backfill`）+ `/api/review/*` 路由 + `review.js` 真渲染。取数（打板四池+题材串+龙虎榜，东财 push2ex/同花顺，纯 urllib、内置节流）→ 8 类情绪硬指标（纯函数，`tests/test_review_metrics.py` 44 断言离线测）→ DeepSeek 裁判研判+文稿 → 落盘 `data/review/<date>.json` → 前端渲染。实测 20260814：涨停63 / 情绪档位「退潮」/ 837 字文稿。批处理入口 `python -m review.run_daily`（供收盘后定时器）。
- **AI 链 = 5 角色分析师 fan-out → 裁判 → 文稿**：`run_analysts` 5 角色（**情绪面 / 资金面 / 题材热点 / 龙虎榜游资 / 龙头梯队**，各读一面，`v4-flash`）→ `judge` 收敛成结构化「明日关注点」（`v4-pro`）→ `article` 文稿（`v4-pro`）。前端「分析师视角」区展示。⚠️**资金面 = 东财 `push2 clist` 板块主力净流入(f62)，实时快照无历史**——故 `pipeline` **仅在跑最新场次(`date=None`)时纳入**（历史重跑跳过，避免取到实时值=未来函数）；`fetch.sector_flow`。⚠️**东财 clist 对住宅 IP 偶发 `RemoteDisconnected` 封锁（PITFALLS#14，实测时开时封）**——`fetch._get_json` 已加退避重试；仍失败则**资金面自动降级、只出 4 角色**（clist 通时 5 角色）。**服务器不同 IP 需重测**。裁判保留「直读硬指标」降级路径（分析师全失败时）。
- **情绪周期已回填可用**：`python -m review.backfill 3` 爬**同花顺**涨停池近 3 个月（66 交易日）存 `data/review/history.json`（gitignore）。⚠️**东财打板池只有 ~15 交易日深度**（更早返回空），故情绪周期**单用同花顺深史**、**2 因子=涨停家数+最高连板**（`high_days_value>>16` 取连板；**炸板率因同花顺无炸板端点、不入周期**，仍单列在硬指标卡走东财近 15 日）。回填只补「既成事实」→ 无未来函数。
- **目标 pipeline**（收盘后定时批处理跑一次、落盘，前端只读 latest）：
  `取数+体检闸 → 纯算情绪硬指标(赚钱效应/晋级率/连板梯队/情绪周期/题材热点/亏钱效应/封板质量) → DeepSeek 5 分析师+复盘裁判 → 生成文稿 → 落盘 → /review 渲染`。九成是硬指标纯计算、秒出；AI 只在研判+文稿两块。
- **边界**：复盘=客观事实整理，只到板块层面、不荐个股、不给买卖点（与选股/agent 的建议口径分开）。
- **数据策略**：复用 A-stock 不封IP源 + `a-stock-data` 技能的打板端点（涨停/连板/炸板/跌停四池 + 昨日定稿 + 题材串），**不引 akshare**；指标数学与正确性闸（定稿vs实时/制度10-20-30cm/覆盖率）照搬成熟口径。**详细设计 + 数据缺口分析在本地 `复盘方案/`（不进公开仓库）。**
- **部署**：本地 `python3 app.py` 时由应用内每日调度 `app._review_scheduler`（守护线程，交易日 `REVIEW_AUTO_TIME` 默 18:30 到点自动跑一次、幂等靠 `_review_sched_state.last_auto`）+ 首启自动回填情绪周期 `_review_boot`（history<5 天则后台 `review.backfill.backfill(3)`）负责，两者在 `__main__` 起线程。服务器上这两个线程跑在 `scheduler.py` 进程里（gunicorn 不执行 `__main__`），生成前先拿 `data/review/.running-<date>` 文件锁，web 的 `/api/review/status` 能看到别的进程在跑（`running_elsewhere`）。服务器 IP 上东财打板四池是否被封仍未实测，`deploy/README-deploy.md` 2.5 节有探测脚本，部署完先跑。

## 快速开始

```bash
pip install -r requirements.txt                       # 只依赖 flask
python3 astockctl.py adduser <你> --admin              # 一次：建账号（首个管理员即舰队站长）
python3 deploy/migrate_to_multiuser.py <你>            # 一次：把根目录旧数据迁到 data/users/<你>/
python3 app.py                                        # http://127.0.0.1:5000，先登录
```

个人数据（自选股/持仓/画像/笔记/规则/模拟盘/agent）在 `data/users/<uid>/`，公共数据（新闻/全市场池/
因子/模板/复盘/用量/账号）在 `data/`，均已 gitignore。migrate 要在第一次登录前跑，否则登录会先建出
空库，migrate 就全部跳过（此时用 `--force`）。本地不要设 `ASTOCK_ENV=production`，否则 cookie 带
Secure、http 下登不上。

## 文件地图

**入口**：`app.py`(Flask 路由，75 个) · `templates/index.html`(选股 UI+CSS，路由 `/`) · `static/app.js`(选股前端逻辑) · **`templates/review.html`+`static/review.js`+`review/`(复盘自动化模块，路由 `/review`)** · `wsgi.py`(gunicorn 入口) · `scheduler.py`(web 外定时任务进程)

### 多用户与部署（2026-09-16 加）
| 文件 | 职责 |
|------|------|
| `userctx.py` | 当前用户 contextvar；个人数据目录解析 `user_path`/`shared_path`；跨线程传播 `Thread`/`spawn`/`submit`/`ctx_map`；舰队站长 `fleet_uid`/`as_fleet`/`in_fleet`（环境变量 `ASTOCK_FLEET_OWNER` 优先，否则 auth 注册的 resolver 每 60 秒惰性取最早管理员）；`open_db` 统一开 WAL |
| `auth.py` / `auth_password.py` | 账号表、服务端会话（存 sha256(token)）、`before_request` 全局闸门（默认全关，白名单 `/login` `/logout` `/healthz` `/static/`）、Origin 同源校验（回环来源信 X-Forwarded-Host）、安全响应头、`/login` `/logout` `/healthz` `/api/me`；密码 scrypt、失败按用户名与 ip 两键锁定 |
| `ratelimit.py` | 每分钟总请求、AI 最小间隔、AI 日预算（每人 + 全站）、heavy 桶按请求计数；`allow_llm`/`record_ai_call` 是真实计费点（见约定）；库 `data/usage.db`；`/api/usage` |
| `astockctl.py` | 账号与用量 CLI：`adduser`/`passwd`/`disable`/`enable`/`kick`/`users`/`usage`/`status` |
| `deploy/` | 阿里云部署：`bootstrap.sh`(首次一键) · `deploy.sh`(服务器侧幂等) · `push.sh`(更新) · `fix_perms.sh`(权限单一源) · systemd 单元 web / scheduler / `astock-news.timer`(每天五次 `fetch_news.py`，同本地 launchd) · `gunicorn.conf.py` · `backup.sh` · `harden_ssh.sh` · `migrate_to_multiuser.py` · `env.example` · `README-deploy.md`(步骤权威) · `mcp/`(让 Claude 桌面端操作服务器的 MCP server，六个固定动作) |

### 三周期选股与观点账本（2026-09-16 加，设计 `plan/2026-09-16-picks-ledger-design.md`）
| 文件 | 职责 |
|------|------|
| `picks_levels.py` | K 线算候选价位（纯函数），模型只能从候选里挑，返回后 `snap` 校验 |
| `picks_store.py` | 观点账本 `data/picks_public.db`（公共三周期）与 `data/users/<uid>/picks.db`（自选股）；修改链、改口规则强制、到期、结果贴回、两层记忆块 |
| `llm_picks.py` | 两个提示词（三周期选股 / 自选股买卖点）+ 返回按条校验 |
| `picks_pipeline.py` | 候选池（短线=复盘题材池+换手；中线=板块龙头+资金；长线=估值+财报）、运行、结算、文件锁、16:00 / 09:05 定时循环 |
| `picks_routes.py` | `/api/picks/*` Blueprint（public / watchlist / chain / run / run_public / status） |

**app.py 的共享辅助已抽出**(2026-07-16，消除 agent_loop 的 `import app` 循环依赖)：
`screening.py`(选股/形态初筛：`_screen_rows`/`_pa_score`/`_safe_kline`/`_safe_metrics`…) ·
`ai_blocks.py`(AI 注入块：`_tier_block`/`_fee_block`/`_lesson_block`/`_agent_blocks`/**`_stock_house_view`**…)。
app.py 用显式 import 带回名字，路由调用点与 `app._X` 可达性不变；agent_loop 直接 `import screening/ai_blocks`。

### 数据与池子
| 文件 | 职责 |
|------|------|
| `datasources.py` | 行情/指标/K线/财报/新闻/研报/龙虎榜/解禁 + `sina_all_stocks`(全A名单+快照,5527只/12s) + `index_quotes`/`market_breadth`(大盘) + `global_markets`(外围数值) + `concept_tags`(东财板块) |
| `universe_store.py` | **全市场池**(`data/universe.db`)：全A名单 + 板块归属(东财 slist 逐股回填~100min) + 板块日变化。`codes_of`/`sector_of`/`sectors_map`/`taxonomy`/`snapshot_daily`/`sector_ranking`/**`backfill_sector_daily`**(逐股日K补历史·`_agg_sector_payload` 与 live 共用口径) |
| `universe.py` | **降级为「精选龙头」fallback**(10×48×170)。`universe_store` 未就绪时兜底 + `is_leader` 标记 |
| `news_store.py` | L2 新闻库(滚动1年) + `is_trading_day`(动态节假日) |
| `notes_store.py` | L5 私域笔记(永久) |
| `store.py` / `config.py` | 自选股持久化 / 读 `.env` |

### 资金与交易
| 文件 | 职责 |
|------|------|
| `fees.py` | **交易成本单一事实源**：`FeeSchedule`(佣金[可议价可免最低]+印花千0.5[仅卖出]+过户万0.1) + `round_trip`/`breakeven_pct` + `for_ai()`。**费率存画像，不硬编码** |
| `profile_store.py` | 多档投资画像(`data/profiles.db`)：现金+风险偏好+**费率表**；5 档 TIERS 按总资产落档 + `RISK_GUIDE` + `block_for_ai` |
| `portfolio.py` | 持仓(按画像隔离 + 多笔 lot 模型)。**现金=可用现金**(买扣卖加) + `cash_reconcile` + 逐笔 `lots_view` + **`reduce`(减仓/卖出·FIFO 先进先出→现金加回+记已实现盈亏)** + **`realized`/`realized_total`(已实现盈亏流水·落袋净)** |
| `paper_store.py` | 模拟撮合(`data/paper.db`)：整手/涨跌停/T+1/**费率可传入**(agent 多账户各用各的) |

### AI 与进化
| 文件 | 职责 |
|------|------|
| `llm.py` | DeepSeek 8 个函数：daily/screen/position/entry/market_overview(v4-pro) + structure_note/company_profile/macro_digest(v4-flash) |
| `template_store.py` | **提示词版本化**(`data/templates.db`)：多版本+激活/**回滚** + 按版本聚合客观指标(引用有效性/schema) |
| `provenance.py` | AI 溯源与依据校验(仅 entry/position)：闭集信号字典 + `verify_basis`(✓/⚠) |
| `rules_store.py` | 交易规则库(`data/rules.db`，84 条，场景化启用，注入 AI) |
| `ai_cache.py` / `websearch.py` | L1 AI 输出缓存 / 博查联网(可选) |

### Agent 与因子
| 文件 | 职责 |
|------|------|
| `factor_lab.py` | **因子回测与失效监控**(`data/factors.db`)：`backtest`(299只×600日=162,014样本/20s，顺带产出 **`excess_dist` 超额分位分布**=判罪线唯一来源) + `rank_of`(超额→历史分位) + `summary`(IC/t值) + **`direction(cohort=)`动态定方向** + **`ic_cohort` 表 + `backtest_large`(大盘 cohort IC，按 Tencent 市值前 600 密采) + `scoring_directions(cohort)`**(cohort 无数据回退全池；打分对象是预筛后大盘池、方向可与全池反向) + `rolling_ic`/`decay_alert`/`flip_rate` + `refresh_if_stale`(顺带跑 backtest_large) + `backtest_stops`(止损网格)。⚠️ `codes_of()` 无 focus 按**代码号**排序非市值(PITFALLS#5b) |
| `agent_store.py` | **Agent 持久层**(`data/agents.db`)：`agents`/`runs`(原文90天·结论365天) / **`lessons`(闭集9类)** / `pending`(限价挂单) / `conditions`(止损) / `claims`(时段原子占位) / `equity` / **`entries`(建仓留痕+冻结分位`x20_pctile`)** / **`journal`(P2 情节记忆·append-only：`journal_add`/`journal_of`/`journal_for_code`/`journal_staple_outcome`)** |
| `agent_loop.py` | **日循环**：研判(`_market_block` 指数+K线结构+涨停跌停+板块强弱)→选股→**决策(可插拔 single/debate)**→风控(确定性硬门)→**挂单**→复盘(确定性失败检测)。`sweep_orders`/`sweep_conditions`/`current_slot` |
| `outcome.py` | **结果结算**(纯函数)：`forward_returns`(自成交价, 按**K线根数**数交易日) + `bench_returns` + `excess`(扣 beta)。地平线**引用** `factor_lab.HORIZONS`(5/10/20) 不复制。**只算不判罪** |
| `structure.py` | **K线结构摘要**(纯函数零网络)：`digest()` 出 MA5/20/60 + 近20日高低 + 最近3根 OHLC(带日期)；`fmt_stock`/`fmt_market` 成行喂 AI。**只给原料不下判断**——趋势由 AI 读均线自己判 |

### 测试（零依赖离线，`python3 tests/xxx.py` 直接跑；项目无 pytest）
`test_universe_store.py`(9) 板块解析 · `test_screen_branches.py`(7) 选股三分支+cohort方向 ·
`test_agent_gates.py`(10) agent 门+挂单 · `test_factor_lab.py`(6) 因子方向 ·
`test_factor_cohort.py`(6) cohort 方向(路由/回退/纯helper) ·
`test_structure.py`(12) K线结构摘要 · `test_outcome.py`(12) 结果结算 ·
`test_excess_dist.py`(14) 超额分布+判罪线 · `test_agent_memory.py`(15) 个体记忆+journal(P1/P2/P2c)+regime-view ·
`test_sector_backfill.py`(13) 板块聚合口径 ·
**`test_fees.py`(8) / `test_portfolio.py`(8) / `test_paper_store.py`(9) 钱数学(费率/现金/撮合)** ·
`test_review_metrics.py`(44) 复盘情绪硬指标 ·
`test_userctx.py`(52) 用户上下文/线程传播/站长解析 · `test_auth.py`(68) 登录闸门/锁定/会话/CSRF ·
`test_ratelimit.py`(61) 计费门与计数/ai_cache 键 · `test_review_lock.py`(47) 复盘文件锁 ·
`test_fleet_routes.py`(36) 舰队路由权限/自选股并集 ·
`test_picks_levels.py`(30) 候选价位纯函数 · `test_picks_store.py`(25) 账本改口规则/结果贴回 ·
`test_llm_picks.py`(13) 提示词返回校验 · `test_picks_pipeline.py`(32) 候选池/运行/结算/文件锁 ·
`test_picks_routes.py`(11) `/api/picks/*` 路由 —— **共 24 文件**

## 多用户与部署（2026-09-16，分支 `multiuser`；设计 `plan/2026-09-16-multiuser-deploy-plan.md`）

用户三个决定：继续推进并正式提交（不再以补丁脚本形态存在）；agent 舰队全站只有一套、归站长；
服务器上 agent 不自动跑（保留「不开 app 就不炒股」）。

- **用户上下文**：`auth._gate` 每个请求 `userctx.set_uid(uid)`，`teardown_request` 清空。个人 store
  （notes/paper/rules/agents/profiles/watchlist/portfolio）的路径在调用时解析到 `data/users/<uid>/`，
  无用户时 `require_uid()` 抛 RuntimeError（宁可 500 不串人）。公共库（news/universe/templates/
  factors/usage/auth）路径不变。后台线程一律 `userctx.Thread`/`spawn`/`submit`，线程池用 `ctx_map`——
  原生 `threading.Thread`/`ex.map` 拿不到用户，个人 store 会炸（PITFALLS#19）。
- **舰队 = 站长的数据**：agents.db/paper.db/profiles.db 不搬公共库（一次舰队运行还要读站长的规则与
  画像）。所有舰队代码路径进 `userctx.as_fleet()`：`app._fleet_route` 包住全部 `/api/agents*`（GET 对
  登录用户开放，写操作只允许管理员或站长，否则 403；站长未设置 503）、`_agent_tick`、
  `scheduler._run_fleet_agents`、ai_blocks 的 `_lesson_block`/`_stock_house_view`/`_regime_view`
  （任何人的请求里都看站长舰队，空则空块）。站长 = `ASTOCK_FLEET_OWNER`，否则最早创建的管理员
  （停用不换人；建号后最多 60 秒生效、不用重启）。管理员在舰队路由里建 agent 用的是站长的激活画像。
- **计费点在真实 LLM 调用**：`llm._chat` 发请求前 `ratelimit.allow_llm(uid)`（门故障也拒绝），成功后
  `record_ai_call`；缓存命中/失败/超时不计。舰队调用记 `fleet`、无用户记 `system`，都只受全站预算
  `ASTOCK_AI_GLOBAL_DAY`（默认 150，要把舰队一天 40+ 次算进去）；个人预算 `ASTOCK_AI_PER_USER_DAY`
  默认 40。`before_request` 的 `check()` 对 ai 路径只做门（频率、6 秒最小间隔、预算是否已满），
  `GET /api/market/overview?refresh=1` 也过这道门。新增会调 LLM 的路由不需要登记，计费自动生效。
  ai_cache 键带 uid（`macro`/`profile` 两类公开资料例外）。
- **三进程拓扑**：gunicorn（gthread，2 worker x 8 线程，只绑 127.0.0.1:5000，`max_requests=0`）
  + `scheduler.py`（公共预热、复盘、每日清理；`ASTOCK_AGENT_AUTO=1` 才在站长上下文跑 agent）
  + tailscale serve 终止 TLS。跨进程状态只靠文件：复盘 `data/review/.running-<date>`、东财节流
  `data/.em_last_call`、用户首次建库 `data/users/<uid>/.init.lock`（都是 fcntl 排他锁）。进程内字典
  （`_review_job`、`_user_inited`、ratelimit 的分钟窗与最小间隔）每个 worker 一份，只做「本进程视角」。
- **本地开发**：`python3 app.py` 在站长上下文起盘中调度器（开着 app 就每桶自动跑，与从前一致），
  不要同时再跑 `scheduler.py`。`ASTOCK_ENV=production` 时 `app.py` 拒绝直接启动。
- **对 conda 硬偏好的例外**：服务器 systemd 单元用 `/opt/astock/.venv`（无人值守的 nologin 账号下比
  conda 少坑）；本地仍 conda。用户可否决。
- **阿里云 + Tailscale 两个必改项**（2026-09-16 踩过，整机 DNS 断了一小时、18:30 复盘失败）：`tailscale set --accept-dns=false --netfilter-mode=off`。原因与说明在 deploy/README-deploy.md 第 4 节。这台机器是共用机（k3s/docker/nginx/游戏服），`deploy.sh` 用 `ASTOCK_UFW=0` 跳过 ufw，外围防线 = 阿里云安全组 + Tailscale。
- **三周期选股运行时**：交易日 16:00 全量跑一次（公共三周期记 `system`、各账号自选股各跑一次），
  09:05 只让公共池跑短线一个周期；自选股不分早盘晚盘，两个时刻都是同一份覆盖全部周期的完整
  提示词，不因为是早盘就收窄。手动点 `/api/picks/run` 走 ai 门、计入个人日额度。跨进程锁是两
  个文件：公共锁 `data/.picks-running-public`，个人锁 `data/users/<uid>/.picks-running`（各账号
  跑自己的一份，互不阻塞）。到点记录 `_last` 只在进程内存里，不落盘——`scheduler.py` 若在
  16:00 之后重启，会把当天的 full 槽当成没跑过，重新触发一轮（多打一轮 DeepSeek 调用，但账本
  改口规则会挡掉站不住的重复结论，不会出现数据损坏）。账本落公共库 `data/picks_public.db`
  （三周期）与 `data/users/<uid>/picks.db`（自选股）；改口规则在 `picks_store._enforce` 里强制，
  不留后门。

## 数据源 & 坑（改代码前必读）

优先级：能用**腾讯/新浪/mootdx**（不封 IP）就别用东财；东财仅用于其独有数据且走限流。

| 数据 | 源 | 备注 / 坑 |
|------|-----|----------|
| 实时行情/估值 | 腾讯 `qt.gtimg.cn`（GBK） | `tencent_quote` 必须含 `last_close`(f4)/`change_amt`(f31)，否则**当日盈亏恒为 0** |
| 波动率/资金流 | 新浪 MoneyFlow | 返回里带每日收盘价 `trade` → 波动率与资金流一份数据两用 |
| 日K线 OHLC | 新浪 `getKLineData` | 腾讯 `hqkline` 端点已失效（`code:11`）；用新浪 |
| 研报/龙虎榜/解禁 | 东财 `reportapi`/`datacenter` | 走 `em_get()` 串行限流（间隔≥1s） |
| 个股资金流 push2/push2his | 东财 | **部分住宅 IP 间歇封锁** → 资金流一律用新浪，别依赖 push2his |
| 全A名单 | 新浪 `hs_a` | ✅ 5527 只，`getHQNodeStockCount` 拿总数 + `getHQNodeData` 分页(80/页×70页并发)。**返回自带行情字段**，名单与板块统计共用 |
| 板块归属 | 东财 `slist`（逐股） | ⚠️ **东财按端点封 IP**：`clist`(批量出数)**封死**（带/不带代理、走/不走 `em_get` 均 RemoteDisconnected），`slist`(逐股)放行 0.2s。故只能逐股回填 ~100min。**别再试 clist** |
| 行业分类 | 新浪 `newSinaHy.php` | ❌ 仅 49 类、覆盖 3013/5527(**54%**)，表老旧，**已弃用** |
| 历史资金流 | 新浪 MoneyFlow | 🔒 **只给 30 天**（传 `num=260` 也只回 30）→ `_pa_score` 的 `net20` 分量**无法回测**、不参与打分。需 ≥600 天源才能补，见 `plan/BACKLOG.md` |
| 美元指数 / VIX | — | 🔒 **无可用源**：新浪外盘 `hf_DX` 实测返回空、VIX 无代码 → `macro_digest` 缺这两个关键风险指标。见 `plan/BACKLOG.md` |
| 财报三表 | 新浪 | `financial_summary` 取营收/归母净利 + 同比 |
| 新闻 | 东财个股新闻 + 财联社快讯 + 东财7×24 | 财联社走 v1 API + 本地签名（`md5(sha1(sorted query))`），零 key |

## AI 配置（DeepSeek + 博查）

- **DeepSeek**：`deepseek-v4-pro`（该账号**只有** v4-pro / v4-flash 可用）。是**推理模型**——`max_tokens` 同时覆盖「思考+正文」，太小会导致思考耗尽、正文返回空。已设 daily=8000 / position=5000 / screen=9000 / market_overview=6000，**别调小**。**温度统一 0.15**（`_chat` 默认值，求严谨理性、少发散）；`_DISCLAIMER` 强制「只据给定数据、不编造、不预测方向」。OpenAI 兼容 `POST /chat/completions`，支持 `response_format:{type:json_object}`，零 SDK（纯 urllib）。**官网核对(2026-08-15)**：`deepseek-v4-pro` 别名自动指向最新正式快照 **Pro-0813**（1M 上下文/384K 输出/推理模型），`deepseek-v4-flash`=Flash-0731 → **已是最新正式 pro，模型串无需改**；复盘模块接 LLM 时沿用同一套。
- **博查（可选，B 方案）**：`POST api.bochaai.com/v1/web-search`，Bearer 鉴权，body `{query,freshness,summary,count}`，成功响应 `webPages.value[].{name,url,siteName,snippet,summary}`，错误体 `{"code":"401","message":"Invalid API KEY"}`。
  - **变量名**：`.env` 里用 `BOCHAAI_API_KEY`（博查惯例，README/`.env` 以此为准）；`config` 也兼容 `BOCHA_API_KEY`（`os.environ.get("BOCHA_API_KEY") or os.environ.get("BOCHAAI_API_KEY")`）。
  - **到期提醒**：`websearch._classify()` 把 401→key 无效/过期、402/403/余额→余额不足、429→限流；`/api/websearch/status?probe=1` 主动探测；前端启动时探测，失效则顶部红条 + 芯片 `🌐联网⚠`。
- **密钥只在 `.env`（gitignore），源码零硬编码。** 后端**不能**调用 Claude 的 WebSearch（那是对话侧工具）——看板联网靠自己的源（A 新闻 API + B 博查）。

## 联网知识 A+B（AI 每次分析前先抓实时资讯）

- **A（免费默认开）**：`market_news_digest()` = 财联社快讯 + 东财7×24，喂进三个 AI 提示词。
- **B（可选）**：配了博查 key 时，`app._ai_web_context()` 追加博查搜索结果。
- 三个 llm 函数都有 `web_context` 参数；`/api/config` 暴露 `news_augment`/`web_search`；芯片显示 `📰新闻` / `🌐联网`。

## 约定（写代码前必读）

**红涨绿跌**（A股惯例）。**情景区间**=年化波动率反推，只描述幅度、不预测方向。
所有 AI 输出标「参考信号，不构成投资建议」。前端用系统字体栈（不加载 Google Fonts）、
图表纯内联 SVG、零外部依赖。

**盈亏两套口径，别混（2026-07-16 用户明确）**：
- **显示层（持仓中）= 持仓盈亏 = 毛 − 已付买入费**（对齐券商，`pnl_broker`）。**卖出费不算进主盈亏**
  ——持仓时还没离场。卖出费按**每支股单列**为「今日离场费」(`sell_fee_if_now`)，展开可见「落袋净」。
  别再把卖出费加回主盈亏（那样比券商更悲观、用户困惑）。
- **AI 决策层 = 保本涨幅走往返**（买+卖都算，`fees.breakeven_pct`）。因为买进这一笔**早晚要卖**，
  决策时不算卖出费会推荐赚不回手续费的价差。**两套口径服务不同目的，不要统一。**
- **已实现盈亏（卖出后）= 落袋净**（`portfolio.reduce` 记流水，2026-07-21 加）：`(卖额 − FIFO 成本)
  − 分摊的已付买入费 − 卖出费`。与「持仓盈亏（未实现）」并列显示，累计 `realized_total` 进资产总览；
  资产总览另有 **总收益卡 = 已实现 + 未实现**（`realized_total + pnl_broker`，前端聚合、非新口径）。
  **减仓 FIFO 先进先出**；「清仓」按钮只删记录不动现金（记错用），**真卖出走「减仓」**。

**异步渲染必须带请求令牌**：多个慢请求写同一 DOM 目标时，用单调计数器
（`recSeq`/`detailSeq`/`mktSeq`/`secSeq`/`agSeq`）——发起即 `++`，`await` 回来后
`if(gen!==seq) return` 丢弃过期响应。**新增此类异步入口时照做**。

**数据源优先级**：能用腾讯/新浪（不封 IP）就别用东财；东财仅用于其独有数据且走
`em_get()` 限流。**⚠️ 东财按端点封 IP，`clist` 已封死** —— 见 `plan/PITFALLS.md#14`。

**按日累积的表一律要有 `purge()`** + 写入路径自动调用 + 容量在前端可见
（`sector_daily` 曾无清理，10 年会涨到 820MB）。

**多用户下的三条硬约束**（2026-09-16）：新起线程/线程池用 `userctx.Thread`/`spawn`/`ctx_map`，
不用原生的；碰 agent 数据的代码路径进 `userctx.as_fleet()`；跨进程要共享的状态落文件加 fcntl 锁，
不放进程内字典。个人库与公共库的边界见「多用户与部署」节，新增 store 先决定归哪边。

**项目文档与代码注释不出现装饰符号**（用户 2026-08-25 规则）：新写内容不用箭头、对勾、感叹号类
emoji；存量文档里已有的不要成批替换。运行时输出字符串按同一口径处理。

### 🔴 改代码前必读：`plan/PITFALLS.md`

24 条**真踩过并付出代价**的坑（本会话新增 5 条：#0/#0a/#0a-2/#0a-3/#0b），按「会不会让你写出错的东西」排序。最要命的三类：

1. **凡是拍脑袋定的阈值，方向大概率是错的** —— 已被数据打脸 5 次（`_pa_score` 两个
   分量方向全反、止损线、教训阈值、"要加迟滞"的判断）。能验必验，验不了要明确
   标注为**纪律参数**。
2. **教训不得与因子数据矛盾** —— 否则系统在惩罚 AI 服从自己的数据，且污染 5 个 AI。
3. **补跑/回测必然引入未来函数**，除非补的是「既成事实」（日K low / 分时）。
   agent 决策**不能补**——重建信息环境必有泄漏。**LLM 回测同理不可信**（模型见过未来）。

### 各特性设计文档（详细实现见 `plan/`）

| 特性 | 文档 |
|------|------|
| ⭐ **Agent 逻辑地图（权威·先读，系统核心/量化基座）** | `plan/2026-07-18-agent-logic-map.md`（pipeline/门/调度/决策/订单/结算判罪/记忆/量化接入点/红线，端到端） |
| **索引：plan/ 全部文档** | `plan/README.md`（本会话加，按主题分组） |
| 全市场股票池 + 板块日变化 | `plan/2026-07-15-full-market-universe-design.md` |
| **盘中 agent 调度器**（长期挂机也每桶自动跑） | `plan/2026-07-17-intraday-agent-scheduler-design.md` |
| **板块走势历史回填**（补齐一个季度） | `plan/2026-07-17-sector-history-backfill-design.md` |
| multi-agent + 失败归因 + 提示词进化（含**个体记忆**两层） | `plan/2026-07-16-agent-evolution-design.md` |
| **决策数据面**（K线结构 + 真·大盘块） | `plan/2026-07-16-decision-data-plane-design.md` |
| **结果导向教训**（超额结算 + 判罪线来自分布） | `plan/2026-07-16-outcome-driven-lessons-design.md` |
| **agent 记忆重构 P1–P3**（共享底座 journal + 冻结分位 + house-view/舰队层） | `plan/2026-07-18-agent-memory-redesign.md` |
| AI 溯源与依据校验 | `plan/2026-07-14-ai-provenance-attribution-design.md` |
| 投资画像 · 本金分级 | `plan/2026-07-14-capital-profiles-templates-design.md` |
| 公司叙事 | `plan/2026-07-14-company-narrative-design.md` |
| 知识与缓存架构 L1–L5 | `plan/2026-07-13-knowledge-cache-architecture.md` |
| 卡在数据源的待办 | `plan/BACKLOG.md` |

### 关键机制速查

> 下面是速查条目；**agent 交易/学习闭环的端到端全景**见 `plan/2026-07-18-agent-logic-map.md`（权威）。

- **AI 注入链**：`_tier_block()`(本金档) + `_fee_block()`(交易成本) + `_lesson_block()`(历史教训)
  + `_macro_block()`(全球宏观) + `_ai_web_context()`(规则库/新闻/笔记/联网) → 前置注入 **5 个 AI**。
  agent 走 `_agent_blocks(ag, cash, total, n_pos)`——**档位跟账户的钱走、费率跟券商走**。
- **交易成本**（`fees.py`，单一事实源）：费率存**画像**（不硬编码）。用户券商**不免 5 元最低**
  （2026-07-16 电话确认，**此前文档误记为「免最低」**——用交割单反推 −94.02 才发现）。
  **有无最低佣金比费率本身更重要**：单笔低于分界(=5/费率)时佣金被 5 元顶起。
  用户单笔约 1400 元 → 无论万9 还是万2.5 **佣金都是 5 元**（都触最低）→ 保本涨幅**约 0.75%**
  （不是 0.232%），且**拆单成倍加佣金**（拆 2 笔 = 2×5 元）。AI 费率块已据此警告「一次建仓、别拆」。
  - **费率现状（2026-07-16 生效）**：画像 6 = **万2.5 + 5元最低**（印花/过户法定不可免）。
    现有两笔 002602 是万9 时买的，但两种费率对其 ~1430 元/笔都触 5 元最低 → 佣金均 5 元、
    对账 −94.02 不变。⚠️ **画像只存单一费率、无 as-of 快照**：小单因触最低无影响；
    单笔 >20000 元(万2.5 的 5 元分界)才吃到费率、且历史笔与新笔会同用当前费率（见待办逐笔快照）。
  - **agent 画像(1/8/9/10)维持「免最低」不改**（用户 2026-07-16 决定：先不动）。
    ⚠️ 后果：agent 的保本涨幅/`below_breakeven` 教训基于比用户更低的成本，**模拟盈亏偏乐观**、
    学到的经验对用户不完全适用。要改再和用户确认。
- **因子方向**（`factor_lab.py`）：`_pa_score` 的打分方向 + 教训记不记，全由
  `direction()` 决定——近60日 `|t|>2` 用近期、否则全样本、**都不显著则不参与打分（不猜）**。
  `refresh_if_stale()` 在启动时惰性重跑（14s）。
- **agent 三道门**：非交易日 → 交易时段(`require_open`) → **时段桶原子占位**(`claim_slot`)。
  时段桶=早盘/尾盘，**错过不补**。
- **盘中调度器**（2026-07-17，`app._agent_scheduler`）：日循环不再只在启动跑一次——守护线程
  **每 5 分钟**探一次 `run_all(require_open=True)`。**长期挂机、非交易时段启动 app 也能每桶自动跑**
  （此前凌晨开 app → 启动那次命中非交易时段跳过 → 当天再不触发，早盘桶空）。`claim_slot` 幂等
  保证每桶只真跑一次（多余 tick 秒返回、零 LLM）；`_agent_tick_lock` 单飞防并发叠加。
- **限价挂单**：AI 给 `limit_price`，`place()` 挂单不即时成交；`sweep_orders()` 用
  分时(当日)/日K(隔夜)判定触及，**成交价锁 limit 不取更优**。当日有效。
- **agent 记忆（2026-07-18 重构 P1–P3，共享底座「一份 journal、多个视图」）**：
  - **底座 = `agent_store.journal` 表（append-only、永不回改 → 结构上不会「变动」）**。每次决策(买/观望)入一行，
    含 `regime` tag + `signals` 快照 + `action` + **`summary`=当时写下的理由原话**(买 reason / 观望 skip_reason，
    **零新增 LLM 调用**)；结算时把 20 日超额 + **冻结分位** 贴回(`journal_staple_outcome`)。
  - **P1 稳定**：判罪分位 `entries.x20_pctile` **结算算一次即冻结**，战绩块/判罪读它、不再随 `factor_lab` 判罪线
    漂移重算(修「记忆总是变动」)；`for_ai`/`lesson_rollup` 加确定性平局键。
  - **视图（都从 journal/lessons 查，确定性检索）**：交易 agent → `_agent_journal_block`(自己近 8 条决策+理由+结果)
    + 自己教训 + **全舰队只读层**(`_lesson_block()`，市场真理该共享，标签区分「你本账户」vs「全体账户」→ 不趋同)；
    用户面深挖/持仓 → `_stock_house_view(code)`(全体 agent 对这只票的历史看法)。
  - **regime-view（P3 收尾，2026-07-18 完成 fee320f）**：`journal_for_regime` + `ai_blocks._regime_view` +
    `agent_loop.current_regime`，注入大盘研判/选股 AI，按「同类行情」回看全舰队战绩；journal 空时空块。
  - 战绩/journal **只喂事实原话、不让 agent 写事后反思**(反思=拟合噪音)。测试 `test_agent_memory`(15)。
  - ⏳ **未做**：舰队→提示词提炼(D，用户最终目标，等教训库有数据再做)。

## 冒烟测试（改完自测）

```bash
python3 tests/test_universe_store.py     # 板块解析 9 例（改 parse_tags 必跑）
python3 tests/test_screen_branches.py    # 选股三分支+cohort方向 7 例（改 _screen_rows/_pa_score 必跑）
python3 tests/test_agent_gates.py        # agent 门+挂单 10 例（改 run_day/幂等必跑）
python3 tests/test_factor_lab.py         # 因子方向 6 例（改 direction/打分必跑）
python3 tests/test_factor_cohort.py      # cohort 方向 6 例（改 direction cohort/backtest_large/scoring_directions 必跑）
python3 tests/test_structure.py          # K线结构摘要 12 例（改 structure/决策提示词必跑）
python3 tests/test_outcome.py            # 结果结算 12 例（改 outcome/地平线/超额必跑）
python3 tests/test_excess_dist.py        # 超额分布+判罪线 14 例（改判罪/分布必跑）
python3 tests/test_agent_memory.py       # 个体记忆+journal+regime-view 15 例（改 for_ai/战绩块/journal 必跑）
python3 tests/test_sector_backfill.py    # 板块聚合口径（改 _agg_sector_payload/snapshot_daily/回填必跑）
python3 tests/test_fees.py               # 费率数学（改 fees.py 必跑）
python3 tests/test_portfolio.py          # 持仓现金/盈亏三口径（改 portfolio.py 必跑）
python3 tests/test_paper_store.py        # 撮合规则 整手/涨跌停/T+1（改 paper_store.py 必跑）
python3 tests/test_review_metrics.py     # 复盘情绪硬指标 44 断言（改 review/metrics 必跑）
python3 tests/test_userctx.py            # 用户上下文/线程传播/站长解析（改 userctx 必跑）
python3 tests/test_auth.py               # 登录闸门/锁定/会话/CSRF（改 auth/auth_password 必跑）
python3 tests/test_ratelimit.py          # 计费门与计数/ai_cache 键（改 ratelimit/llm._chat/ai_cache 必跑）
python3 tests/test_review_lock.py        # 复盘文件锁（改 review/pipeline 必跑）
python3 tests/test_fleet_routes.py       # 舰队路由权限/自选股并集（改 _fleet_route/news_store/store 必跑）
python3 tests/test_picks_levels.py       # 候选价位纯函数 30 断言（改 picks_levels 必跑）
python3 tests/test_picks_store.py        # 账本改口规则/结果贴回 25 断言（改 picks_store 必跑）
python3 tests/test_llm_picks.py          # 提示词返回按条校验 13 断言（改 llm_picks 必跑）
python3 tests/test_picks_pipeline.py     # 候选池/运行/结算/文件锁 32 断言（改 picks_pipeline 必跑）
python3 tests/test_picks_routes.py       # /api/picks/* 路由 11 断言（改 picks_routes 必跑）
# 全部零依赖、离线、不打网络。共 24 文件。一行跑全部：
#   for f in tests/test_*.py; do python3 "$f" >/dev/null 2>&1 || echo "FAIL $f"; done
# 改 agent 记忆(journal/冻结分位/house-view)后：改 agent_store/agent_loop/ai_blocks → 跑 test_agent_memory + test_excess_dist。

# ⚠️ 改**决策提示词/数据面**后，必须实跑一个 debate 档（single 跑通≠debate 跑通，
#    辩论是 token 预算最短板；实测踩过两次）：
#    python3 -c "import agent_loop as al; print(al.run_day(18, dry_run=True, force=True))"
#    ✅ 2026-07-18 校验：把 agent 18 记忆塞到显示上限（journal 8/entries 12/lessons 10 类，
#    记忆块合计 ~8261 字符）后实跑 debate——裁判仍产出完整 JSON（intents+skip_reason，
#    输出仅 263 字），未截断。当前 8000/12000 预算对满记忆有余量，不必调大。

python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('*.py')+glob.glob('review/*.py')+glob.glob('tests/*.py')+glob.glob('deploy/*.py')]"
node --check static/app.js && node --check static/review.js
perl -e 'alarm 90; exec @ARGV' -- env NO_PROXY='*' python3 -c "import app, wsgi, scheduler"   # 零网络

python app.py &                          # ⚠️ 一律用 127.0.0.1 别用 localhost：
                                         #    localhost→::1→AirPlay 403（见 PITFALLS#18）
# 接口都要登录：先拿 cookie（本地 ASTOCK_ENV 不设 production，cookie 才不带 Secure）
curl -s -c cj.txt -H 'Origin: http://127.0.0.1:5000' -d 'uid=<你>&password=<密码>' 127.0.0.1:5000/login
curl -s -b cj.txt 127.0.0.1:5000/api/me                 # uid / is_admin
curl -s -b cj.txt 127.0.0.1:5000/api/usage              # 今日 AI 次数 / 全站预算
curl -s -b cj.txt 127.0.0.1:5000/api/config
curl -s -b cj.txt 127.0.0.1:5000/api/universe/status    # 池子：总数/eligible/板块回填进度
curl -s -b cj.txt "127.0.0.1:5000/api/sectors?kind=sw1&limit=5"
curl -s -b cj.txt 127.0.0.1:5000/api/factors            # 因子 IC/方向/翻转/新鲜度
curl -s -b cj.txt 127.0.0.1:5000/api/agents             # 站长舰队 + 教训汇总（未设站长 503）
curl -s -b cj.txt 127.0.0.1:5000/api/review/status      # running / running_elsewhere
# AI 类接口 30~90s → --max-time 200；有代理 → --noproxy '*'
# 服务器三进程冒烟（隔离副本里做，2026-09-16 实测过）：
#   gunicorn -c deploy/gunicorn.conf.py -b 127.0.0.1:5002 wsgi:application  +  python3 scheduler.py
```

## 安全（提交前必做）

- `.gitignore` 排除：`.env` / `.env.*` / `.claude/` / `.vscode/` / `__pycache__/` / `watchlist.json` / `portfolio.json`。
- **提交前铁律**：`git add -A` 后跑
  ```bash
  git ls-files --error-unmatch .env    # 必须报错(=未跟踪)
  git ls-files -z | xargs -0 grep -lE "sk-[A-Za-z0-9]{16,}"   # 必须无输出
  git diff --cached -U0 | grep -E '^\+[^+]' | grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|/Volumes/|/Users/' \
    | grep -vE '127\.0\.0\.1|0\.0\.0\.0|203\.0\.113'          # 公网 IP / 本机绝对路径：必须无输出
  ```
- 绝不把 key / 个人邮箱 / 本地绝对路径 / 服务器公网 IP / 服务器登录名写进被跟踪文件
  （2026-09-11 的 deploy 文档曾含真实 IP 与登录名，2026-09-16 入库前改成占位符；前两条 grep 拦不住
  这类泄露，所以加了第三条）。测试里的 IP 用 203.0.113.x 文档保留段。
- 远程 `git@github.com:Gresham429/A-stock.git`（main 分支，MIT，Conventional Commits）。`gh` 未登录，推送走 SSH（已配好）。

## 代码风格

- 小文件（目标 <400 行）、类型注解、模块级 `logger`、不要裸 `except`、不硬编码密钥。
- 所有东财请求走 `em_get()`（内置限流）；新数据源优先选不封 IP 的腾讯/新浪。

## 当前状态 / 待办

**2026-09-16：多用户版已合入 `main`（d3191ae 之后又有 5 个 deploy 修补 commit），并已部署到阿里云 ECS
（别名 `aliyun_ecs`，Ubuntu 20.04 共用机，Miniconda 3.10 环境在 `/opt/astock/.venv`），只能经 Tailscale 访问：
`https://astock.tail141314.ts.net`。站长账号 `gresham`，本地 data/ 全部迁上去了（新闻库/全市场池/因子/复盘/
20 个 agent）。web + scheduler + 新闻 timer（08:40/11:40/14:00/15:30/20:30）+ 备份 cron（23:30）都在跑，
agent 不自动跑。服务器 IP 上东财端点全通（含家里被封的 clist）。`main` 未推送 GitHub。
本地 `data/` 仍是旧布局：本地要用多用户版先 `adduser` + `migrate`（见「快速开始」）。
更新服务器：本地 `bash deploy/push.sh`（用 `~/astock-staging` 中转）。**

复盘模块（`/review`）2026-08-15 端到端建成并已合入 main：取数、8 类硬指标、5 分析师 + 裁判 + 文稿、
落盘、渲染、情绪周期回填、应用内每日调度。agent 舰队记忆闭环 P1-P3 于 2026-07-18 落地，
`journal`/教训库靠真实交易日填充（app 最后一次跑约 2026-08-17）。历史细节看 git log 与 `plan/`。

### 🔴 下一步 / 下个会话可直接执行的 backlog

**分三类：多用户收尾 · 需用户签字才改（下方标 🟡 的存量条目） · 卡时间（标 ⏳）。**
每做一项：补离线单测，跑全套件(19 文件)，若改后端则重启 app 验 boot，单独 commit 推送。**标 🟡 的未经用户确认不要改。**

**多用户收尾（2026-09-16 review 里判定可延后的项，都有明确修法）**
- 合并与推送：`multiuser` 分支 `git merge --no-ff` 到 main 后推送，用户定。
- 部署前在 ECS 上跑 `deploy/README-deploy.md` 2.5 的东财端点探测（打板四池 / push2ex / slist）。
- `ratelimit.allow_llm` 与 `record_ai_call` 非原子：并发下日预算可超出，上限是同时在飞的调用数
  （2 worker x 8 线程不超过 15 次）。严格版把读和加放进同一事务并在 record 时返回是否超限。
- `agent_loop.sweep_conditions` 跨进程非原子：两个管理员同时在不同 worker 点 run_all 可能重复补判
  条件单。修法 `agent_store.claim_condition(cid)`（`UPDATE ... WHERE status='live'` 按 rowcount）。
- 复盘流水线中途被预算门拒绝时，带「已达上限」文案的 stub 会落盘为 done，当天不再自动重跑
  （存量行为，`force=1` 可重跑）。修法：预算类 LLMError 不落盘。
- `run_all` 里某 agent 撞预算后其余 agent 仍逐个尝试（有界空转）。可用共享 Event 让后续直接 skipped。
- 登录失败按 ip 键锁定：NAT 共享出口时朋友连错 5 次会把同 IP 的站长也锁 5 分钟（设计取舍，
  README-deploy 已写明）。
- 大文件：`app.py` 1536 / `agent_loop.py` 1025 / `datasources.py` 877 行超 800 硬限。可先把
  `datasources` 的 `_em_*` 五个函数挪 `em_throttle.py`，`app.py` 的 `_fleet_route`/`ensure_user_stores`
  挪 `fleet_routes.py`。
- 仍无单测：`template_store` / `provenance` / `rules_store` / `profile_store` / `news_store` /
  `notes_store` / `astockctl` / `scheduler`。

🟡 **需用户签字才改（自主会话只分析列建议，别直接改）**
- **`sample_codes` 市值分层实为代码分层**（cohort 修复时挖出，PITFALLS#5b）：`codes_of()` 无 focus 按
  代码号排序、非市值 → 全池 IC/`excess_dist` 的样本是代码分层、非宣称的市值分层。**未修**（动它碰判罪线/
  冻结分位，敏感）。要修须重排样本 + 重跑分布 + 确认冻结分位不受影响。
- **阈值改动**：`CHASE_HIGH_POS` 85→80（只用于 detect_failures 教训门、被 `bad('range_pos')` 门控，
  非打分；85 是线性惩罚上的任意切点）；纪律参数见下。**建议保持 85**（降到 80 只多记追高教训）。

⏳ **卡时间（唯一主线，代码全就位）**
- **让 20 个 agent 攒数据**：记忆闭环(P1–P3)全落地但 `journal`/教训库仍空，要真实交易日填充。
  开 app 即自动跑(调度器)；**下个交易日早盘桶(9:30)自动跑**。第一批 20 日结算约 **2026-08-13**。
  ⚠️ 数据面好 ≠ 必成交：07-16/17 早盘 AI **正确**拒绝逆势→0 成交，尾盘转好才成交 10 笔。**别为攒数据松风控。**
- **舰队→提示词提炼(D，用户最终目标)**：等教训库有数据再做，见 `plan/2026-07-16-agent-evolution-design.md`。

### ⚠️ 已知未验证 / 未做

- **阈值验证结论（2026-07-18 已用 factor_lab 验，方向全对、无雷）**：
  `CHASE_HIGH_POS=85` 方向 ✓(range_pos IC t=-4.9 显著负)，但 85 是**线性**惩罚上的任意切点(可考虑 80)；
  `STOP_LOSS_PCT=-10` 是风险纪律的合理中点(止损网格：越紧均值收益越低；-10 封住尾部 −58%→−10 只让 0.3% 均值)；
  `LOSS_CUT_PCT=-12` 是 −10 后 2% 缓冲；`STALE_DAYS=20` 与评估地平线对齐。
  ⚠️ `vol`/`cum20` 近 60 日 IC 方向**翻转**（`direction()` 已自适应处理）。`_PRESCREEN=600` 覆盖分析
  (d4f749a)发现反转因子只在被丢的小盘有效、方向在保留的大盘翻转——**方向问题本会话已修**（cohort-aware
  打分，见上「本会话 #5」）；是否改 `_PRESCREEN` 本身（扩池纳入小盘）仍归 🟡。
- **纪律参数**（不需数据验证，但需用户认可）：`MAX_VOL=120`、`MAX_POS_PCT=30`、
  `MIN_CASH_PCT=10`、`VOL_FLOOR/CEIL=15/130`、`STOP_LOSS_PCT=-10`(已回测)、
  `LESSON_PCT=10`(教训判罪线=超额底部十分位，**用户 2026-07-16 已认可**，改前需重新征询；
  分布是 16 万样本的事实，「取底部 10%」是选择性取舍)。
- **测试盲区**：`fees`/`portfolio`(现金扣减)/`paper_store`(撮合) 已补测(58 断言)。
  仍无单测：`template_store` / `provenance` / `rules_store` / `profile_store` / `news_store` / `notes_store`。
- **`DebateDecider` 默认不启用**（UI 可选）：先用 single 拿基线，用数据证明需要再切。
- **launchd 定时全不挂**（用户决定）：agent「不开 app 就意味着那天不炒股」——但**只要 app 开着**，
  盘中调度器就每桶自动跑（无需在交易时段启动，2026-07-17 修）。服务器上同样不自动跑
  （`ASTOCK_AGENT_AUTO` 默认 0，2026-09-16 用户再次确认）。
  板块统计「每交易日都开 app，`_universe_boot` 已覆盖」；服务器上由 `scheduler.py` 覆盖。

### 🔒 卡在数据源（代码已就位，拿到源即接；用户要求提醒他加）

见 `plan/BACKLOG.md`：
1. **`net20` 资金分量无法回测** —— 新浪 MoneyFlow 只给 30 天，因子回测要 ≥600 天。
   故 `_pa_score` 四分量里唯独资金项未验方向、不参与打分。
2. **宏观缺 美元指数/VIX** —— 新浪外盘没有。接上只需 `ds.global_markets()` 加两条目。

### 本地数据文件（全部 gitignore）

公共 `data/`: `news.db` `universe.db` `factors.db` `templates.db` `auth.db` `usage.db` `review/`
`picks_public.db` `.em_last_call` `.picks-running-*`；根目录 `ai_cache.json`（键带 uid）。
个人 `data/users/<uid>/`: `watchlist.json` `portfolio.json`(按画像隔离+lot 模型) `notes.db` `rules.db`
`paper.db` `profiles.db` `agents.db` `picks.db` `.init.lock` `.picks-running`。舰队只读站长目录里的
`agents.db`/`paper.db`/`profiles.db`。
旧布局（根目录 `watchlist.json`、`data/agents.db` 等）由 `deploy/migrate_to_multiuser.py <uid>` 复制进
站长目录，原文件不删。

### 用户侧待办（我做不了）

- **佣金已落定(2026-07-16 生效)**：画像 6 = 万2.5 + 5元最低（券商拒免最低；印花/过户法定）。
  对用户单笔~1400 元不省（仍触 5 元最低）；单笔 >20000 元才吃到费率。已配置，无待办。
  - 可选后续：**逐笔费率快照**——费率随时间变，现画像只存单一费率。用户资金变大、单笔超
    5556 元后，历史笔与新笔费率不同才需要；小单因都触最低而无影响，暂不做。
