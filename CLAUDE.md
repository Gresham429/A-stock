# CLAUDE.md：A股观察台 / A-Share Watchdesk

给接手会话的项目上下文。读完这份就能开工，不必重新摸索。

文档分工（一题一份，原地重写，不叠历史）：

| 要找什么 | 去哪 |
|------|------|
| 上手、不变量、机制速查 | 本文 |
| 一页架构图与导航 | `framework.md` |
| 使用者手册、本地安装 | `README.md` |
| 服务器部署步骤与运维 | `deploy/README-deploy.md` |
| 各特性设计存档、索引 | `plan/README.md` |
| 真踩过的坑（改代码前必读） | `plan/PITFALLS.md` |
| 待办（唯一一份） | `plan/BACKLOG.md` |
| 现状数字：路由数、大文件、测试结果、plan 状态 | `python3 tools/status.py`（不手写） |

## 这是什么

A 股看板，Flask 后端代理各数据源，前端零构建（HTML + CSS + 原生 JS）。功能：多股对比与点击深挖
（多周期行情图、分时与 K 线蜡烛、可选 MA5/10/20/60/120/240）、顶部大盘研判条、全市场两级选股、
持仓盈亏、DeepSeek 推荐与建议（结果落盘缓存带时间戳）、近 1 年新闻与政策库、私域笔记、交易规则库
（价格行为体系 + A 股制度特性，可增删改、注入 AI）、投资画像与本金分级玩法、公司叙事、AI 溯源与依据
校验、全球宏观到板块指向、模拟盘、每日复盘（`/review`，板块层面 + 往下走一层的个股线索）、
三周期选股与自选股买卖点（观点账本）、
到点提醒（价格摸到买点/卖点/止损就推手机，一个账号可配多台，点位默认取 AI 分析、可自己改）、
20 个模拟盘 agent 组成的舰队（已暂停，代码保留作 backup）。

多人共用：登录、个人数据按人隔离到 `data/users/<uid>/`、AI 日预算、gunicorn + 独立调度进程、
阿里云部署脚本。本地单人用法不变，只多一次建号。生产实例只能经 Tailscale 访问：
`https://astock.tail141314.ts.net`。

为什么不是托管网页：深挖、加股票、调 AI、联网搜索都要实时外部请求，托管型页面的 CSP 禁止外部请求。

## 快速开始

```bash
pip install -r requirements.txt                       # 只依赖 flask
python3 astockctl.py adduser <你> --admin              # 一次：建账号（首个管理员即舰队站长）
python3 deploy/migrate_to_multiuser.py <你>            # 一次：把根目录旧数据迁到 data/users/<你>/
python3 app.py                                        # http://127.0.0.1:5000，先登录
```

个人数据（自选股、持仓、画像、笔记、规则、模拟盘、agent、自选股买卖点）在 `data/users/<uid>/`，
公共数据（新闻、全市场池、因子、模板、复盘、公共三周期、用量、账号）在 `data/`，均已 gitignore。
migrate 要在第一次登录前跑，否则登录会先建出空库，migrate 就全部跳过（此时用 `--force`）。
本地不要设 `ASTOCK_ENV=production`，否则 cookie 带 Secure、http 下登不上。

## 文件地图

### 入口与进程

| 文件 | 职责 |
|------|------|
| `app.py` | Flask 路由与应用内调度（本地 `python3 app.py` 时起盘中调度器、复盘调度、公共池预热） |
| `templates/index.html` + `static/app.js` | 选股看板，路由 `/` |
| `static/theme.css` + `static/theme.js` | 三个页面共用的浅色主题与移动端适配（默认浅色，右上角可切深色）；项目约定见「约定」节 |
| `templates/review.html` + `static/review.js` + `review/` | 复盘模块，路由 `/review` |
| `templates/login.html` | 登录页 |
| `wsgi.py` | gunicorn 入口（不执行 `app.py` 的 `__main__`，不起任何调度线程） |
| `scheduler.py` | web 外的定时任务进程：公共预热、复盘、三周期选股、每日清理；agent 自动跑默认关 |
| `screening.py` / `ai_blocks.py` | 从 app.py 抽出的共享辅助（选股初筛 / AI 注入块），agent_loop 直接 import 它们，不 import app。`_screen_rows` 全市场路径按市值三层各留 12 个名额（`cap_layers`），有 focus 时不强制分层 |

### 多用户

| 文件 | 职责 |
|------|------|
| `userctx.py` | 当前用户 contextvar；个人数据目录解析 `user_path`/`shared_path`；跨线程传播 `Thread`/`spawn`/`submit`/`ctx_map`；舰队站长 `fleet_uid`/`as_fleet`/`in_fleet`（环境变量 `ASTOCK_FLEET_OWNER` 优先，否则 auth 注册的 resolver 每 60 秒惰性取最早管理员）；`open_db` 统一开 WAL |
| `auth.py` / `auth_password.py` | 账号表、服务端会话（存 sha256(token)）、`before_request` 全局闸门（默认全关，白名单 `/login` `/logout` `/healthz` `/static/`）、Origin 同源校验（回环来源信 X-Forwarded-Host）、安全响应头、`/api/me`；密码 scrypt，失败按用户名与 ip 两键锁定 |
| `ratelimit.py` | 每分钟总请求、AI 最小间隔、AI 日预算（每人 + 全站）、heavy 桶按请求计数；`allow_llm`/`record_ai_call` 是真实计费点；库 `data/usage.db`；`/api/usage` |
| `astockctl.py` | 账号与用量 CLI：`adduser`/`passwd`/`disable`/`enable`/`kick`/`users`/`usage`/`status` |
| `deploy/` | 阿里云部署：`bootstrap.sh`（首次一键）、`deploy.sh`（服务器侧幂等）、`push.sh`（更新）、`fix_perms.sh`（权限单一源）、systemd 单元 web / scheduler / news timer、`gunicorn.conf.py`、`backup.sh`、`harden_ssh.sh`、`migrate_to_multiuser.py`、`env.example`、`README-deploy.md`（步骤权威）、`mcp/`（让 Claude 桌面端操作服务器的 MCP server，六个固定动作） |

### 三周期选股与观点账本（设计 `plan/2026-09-16-picks-ledger-design.md`）

| 文件 | 职责 |
|------|------|
| `picks_levels.py` | K 线算候选价位（纯函数），模型只能从候选里挑，返回后 `snap` 校验 |
| `picks_store.py` | 观点账本 `data/picks_public.db`（公共三周期）与 `data/users/<uid>/picks.db`（自选股）；修改链、改口规则强制、到期、结果贴回、两层记忆块 |
| `llm_picks.py` | 两个提示词（三周期选股 / 自选股买卖点）+ 返回按条校验 |
| `picks_pipeline.py` | 三周期候选池 + 运行 + 结算 + 文件锁 + 16:00 / 09:05 定时循环。候选池的分层框架见 `cap_layers.py`，每周期因子清单是 `CYCLE_FACTORS`（短线 cum5/cum20/区间位置，中线 cum20/cum60，长线 cum120/波动率，等权、方向按层取）。周期专属的筛选来源：短线补复盘题材池、中线补板块动量前列的龙头与成员、长线先过估值与财报双正（本地 `fundamentals_store`，不再逐只打网络） |
| `picks_track.py` | 候选名单前向超额追踪 `data/picks_track.db`：每天记同层候选深度集（基准）与三周期名单，按 5 / 10 / 20 个交易日结算超额（对比同层基准中位数）。设计第七节第二层验收，不带 AI、不做回测假设。`python3 picks_track.py record\|settle\|status` |
| `cap_layers.py` | 流通市值分层与名额（零依赖纯模块）：大盘 ≥500 亿 / 中盘 100 到 500 亿 / 小盘 30 到 100 亿，30 亿以下不纳入；`layer_of` / `select`（每层名额 + 申万二级上限，层内计数）。`factor_lab`、`screening`、`picks_pipeline` 共用同一套边界 |
| `picks_routes.py` | `/api/picks/*` Blueprint（public / watchlist / chain / run / run_public / status） |
| `alerts.py` | 到点提醒的触发逻辑：把账本里 AI 的点位与用户手改的点位合并，价格进入或接近（默认 1% 容差，纪律参数）就推手机；同一标的同一类每天只发一次；只看 `visible_horizons()`（面板上还 mask 着的周期不推） |
| `alerts_store.py` | 提醒配置与去重（个人库 `data/users/<uid>/alerts.db`）：`targets`（多台手机）、`points`（手改点位，整表替换、错一行整批不落）、`sent`（当天已发）。只存手动覆盖，AI 点位检查时现读账本，避免两边打架 |
| `alerts_routes.py` | `/api/alerts` Blueprint（读配置 / 存手机 / 存点位 / 发测试 / 立即检查），5 条路由 |
| `notify.py` | 发送层：ntfy / bark / wecom / dingtalk（含加签）/ pushplus（微信）/ log 六个渠道，零 SDK 纯 urllib，逐台发、逐台记结果，永不抛异常影响调度 |

### 数据与池子

| 文件 | 职责 |
|------|------|
| `datasources.py` | 行情、指标、K 线、财报、新闻、研报、龙虎榜、解禁；`sina_all_stocks`（全 A 名单 + 快照）；`index_quotes`/`market_breadth`（大盘）；`global_markets`（外围数值）；`concept_tags`（东财板块） |
| `universe_store.py` | 全市场池 `data/universe.db`：全 A 名单 + 板块归属（东财 slist 逐股回填）+ 板块日变化。`codes_of`/`sector_of`/`sectors_map`/`taxonomy`/`snapshot_daily`/`sector_ranking`/`backfill_sector_daily`（逐股日 K 补历史，`_agg_sector_payload` 与 live 共用口径）。名单刷新把本轮没再出现的代码标 `active=0`（退市下线；覆盖率不足九成时跳过，防分页抓取不全误伤半个池子）。另有 `valuation_daily`：交易日收盘后落一份全市场 PE/PB 快照（`valuation_snapshot`，保留 2 年，写入路径自动清理），给长线估值因子攒历史时点数据。`codes_by_mcap`（全池按市值降序）与 `mcap_of`（代码到流通市值，**统一换算成亿元**，表里存的是新浪的万元）是分层与抽样的唯一入口 |
| `universe.py` | 「精选龙头」fallback（10 一级 x 48 二级 x 170 只）。`universe_store` 未就绪时兜底 + `is_leader` 标记 |
| `fundamentals_store.py` | 财务面板 `data/fundamentals.db`：把新浪财报的多期数据落库（`sync` 分批断点续传，`python3 fundamentals_store.py sync\|status`），给中长线攒基本面历史。主键 `(code, period)`，行数受报告期数约束，不需要 purge |
| `moneyflow_store.py` | 资金流历史 `data/moneyflow.db`：`snapshot` 每交易日用东财 clist 落一份全市场当日主力净流入（约 56 页，持续积累的正路），`sync` 走 push2his 的 120 天历史种子（对突发敏感，低频用）。保留 2 年，写入路径自动清理 |
| `news_store.py` | L2 新闻库（滚动 1 年）+ `is_trading_day`（动态节假日） |
| `notes_store.py` | L5 私域笔记（永久） |
| `store.py` / `config.py` | 自选股持久化 / 读 `.env` |
| `fetch_news.py` | 新闻抓取批处理入口（本地 launchd、服务器 `astock-news.timer`） |

### 资金与交易

| 文件 | 职责 |
|------|------|
| `fees.py` | 交易成本单一事实源：`FeeSchedule`（佣金可议价可免最低、印花千 0.5 仅卖出、过户万 0.1）+ `round_trip`/`breakeven_pct` + `for_ai()`。费率存画像，不硬编码 |
| `profile_store.py` | 多档投资画像（`profiles.db`，按人隔离）：现金 + 风险偏好 + 费率表；5 档 TIERS 按总资产落档 + `RISK_GUIDE` + `block_for_ai` |
| `portfolio.py` | 持仓（按画像隔离 + 多笔 lot 模型）。现金 = 可用现金（买扣卖加）+ `cash_reconcile` + 逐笔 `lots_view` + `reduce`（减仓 / 卖出，FIFO，现金加回并记已实现盈亏）+ `realized`/`realized_total` |
| `paper_store.py` | 模拟撮合（`paper.db`）：整手、涨跌停、T+1、费率可传入（agent 多账户各用各的） |

### AI 与知识

| 文件 | 职责 |
|------|------|
| `llm.py` | DeepSeek 调用：daily / screen / position / entry / market_overview 走 v4-pro，structure_note / company_profile / macro_digest 走 v4-flash；`_chat` 是唯一出口，计费点在这里 |
| `template_store.py` | 提示词版本化（`data/templates.db`）：多版本、激活、回滚，按版本聚合客观指标 |
| `provenance.py` | AI 溯源与依据校验（仅 entry / position）：闭集信号字典 + `verify_basis` |
| `rules_store.py` | 交易规则库（`rules.db`，场景化启用，注入 AI） |
| `ai_cache.py` / `websearch.py` | L1 AI 输出缓存（`data/ai_cache.json`，键带 uid）/ 博查联网（可选） |

### Agent 与因子

| 文件 | 职责 |
|------|------|
| `factor_lab.py` | 因子回测与失效监控（`data/factors.db`）：`backtest`（顺带产出 `excess_dist` 超额分位分布，判罪线唯一来源；同时写 `layer_mid`/`layer_small` 的分层 IC）+ `rank_of` + `summary`（IC / t 值）+ `direction(cohort=)` 动态定方向 + `cycle_directions(pairs, cohort)`（按周期的因子与地平线取方向）+ `layer_report`（分层训练窗对留出窗，验收用）+ `ic_cohort` 表（层 cohort 与大盘层密采）+ `backtest_large`（大盘层 ≥500 亿密采，写 `layer_large`）+ `scoring_directions(cohort)` + `rolling_ic`/`decay_alert`/`flip_rate` + `refresh_if_stale` + `backtest_stops`（止损网格）。6 个因子全是日K确定性分量（vol/cum5/cum20/cum60/cum120/range_pos），缺失按列屏蔽。注意 `codes_of()` 无 focus 按代码号排序不按市值（PITFALLS #5b） |
| `agent_store.py` | Agent 持久层（`agents.db`，站长目录）：`agents` / `runs`（原文 90 天、结论 365 天，原文截 20000 字符）/ `lessons`（闭集 10 类：9 类入场属性 + `bad_outcome` 事后结算）/ `pending`（限价挂单）/ `conditions`（止损）/ `claims`（时段原子占位）/ `equity` / `entries`（建仓留痕 + 冻结分位 `x20_pctile`）/ `journal`（情节记忆，append-only） |
| `agent_loop.py` | 日循环：研判（`_market_block`）、选股、决策（可插拔 single / debate）、风控（确定性硬门）、挂单、复盘（确定性失败检测）。`sweep_orders`/`sweep_conditions`/`current_slot` |
| `outcome.py` | 结果结算（纯函数）：`forward_returns`（自成交价，按 K 线根数数交易日）+ `bench_returns` + `excess`（扣 beta）。地平线引用 `factor_lab.HORIZONS` 不复制。只算不判罪 |
| `structure.py` | K 线结构摘要（纯函数零网络）：`digest()` 出 MA5/20/60 + 近 20 日高低 + 最近 3 根 OHLC；`fmt_stock`/`fmt_market` 成行喂 AI。只给原料不下判断 |

### 复盘模块 `review/`

`fetch`（打板四池、题材串、龙虎榜、板块资金流；东财 push2ex 与同花顺，纯 urllib、内置节流）、
`metrics`（8 类情绪硬指标，纯函数）、`stocks`（个股线索：挑股 / 补数据 / 一次调用出档案）、
`store`（落盘 `data/review/<date>.json`、`history.json`）、
`llm_review`（5 角色分析师 fan-out，v4-flash；裁判与文稿，v4-pro）、`pipeline`、`run_daily`（批处理入口）、
`backfill`（同花顺涨停池回填情绪周期）。

### 测试

`tests/test_*.py` 零依赖离线，`python3 tests/xxx.py` 直接跑，项目无 pytest。文件清单与每个文件的
断言数看 `python3 tools/status.py`。对应关系：改 `parse_tags` 跑 `test_universe_store`；改 `_screen_rows`/`_pa_score`
跑 `test_screen_branches`；改 `run_day`/幂等跑 `test_agent_gates`；改 `direction`/打分跑 `test_factor_lab` 与
`test_factor_cohort`；改 `structure`/决策提示词跑 `test_structure`；改 `outcome`/地平线跑 `test_outcome`；
改判罪/分布跑 `test_excess_dist`；改 `agent_store`/`agent_loop`/`ai_blocks` 的记忆部分跑 `test_agent_memory`
与 `test_excess_dist`；改板块聚合跑 `test_sector_backfill`；改 `fees`/`portfolio`/`paper_store` 跑同名测试；
改复盘指标跑 `test_review_metrics`；改个股线索跑 `test_review_stocks`；改 `userctx`/`auth`/`ratelimit`/复盘锁/舰队路由跑同名测试；
改 picks 模块跑 `test_picks_*` 与 `test_llm_picks`；改分层边界或行业上限跑 `test_cap_layers`；改提醒的触发算术/文本解析/发送载荷跑 `test_alerts` 与 `test_alerts_routes`；改前向超额追踪跑 `test_picks_track`；改 `fundamentals_store` 跑 `test_fundamentals_store`；改 `moneyflow_store` 跑 `test_moneyflow_store`。

## 复盘模块（`/review`）

定位：A 股短线情绪的每日复盘，收盘后自动出一份盘面复盘。与选股看板 `/` 分栏、独立页面、顶部切换。

- 流水线：取数与体检闸，再纯算情绪硬指标（赚钱效应、晋级率、连板梯队、情绪周期、题材热点、亏钱效应、
  封板质量），再 DeepSeek 5 分析师（情绪面、资金面、题材热点、龙虎榜游资、龙头梯队）加复盘裁判，
  再生成文稿，落盘，前端只读最新一份。九成是硬指标纯计算，AI 只在研判与文稿两块。
- 资金面来自东财 `push2 clist` 板块主力净流入（f62），是实时快照无历史，所以 `pipeline` 只在跑最新场次
  （`date=None`）时纳入，历史重跑跳过，避免未来函数。clist 对住宅 IP 间歇封锁，`fetch._get_json` 带退避
  重试，仍失败则资金面降级、只出 4 角色。裁判保留「直读硬指标」降级路径。
- 情绪周期单用同花顺深史（东财打板池只有约 15 个交易日深度），2 因子 = 涨停家数 + 最高连板
  （`high_days_value>>16` 取连板）；炸板率因同花顺无端点不入周期，仍单列在硬指标卡走东财近 15 日。
  回填只补既成事实，无未来函数。`python -m review.backfill 3` 回填近 3 个月。
- 个股线索（子项目 A，2026-09-17）：从板块资金流前 3 个板块的成员、连板梯队前 3、龙虎榜净买前 2
  挑出当日的标的（默认 8 只，去重、带理由标签、不打分），每只补行情/估值、财务（本地面板）、
  舆情（本地新闻库）、资金（新浪）、板块归属、龙虎榜命中，再**一次** v4-flash 调用出档案
  （tagline / 基本面 / 财务 / 舆情 / 驱动 / 风险 / 盯什么）。**输出按白名单重建，价位类字段一律丢弃**
  （买卖点归子项目 B）。公开块进 envelope 的 `stocks`；自选股那一层按人隔离，落
  `data/users/<uid>/review_stocks.json`，公开复盘里不出现任何人的自选股。设计见
  `plan/2026-09-17-review-stocks-design.md`。
- 边界：板块层面不荐个股、不给买卖点；个股线索只做客观档案（不给价位、不给仓位），
  买卖点归三周期选股与自选股买卖点。不引 akshare。
- 调度：本地 `python3 app.py` 时 `app._review_scheduler`（交易日 `REVIEW_AUTO_TIME` 默认 18:30 到点跑一次，
  幂等靠 `_review_sched_state.last_auto`）+ 首启 `_review_boot`（history 不足 5 天则后台回填）。服务器上这两个
  线程跑在 `scheduler.py` 里。生成前先拿 `data/review/.running-<date>` 文件锁，`/api/review/status` 能看到
  别的进程在跑（`running_elsewhere`）。
- AI 整块降级不落占位：分析师全失败且裁判与文稿都没产出时，`ai` 记 None、envelope 打
  `ai_degraded` / `ai_error` / `ai_error_kind`，硬指标照常落盘（不伪装成一份完整复盘）。错误类型来自
  `llm.LLMError.kind`：`budget` 表示当天额度用完、重试无意义；其它类型由调度在 20 分钟后自动重试一次
  （`pipeline.AI_RETRY_AFTER_MIN`）。未配置 key 时直接跳过 AI，不算降级。

## 多用户与部署

用户三个决定（2026-09-16）：多用户改造正式进代码；agent 舰队全站只有一套、归站长；服务器上 agent
不自动跑（保留「不开 app 就不炒股」）。

- 用户上下文：`auth._gate` 每个请求 `userctx.set_uid(uid)`，`teardown_request` 清空。个人 store 的路径在
  调用时解析到 `data/users/<uid>/`，无用户时 `require_uid()` 抛 RuntimeError（宁可 500 不串人）。公共库
  路径不变。后台线程一律 `userctx.Thread`/`spawn`/`submit`，线程池用 `ctx_map`；原生 `threading.Thread`/
  `ex.map` 拿不到用户，个人 store 会炸（PITFALLS #19）。
- 舰队（已暂停，2026-09-17 用户决定）：20 个 agent 全部置 `active=0`，`templates/index.html` 的工具条
  入口与弹窗、`static/app.js` 的调用点已移除；后端 `/api/agents*`、`agent_loop`/`agent_store`/`factor_lab`
  与自动交易的纪律线全部保留作 backup（不进调用链，`_agent_boot` 见无启用 agent 即返回）。
  要恢复：把入口与弹窗加回页面，再把 `agents.db` 里的 agent 置回 `active=1`。
- 舰队 = 站长的数据：agents.db / paper.db / profiles.db 不搬公共库（一次舰队运行还要读站长的规则与画像）。
  所有舰队代码路径进 `userctx.as_fleet()`：`app._fleet_route` 包住全部 `/api/agents*`（GET 对登录用户开放，
  写操作只允许管理员或站长，否则 403；站长未设置 503）、`_agent_tick`、`scheduler._run_fleet_agents`、
  `picks_routes` 的 run_public、ai_blocks 的 `_lesson_block`/`_stock_house_view`/`_regime_view`。站长 =
  `ASTOCK_FLEET_OWNER`，否则最早创建的管理员（停用不换人；建号后最多 60 秒生效）。
- 计费点在真实 LLM 调用：`llm._chat` 发请求前 `ratelimit.allow_llm(uid)`（门故障也拒绝），成功后
  `record_ai_call`；缓存命中、失败、超时不计。门是「先读计数、成功后加一」两步，并发时最多多放行
  「同时在飞的调用数」（约十几二十次，已知取舍，不做预占以免进程被杀漏额度）。预算类拒绝抛
  `llm.LLMError(kind="budget")`，`run_all` 收到后短路本轮其余 agent（不白跑数据面）。舰队调用记 `fleet`、
  无用户记 `system`，都只受全站预算
  `ASTOCK_AI_GLOBAL_DAY`（默认 150，要把舰队一天 40 多次算进去）；个人预算 `ASTOCK_AI_PER_USER_DAY`
  默认 40。`before_request` 的 `check()` 对 ai 路径只做门（频率、6 秒最小间隔、预算是否已满）。新增会调
  LLM 的路由不需要登记，计费自动生效。ai_cache 键带 uid（`macro`/`profile` 两类公开资料例外）。
- 三进程拓扑：gunicorn（gthread，2 worker x 8 线程，只绑 127.0.0.1:5000，`max_requests=0`）+ `scheduler.py`
  + tailscale serve 终止 TLS。跨进程状态只靠文件锁：东财节流
  `data/.em_last_call` 与用户首次建库 `data/users/<uid>/.init.lock` 用 fcntl 排他锁（不可用时回退进程内行为）；
  复盘 `data/review/.running-<date>` 与选股 `data/.picks-running-public`、`data/users/<uid>/.picks-running` 用
  O_EXCL 存在性锁（超 30 分钟视为陈旧可覆盖；复盘拿不到锁拒绝生成，选股拿不到锁放行）；
  选股到点记录 `data/.picks-last.json`（原子写）记「当天这个槽跑过了吗」。进程内字典（`_review_job`、
  `_user_inited`、ratelimit 的分钟窗与最小间隔）每个 worker 一份，只做「本进程视角」。
- 本地开发：`python3 app.py` 在站长上下文起盘中调度器；启动瞬间解析不到站长时由 `_agent_watch`
  每 60 秒等一次，建号后自动补启（不必重启）。不要同时再跑 `scheduler.py`。
  `ASTOCK_ENV=production` 时 `app.py` 拒绝直接启动。
- 对 conda 硬偏好的例外：服务器 systemd 单元用 `/opt/astock/.venv`（Miniconda 建的 3.10 环境放在这个
  路径；无人值守的 nologin 账号下比 conda activate 少坑）；本地仍 conda。用户可否决。
- 阿里云 + Tailscale 必做：`tailscale set --accept-dns=false --netfilter-mode=off`，否则 Tailscale 的防伪造
  规则会丢掉阿里云 100.100.2.x 的 DNS 应答，整机 DNS 断。原因与步骤在 `deploy/README-deploy.md` 第四节。
  这台机器是共用机，`deploy.sh` 用 `ASTOCK_UFW=0` 跳过 ufw，外围防线 = 阿里云安全组 + Tailscale。
- 三周期选股运行时：交易日 16:00 全量（公共三周期在 `as_fleet()` 下跑、记 `fleet`；各账号自选股各跑一次记
  本人），09:05 只让公共池跑短线；自选股不分早晚，两个时刻都是同一份覆盖全部周期的完整提示词。手动
  `/api/picks/run` 走 ai 门、计入个人日额度；`/api/picks/run_public` 管理员可点、记 `fleet`。改口规则在
  `picks_store._enforce` 里强制，不留后门。中长线在攒够历史前**不在面板展示**（`picks_pipeline.visible_horizons`
  的门，门槛 250 个交易日；前端取 `/api/config` 的 `picks_horizons` 决定展示哪几列），账本与 AI 照常
  生成、数据不断档，攒够自动上线，见 `plan/2026-09-17-screening-factor-refactor-design.md`。
  公共短线候选池的题材串来自最近一期复盘（review store 的
  `raw_theme`），复盘没生成时短线池只剩分层因子选股那一段（不再退化成纯换手榜：换手率没有历史分位，
  已不参与选股）。到点记录落 `data/.picks-last.json`（跨进程、重启不丢），
  避免 scheduler 在 16:00 后重启把当天 full 槽再跑一轮。
- 到点提醒：`scheduler.py` 与本地 `python3 app.py` 各起一个 `alerts.loop_forever`，
  每 `ASTOCK_ALERT_TICK_SEC`（默认 300）秒给每个配了目标的账号扫一遍，非交易时段空转。
  没配目标时整个循环等于空转，不产生任何请求。

服务器现状（部署级事实，改了就改这里）：阿里云 ECS 别名 `aliyun_ecs`，Ubuntu 20.04 共用机（k3s、docker、
nginx、java 同机），站长账号 `<站长账号>`。systemd 单元 `astock-web`、`astock-scheduler`、`astock-news.timer`
（08:40 / 11:40 / 14:00 / 15:30 / 20:30 抓新闻），备份 cron 23:30 以 astock 身份跑。更新服务器：本地
`bash deploy/push.sh`（经 `~/astock-staging` 中转）。非 `astock` 的登录账号不能 `cd /opt/astock`，用
`sudo -u astock /opt/astock/.venv/bin/python3 /opt/astock/...` 绝对路径。
服务器上的东财端点**不是全通**（2026-09-17 实测）：`datacenter-web`、`push2ex` 通，
`push2.eastmoney.com` 与 `push2his.eastmoney.com` 在 TCP 层就被拒（curl 000 / Empty reply，
换 UA 与 Referer 一样，强制 IPv4 也一样，家里 IP 反而通），所以 clist 类请求一律带镜像备用主机
`push2delay.eastmoney.com`（`moneyflow_store._CLIST_HOSTS`、`review.fetch._SECTOR_HOSTS`）。
另外这台机器的 DNS 对 `push2*` 会回 AAAA（trafficmanager 的 IPv6 池），而它没有 IPv6 默认路由，
所以别依赖系统地址选择。历史资金流种子（`push2his`）只能在家里跑。

## 数据源与坑（改代码前必读）

优先级：能用腾讯、新浪、mootdx（不封 IP）就别用东财；东财仅用于其独有数据且走 `em_get()` 限流。

| 数据 | 源 | 备注 / 坑 |
|------|-----|----------|
| 实时行情、估值 | 腾讯 `qt.gtimg.cn`（GBK） | `tencent_quote` 必须含 `last_close`(f4) / `change_amt`(f31)，否则当日盈亏恒为 0 |
| 波动率、资金流 | 新浪 MoneyFlow | 返回里带每日收盘价 `trade`，波动率与资金流一份数据两用 |
| 日 K 线 OHLC | 新浪 `getKLineData` | 腾讯 `hqkline` 端点已失效（`code:11`） |
| 研报、龙虎榜、解禁 | 东财 `reportapi` / `datacenter` | 走 `em_get()` 串行限流（间隔 1 秒以上） |
| 个股资金流 push2 / push2his | 东财 | 部分住宅 IP 间歇封锁，所以资金流一律用新浪，别依赖 push2his。服务器（阿里云 IP）上 `push2` 与 `push2his` 整条不通，只有延迟镜像 `push2delay` 通：clist 类请求用 `moneyflow_store._CLIST_HOSTS` 的主备切换，历史种子只能在家里跑 |
| 全 A 名单 | 新浪 `hs_a` | `getHQNodeStockCount` 拿总数 + `getHQNodeData` 分页（80 / 页并发）。返回自带行情字段，名单与板块统计共用 |
| 板块归属 | 东财 `slist`（逐股） | 东财按端点封 IP：`clist`（批量）对住宅 IP 间歇封锁，`slist`（逐股）放行 0.2 秒一只，所以逐股回填约 100 分钟。服务器 IP 上 `push2` 的 clist 不通、镜像 `push2delay` 通（PITFALLS #14） |
| 行业分类 | 新浪 `newSinaHy.php` | 仅 49 类、覆盖约 54%，表老旧，已弃用 |
| 历史资金流 | 新浪 MoneyFlow | 只给 30 天（传 `num=260` 也只回 30），所以 `_pa_score` 的 `net20` 分量无法回测、不参与打分。见 `plan/BACKLOG.md` |
| 美元指数 / VIX | 无 | 新浪外盘 `hf_DX` 返回空、VIX 无代码，`macro_digest` 缺这两个风险指标。见 `plan/BACKLOG.md` |
| 财报三表 | 新浪 | `financial_summary` 取营收、归母净利 + 同比 |
| 新闻 | 东财个股新闻 + 财联社快讯 + 东财 7x24 | 财联社走 v1 API + 本地签名（`md5(sha1(sorted query))`），零 key |

## AI 配置（DeepSeek + 博查）

- DeepSeek：`deepseek-v4-pro`（该账号只有 v4-pro / v4-flash 可用）。是推理模型，`max_tokens` 同时覆盖
  思考与正文，太小会导致思考耗尽、正文返回空。各函数的 `max_tokens` 以 `llm.py`、`llm_picks.py`、
  `review/llm_review.py` 里的值为准（daily 8000、screen 9000、entry 7000、position 6000、market_overview
  6000、picks 20000），别调小。温度统一 0.15（`_chat` 默认值）；`_DISCLAIMER` 强制「只据给定数据、
  不编造、不预测方向」。OpenAI 兼容 `POST /chat/completions`，支持 `response_format:{type:json_object}`，
  零 SDK（纯 urllib）。`deepseek-v4-pro` 别名指向最新正式快照，模型串不用改。
- 博查（可选）：`POST api.bochaai.com/v1/web-search`，Bearer 鉴权，body `{query,freshness,summary,count}`，
  成功响应 `webPages.value[].{name,url,siteName,snippet,summary}`，错误体 `{"code":"401","message":"Invalid API KEY"}`。
  `.env` 用 `BOCHAAI_API_KEY`，`config` 也兼容 `BOCHA_API_KEY`。`websearch._classify()` 把 401 归为 key 无效、
  402/403 归为余额不足、429 归为限流；`/api/websearch/status?probe=1` 主动探测，前端启动时探测。
- 联网知识两路：A 免费默认开，`market_news_digest()` = 财联社快讯 + 东财 7x24，喂进 AI 提示词；B 配了博查
  key 时 `app._ai_web_context()` 追加搜索结果。llm 函数带 `web_context` 参数；`/api/config` 暴露
  `news_augment` / `web_search`。
- 密钥只在 `.env`（gitignore），源码零硬编码。后端不能调用 Claude 的 WebSearch（那是对话侧工具）。

## 约定（写代码前必读）

红涨绿跌（A 股惯例）。情景区间 = 年化波动率反推，只描述幅度、不预测方向。所有 AI 输出标
「参考信号，不构成投资建议」。前端用系统字体栈（不加载 Google Fonts）、图表纯内联 SVG、零外部依赖。

盈亏两套口径，别混（用户 2026-07-16 明确）：
- 显示层（持仓中）= 持仓盈亏 = 毛减已付买入费（对齐券商，`pnl_broker`）。卖出费不算进主盈亏，
  按每支股单列为「今日离场费」（`sell_fee_if_now`），展开可见「落袋净」。
- AI 决策层 = 保本涨幅走往返（买 + 卖都算，`fees.breakeven_pct`），因为买进这一笔早晚要卖。
  两套口径服务不同目的，不要统一。
- 已实现盈亏（卖出后）= 落袋净（`portfolio.reduce` 记流水）：卖额减 FIFO 成本，再减分摊的已付买入费和
  卖出费。累计 `realized_total` 进资产总览；总收益卡 = 已实现 + 未实现（前端聚合）。减仓 FIFO；
  「清仓」按钮只删记录不动现金（记错用），真卖出走「减仓」。

异步渲染必须带请求令牌：多个慢请求写同一 DOM 目标时用单调计数器（`recSeq`/`detailSeq`/`mktSeq`/
`secSeq`/`agSeq`），发起即 `++`，`await` 回来后 `if(gen!==seq) return` 丢弃过期响应。

按日累积的表一律要有 `purge()`，写入路径自动调用，容量在前端可见（`sector_daily` 曾无清理，
10 年会涨到 820MB）。

多用户下的三条硬约束：新起线程或线程池用 `userctx.Thread`/`spawn`/`ctx_map`，不用原生的；碰 agent
数据的代码路径进 `userctx.as_fleet()`；跨进程要共享的状态落文件锁（fcntl 排他锁或 O_EXCL 存在性锁），
不放进程内字典。新增 store 先决定归个人库还是公共库。

主题与移动端（2026-09-17 子项目 D）：**默认浅色**，`static/theme.css` + `static/theme.js` 三个页面共用
（选股看板 / 复盘台 / 登录页）。配色 token 仍定义在各页自己的 `<style>` 里（深色是底），
`html[data-theme="light"]` 覆盖成浅色；`data-theme` 由 `<head>` 里一行内联脚本在样式生效前写好，
不会闪一下深色，切换记在 localStorage。**加新样式时别硬编码颜色**：变量换不掉硬编码值，
浅色下会留下黑斑（首版就漏了登录页输入框、悬停气泡、报错条三处，靠截图才发现）。
手机上（≤760px）表格改横向滚动而不是挤压裁切、输入框字号 16px（iOS 聚焦不放大）、
抽屉铺满、工具条换行、栅格单列。版面约定（用户 2026-09-17）：加自选在「自选股对比」标题行里
（离列表最近，不放页头）、三周期推荐默认收起成一行并可展开（状态记本机）、复盘台顶部有大字
场次横条（哪一天、第几场，不是最新一场要醒目）。改了样式要**真的渲染出来看**：本地复制一份带 data/ 的副本、
把 `app.run` 的端口改成 5001 起起来，用 Playwright（本机已装）按 390×844 与 1440×900 各截一张。

项目文档、代码注释、commit message 不出现装饰符号（用户 2026-08-25 规则）：不用箭头、对勾、感叹号
类 emoji，用文字。存量文档已在 2026-09-17 清完，之后新写的内容照此。运行时输出字符串按同一口径。

改代码前必读 `plan/PITFALLS.md`。最要命的三类：
1. 凡是拍脑袋定的阈值，方向大概率是错的，已被数据打脸多次（`_pa_score` 两个分量方向全反、止损线、
   教训阈值）。能验必验，验不了要明确标注为纪律参数。
2. 教训不得与因子数据矛盾，否则系统在惩罚 AI 服从自己的数据。
3. 补跑与回测必然引入未来函数，除非补的是既成事实（日 K low、分时）。agent 决策不能补；LLM 回测
   同理不可信（模型见过未来）。

## 关键机制速查

agent 交易与学习闭环的端到端全景见 `plan/2026-07-18-agent-logic-map.md`（权威）。

- AI 注入链：`_tier_block()`（本金档）+ `_fee_block()`（交易成本）+ `_lesson_block()`（历史教训）+
  `_macro_block()`（全球宏观）+ `_ai_web_context()`（规则库、新闻、笔记、联网），前置注入用户面的 AI。
  agent 走 `_agent_blocks(ag, cash, total, n_pos)`：档位跟账户的钱走、费率跟券商走。
- 交易成本（`fees.py`）：费率存画像。用户券商不免 5 元最低（2026-07-16 电话确认）。有无最低佣金比
  费率本身更重要：单笔低于分界（5 / 费率）时佣金被 5 元顶起。用户单笔约 1400 元，万 9 还是万 2.5
  佣金都是 5 元，保本涨幅约 0.75%，且拆单成倍加佣金。AI 费率块据此警告「一次建仓、别拆」。
  用户画像 = 万 2.5 + 5 元最低；agent 画像维持「免最低」（用户决定，后果见 BACKLOG）。
- 因子方向（`factor_lab.py`）：`_pa_score` 的打分方向与教训记不记全由 `direction()` 决定：近 60 日
  `|t|>2` 用近期，否则全样本，都不显著则不参与打分（不猜）。方向分池读：cohort 是市值层
  （`layer_large`/`layer_mid`/`layer_small`，`backtest` 与 `backtest_large` 写进 `ic_cohort`），
  某层无数据时整体回退全池、有数据但不显著则尊重 0。这个分层不是装饰：2026-09-17 实测波动率因子
  在大盘层是 +1（高波动跑赢）、在中小盘是 −1，全池一个方向套三层必然在大盘押错。三周期的因子与
  地平线在 `picks_pipeline.CYCLE_FACTORS`，`.claude/docs-tidy/factor_range_check.py` 复核分位锚点，
  `layer_report()` 出验收数字（训练窗对留出窗）。`refresh_if_stale()` 启动时惰性重跑。
- agent 三道门：非交易日，交易时段（`require_open`），时段桶原子占位（`claim_slot`）。时段桶 = 早盘 /
  尾盘，错过不补。
- 盘中调度器（`app._agent_scheduler`）：守护线程每 5 分钟探一次 `run_all(require_open=True)`，长期挂机
  或非交易时段启动也能每桶自动跑；`claim_slot` 幂等保证每桶只真跑一次，`_agent_tick_lock` 单飞。
  服务器 gunicorn 不起它，`scheduler.py` 要 `ASTOCK_AGENT_AUTO=1` 才跑。
- 限价挂单：AI 给 `limit_price`，`place()` 挂单不即时成交；`sweep_orders()` 用分时（当日）或日 K（隔夜）
  判定触及，成交价锁 limit 不取更优。当日有效。条件单补判（`sweep_conditions`）触发后若卖不出
  （跌停封板、行情异常），放回 `live` 下次再试、不作废；只有仓位已不在时才作废。
- agent 记忆（一份 journal、多个视图）：底座 `agent_store.journal`（append-only），每次决策入一行，含
  `regime` tag、`signals` 快照、`action`、`summary`（当时写下的理由原话，零新增 LLM 调用）；结算时贴回
  20 日超额与冻结分位（`journal_staple_outcome`）。判罪分位 `entries.x20_pctile` 结算算一次即冻结，不随
  判罪线漂移重算。视图：交易 agent 看 `_agent_journal_block`（自己近 8 条）+ 自己教训 + 全舰队只读层
  （`_lesson_block()`，标签区分本账户与全体）；用户面深挖看 `_stock_house_view(code)`；大盘研判与选股看
  `_regime_view`（同类行情下全舰队战绩）。只喂事实原话，不让 agent 写事后反思。
- 到点提醒（`alerts.py`）：渠道 ntfy / bark / wecom / dingtalk / pushplus（微信推送）/ log，
  **地址可以不写渠道**——
  `alerts_store.infer_target` 认域名（企业微信、钉钉、ntfy.sh、api.day.app）与裸串（只有带
  连字符/下划线/数字的才当 ntfy 主题，`telegram` 这种打错的渠道名必须报错，不能静默变成一个
  没人订阅的主题）。页面点「生成 ntfy 主题」直接给一行可用配置：安卓苹果都装 ntfy、不用注册不用备案。
  点位 = 手动覆盖优先，其余用账本里 AI 的 `entry_lo/hi`、`exit_lo/hi`、`stop`。
  「接近」的容差 `APPROACH_PCT`（默认 1%，环境变量 `ASTOCK_ALERT_APPROACH_PCT`）是**纪律参数**，
  提醒正文里会写清「已进入」还是「接近」，并标点位来自手动还是 AI。三类触发每天每标的各一次，
  止损优先于买点（同时摸到时先报风险）。**AI 的价位生成当天不推**（它贴着生成时的现价给，
  当天推等于复读面板），第二个交易日起生效；手设点位不受这条约束。非交易时段直接跳过，
  面板上被 mask 的中长线也不推，免得面板不显示、手机却响。渠道配置在页面「三周期」下的
  「到点提醒」里按行罗列，一台一行；先填 `log` 可以只写服务器日志验证逻辑。
- 观点账本改口规则（`picks_store._enforce`）：有效期内只在触发事件（止损触发、目标达成、周期到期、
  新信息推翻）时允许 revise / withdraw，否则降为 keep；`stop_hit` / `target_hit` 还要与结果贴回的
  `touched` 一致。记忆块两层：近 60 天全文最多 12 条，远期只放确定性汇总加最多 3 条相关性挑选。

## 冒烟测试（改完自测）

```bash
python3 tools/status.py                  # 跑全部离线测试并打印路由数、大文件、plan 状态
python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('*.py')+glob.glob('review/*.py')+glob.glob('tests/*.py')+glob.glob('deploy/*.py')+glob.glob('tools/*.py')]"
node --check static/app.js && node --check static/review.js && node --check static/theme.js
perl -e 'alarm 90; exec @ARGV' -- env NO_PROXY='*' python3 -c "import app, wsgi, scheduler"   # 零网络

# 改决策提示词或数据面后，必须实跑一个 debate 档（single 跑通不等于 debate 跑通，辩论是 token 预算最短板）：
python3 -c "import agent_loop as al; print(al.run_day(18, dry_run=True, force=True))"

python app.py &                          # 一律用 127.0.0.1 别用 localhost：localhost 走 ::1 撞 AirPlay 403（PITFALLS #18）
# 接口都要登录：先拿 cookie（本地 ASTOCK_ENV 不设 production，cookie 才不带 Secure）
curl -s -c cj.txt -H 'Origin: http://127.0.0.1:5000' -d 'uid=<你>&password=<密码>' 127.0.0.1:5000/login
curl -s -b cj.txt 127.0.0.1:5000/api/me                 # uid / is_admin
curl -s -b cj.txt 127.0.0.1:5000/api/usage              # 今日 AI 次数 / 全站预算
curl -s -b cj.txt 127.0.0.1:5000/api/config
curl -s -b cj.txt 127.0.0.1:5000/api/universe/status    # 池子：总数 / eligible / 板块回填进度
curl -s -b cj.txt "127.0.0.1:5000/api/sectors?kind=sw1&limit=5"
curl -s -b cj.txt 127.0.0.1:5000/api/factors            # 因子 IC / 方向 / 翻转 / 新鲜度
curl -s -b cj.txt 127.0.0.1:5000/api/agents             # 站长舰队 + 教训汇总（未设站长 503）
curl -s -b cj.txt 127.0.0.1:5000/api/review/status      # running / running_elsewhere / stocks
curl -s -b cj.txt 127.0.0.1:5000/api/review/stocks      # 个股线索：公开块 + 我的自选股块
curl -s -b cj.txt 127.0.0.1:5000/api/picks/public       # 三周期各 5 只
# AI 类接口 30 到 90 秒，加 --max-time 200；有代理加 --noproxy '*'
# 服务器三进程冒烟在隔离副本里做：gunicorn -c deploy/gunicorn.conf.py -b 127.0.0.1:5002 wsgi:application 加 python3 scheduler.py
```

不要在仓库目录里直接跑 app.py 或 scheduler.py 做实验（会写 data/），用 scratchpad 里的副本、端口 5001 起。

## 安全（提交前必做）

- `.gitignore` 排除 `.env` / `.env.*` / `.claude/` / `.vscode/` / `__pycache__/` / `watchlist.json` / `portfolio.json` / `data/`。
- 提交前铁律，`git add -A` 后跑：
  ```bash
  git ls-files --error-unmatch .env    # 必须报错（未跟踪）
  git ls-files -z | xargs -0 grep -lE "sk-[A-Za-z0-9]{16,}"   # 必须无输出
  git diff --cached -U0 | grep -E '^\+[^+]' | grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|/Volumes/|/Users/' \
    | grep -vE '127\.0\.0\.1|0\.0\.0\.0|203\.0\.113'          # 公网 IP / 本机绝对路径：必须无输出
  ```
- 绝不把 key、个人邮箱、本地绝对路径、服务器公网 IP、服务器登录名写进被跟踪文件。测试里的 IP 用
  203.0.113.x 文档保留段。deploy 文档里用 `<公网IP>` `<登录名>` 占位符。
- 远程 `git@github.com:Gresham429/A-stock.git`（main 分支，MIT，Conventional Commits，中文 subject）。
  `gh` 未登录，推送走 SSH。

## 代码风格

- 小文件（目标 400 行以内）、类型注解、模块级 `logger`、不要裸 `except`、不硬编码密钥。
- 所有东财请求走 `em_get()`（内置限流）；新数据源优先选不封 IP 的腾讯、新浪。
- 项目文档不写可从代码得到的数字，用 `tools/status.py` 看。

## 数据文件（全部 gitignore）

公共 `data/`：`news.db` `universe.db` `factors.db` `templates.db` `auth.db` `usage.db` `review/`
`picks_public.db` `picks_track.db` `ai_cache.json` `fundamentals.db` `moneyflow.db` `.em_last_call` `.picks-running-public` `.picks-last.json`。
个人 `data/users/<uid>/`：`watchlist.json` `portfolio.json` `notes.db` `rules.db` `paper.db` `profiles.db`
`agents.db` `picks.db` `alerts.db` `review_stocks.json` `.init.lock` `.picks-running`。舰队只读站长目录里的 `agents.db` / `paper.db` / `profiles.db`。
旧布局（根目录 `watchlist.json`、`data/agents.db` 等）由 `deploy/migrate_to_multiuser.py <uid>` 复制进
站长目录，原文件不删。

## 待办

只在 `plan/BACKLOG.md` 一处。
