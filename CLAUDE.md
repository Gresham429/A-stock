# CLAUDE.md — A股观察台 / A-Share Watchdesk

> 给未来会话（换 session）的项目上下文。读完这份即可无缝接手，不必重新摸索。

## 这是什么

一个**本地运行**的 A 股看板：多股对比 + 点击深挖（含多周期行情图：分时+K线蜡烛(可选 MA5/10/20/60/120/240)）+ 顶部大盘研判条 + 全市场两级选股 + 持仓盈亏 + DeepSeek AI 推荐/建议（结果落盘缓存带时间戳）+ 近1年新闻/政策库 + 私域笔记 + 交易规则库（价格行为体系 + A股制度特性，可增删改、注入 AI）+ **投资画像本金分级玩法** + **公司叙事(做过/在做/要做)** + **AI 溯源与依据校验** + **全球宏观→板块指向** + 模拟盘 + 名词解释。
纯本地 Flask 后端代理各数据源，前端零构建（HTML+CSS+原生 JS）。**为什么不是托管网页**：深挖/加股票/调 AI/联网搜索都要实时外部请求，而托管型 Artifact 的 CSP 禁止一切外部请求，做不到。

## 快速开始

```bash
pip install -r requirements.txt      # 只依赖 flask
python app.py                        # http://127.0.0.1:5000
```

数据文件（`watchlist.json` / `portfolio.json`）首次运行自动生成，均已 gitignore。

## 文件地图

**入口**：`app.py`(Flask 路由，62 个) · `templates/index.html`(UI+CSS) · `static/app.js`(全部前端逻辑)
**app.py 的共享辅助已抽出**(2026-07-16，消除 agent_loop 的 `import app` 循环依赖)：
`screening.py`(选股/形态初筛：`_screen_rows`/`_pa_score`/`_safe_kline`/`_safe_metrics`…) ·
`ai_blocks.py`(AI 注入块：`_tier_block`/`_fee_block`/`_lesson_block`/`_agent_blocks`…)。
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
| `portfolio.py` | 持仓(按画像隔离 + 多笔 lot 模型)。**现金=可用现金**(买扣卖加) + `cash_reconcile` + 逐笔 `lots_view`(每笔买入费/市值/盈亏，展开用) |
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
| `factor_lab.py` | **因子回测与失效监控**(`data/factors.db`)：`backtest`(299只×600日=162,014样本/20s，顺带产出 **`excess_dist` 超额分位分布**=判罪线唯一来源) + `rank_of`(超额→历史分位) + `summary`(IC/t值) + **`direction()`动态定方向** + `rolling_ic`/`decay_alert`/`flip_rate` + `refresh_if_stale` + `backtest_stops`(止损网格) |
| `agent_store.py` | **Agent 持久层**(`data/agents.db`)：`agents`/`runs`(原文90天·结论365天) / **`lessons`(闭集9类)** / `pending`(限价挂单) / `conditions`(止损) / `claims`(时段原子占位) / `equity` |
| `agent_loop.py` | **日循环**：研判(`_market_block` 指数+K线结构+涨停跌停+板块强弱)→选股→**决策(可插拔 single/debate)**→风控(确定性硬门)→**挂单**→复盘(确定性失败检测)。`sweep_orders`/`sweep_conditions`/`current_slot` |
| `outcome.py` | **结果结算**(纯函数)：`forward_returns`(自成交价, 按**K线根数**数交易日) + `bench_returns` + `excess`(扣 beta)。地平线**引用** `factor_lab.HORIZONS`(5/10/20) 不复制。**只算不判罪** |
| `structure.py` | **K线结构摘要**(纯函数零网络)：`digest()` 出 MA5/20/60 + 近20日高低 + 最近3根 OHLC(带日期)；`fmt_stock`/`fmt_market` 成行喂 AI。**只给原料不下判断**——趋势由 AI 读均线自己判 |

### 测试（零依赖离线，`python3 tests/xxx.py` 直接跑；项目无 pytest）
`test_universe_store.py`(9) 板块解析 · `test_screen_branches.py`(5) 选股三分支 ·
`test_agent_gates.py`(10) agent 门+挂单 · `test_factor_lab.py`(6) 因子方向 ·
`test_structure.py`(12) K线结构摘要 · `test_outcome.py`(12) 结果结算 ·
`test_excess_dist.py`(14) 超额分布+判罪线 · `test_agent_memory.py`(6) 个体记忆 ·
`test_sector_backfill.py`(13) 板块聚合口径(`_agg_sector_payload`) —— **共 87 例**

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

- **DeepSeek**：`deepseek-v4-pro`（该账号**只有** v4-pro / v4-flash 可用）。是**推理模型**——`max_tokens` 同时覆盖「思考+正文」，太小会导致思考耗尽、正文返回空。已设 daily=8000 / position=5000 / screen=9000 / market_overview=6000，**别调小**。**温度统一 0.15**（`_chat` 默认值，求严谨理性、少发散）；`_DISCLAIMER` 强制「只据给定数据、不编造、不预测方向」。OpenAI 兼容 `POST /chat/completions`，支持 `response_format:{type:json_object}`，零 SDK（纯 urllib）。
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

**异步渲染必须带请求令牌**：多个慢请求写同一 DOM 目标时，用单调计数器
（`recSeq`/`detailSeq`/`mktSeq`/`secSeq`/`agSeq`）——发起即 `++`，`await` 回来后
`if(gen!==seq) return` 丢弃过期响应。**新增此类异步入口时照做**。

**数据源优先级**：能用腾讯/新浪（不封 IP）就别用东财；东财仅用于其独有数据且走
`em_get()` 限流。**⚠️ 东财按端点封 IP，`clist` 已封死** —— 见 `plan/PITFALLS.md#14`。

**按日累积的表一律要有 `purge()`** + 写入路径自动调用 + 容量在前端可见
（`sector_daily` 曾无清理，10 年会涨到 820MB）。

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
| **索引：plan/ 全部文档** | `plan/README.md`（本会话加，按主题分组） |
| 全市场股票池 + 板块日变化 | `plan/2026-07-15-full-market-universe-design.md` |
| **盘中 agent 调度器**（长期挂机也每桶自动跑） | `plan/2026-07-17-intraday-agent-scheduler-design.md` |
| **板块走势历史回填**（补齐一个季度） | `plan/2026-07-17-sector-history-backfill-design.md` |
| multi-agent + 失败归因 + 提示词进化（含**个体记忆**两层） | `plan/2026-07-16-agent-evolution-design.md` |
| **决策数据面**（K线结构 + 真·大盘块） | `plan/2026-07-16-decision-data-plane-design.md` |
| **结果导向教训**（超额结算 + 判罪线来自分布） | `plan/2026-07-16-outcome-driven-lessons-design.md` |
| AI 溯源与依据校验 | `plan/2026-07-14-ai-provenance-attribution-design.md` |
| 投资画像 · 本金分级 | `plan/2026-07-14-capital-profiles-templates-design.md` |
| 公司叙事 | `plan/2026-07-14-company-narrative-design.md` |
| 知识与缓存架构 L1–L5 | `plan/2026-07-13-knowledge-cache-architecture.md` |
| 卡在数据源的待办 | `plan/BACKLOG.md` |

### 关键机制速查

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
- **agent 记忆分两层**（2026-07-16）：**个体**——每 agent 决策只召回**自己的**教训
  (`for_ai(agent_id)`) + **自己的**战绩(`_agent_history_block`：历史建仓+20日超额结果)
  + 自己持仓；多-agent 对照才干净。**舰队**——用户面 5 个 AI 读**全体**汇总(`for_ai()` 无
  agent_id)。战绩块**只喂事实不让 agent 写反思**(反思=拟合噪音)。
  ⏳ **未做**：舰队→提示词提炼(D)，用户最终目标，等教训库有数据再落地。

## 冒烟测试（改完自测）

```bash
python3 tests/test_universe_store.py     # 板块解析 9 例（改 parse_tags 必跑）
python3 tests/test_screen_branches.py    # 选股三分支 5 例（改 _screen_rows 一带必跑）
python3 tests/test_agent_gates.py        # agent 门+挂单 10 例（改 run_day/幂等必跑）
python3 tests/test_factor_lab.py         # 因子方向 6 例（改 direction/打分必跑）
python3 tests/test_structure.py          # K线结构摘要 12 例（改 structure/决策提示词必跑）
python3 tests/test_outcome.py            # 结果结算 12 例（改 outcome/地平线/超额必跑）
python3 tests/test_excess_dist.py        # 超额分布+判罪线 14 例（改判罪/分布必跑）
python3 tests/test_agent_memory.py       # 个体记忆 6 例（改 for_ai/战绩块必跑）
python3 tests/test_sector_backfill.py    # 板块聚合口径 13 例（改 _agg_sector_payload/snapshot_daily/回填必跑）
# 全部零依赖、离线、不打网络。共 87 例。

# ⚠️ 改**决策提示词/数据面**后，必须实跑一个 debate 档（single 跑通≠debate 跑通，
#    辩论是 token 预算最短板；实测踩过两次）：
#    python3 -c "import agent_loop as al; print(al.run_day(18, dry_run=True, force=True))"

python3 -c "import ast; [ast.parse(open(f).read()) for f in ['app.py','agent_loop.py','screening.py','ai_blocks.py','outcome.py','structure.py','universe_store.py']]"
node --check static/app.js

python app.py &                          # ⚠️ 一律用 127.0.0.1 别用 localhost：
                                         #    localhost→::1→AirPlay 403（见 PITFALLS#18）
curl -s 127.0.0.1:5000/api/config
curl -s 127.0.0.1:5000/api/universe/status      # 池子：总数/eligible/板块回填进度
curl -s "127.0.0.1:5000/api/sectors?kind=sw1&limit=5"
curl -s 127.0.0.1:5000/api/factors              # 因子 IC/方向/翻转/新鲜度
curl -s 127.0.0.1:5000/api/agents               # agent 存档 + 教训汇总
# AI 类接口 30~90s → --max-time 200；有代理 → --noproxy '*'
```

## 安全（提交前必做）

- `.gitignore` 排除：`.env` / `.env.*` / `.claude/` / `.vscode/` / `__pycache__/` / `watchlist.json` / `portfolio.json`。
- **提交前铁律**：`git add -A` 后跑
  ```bash
  git ls-files --error-unmatch .env    # 必须报错(=未跟踪)
  git ls-files -z | xargs -0 grep -lE "sk-[A-Za-z0-9]{16,}"   # 必须无输出
  ```
- 绝不把 key / 个人邮箱 / 本地绝对路径写进被跟踪文件。
- 远程 `git@github.com:Gresham429/A-stock.git`（main 分支，MIT，Conventional Commits）。`gh` 未登录，推送走 SSH（已配好）。

## 代码风格

- 小文件（目标 <400 行）、类型注解、模块级 `logger`、不要裸 `except`、不硬编码密钥。
- 所有东财请求走 `em_get()`（内置限流）；新数据源优先选不封 IP 的腾讯/新浪。

## 当前状态 / 待办

**代码全部已推 GitHub(main)，工作区干净。74 例离线测试全过。app 单进程在跑。**
本会话(2026-07-16→17)约 21 个 commit，抽出 4 个新模块（`structure`/`outcome`/`screening`/`ai_blocks`）。

### 本会话做了什么（给下个会话的时间线）

1. **决策数据面**：补 `structure.py`(K线结构) + 真·大盘块 → agent 从「数据不足」变实质判断。
2. **结果导向教训 Phase 1+2**：`entries` 留痕 + `settle_entries` 超额结算；判罪线来自
   `factor_lab.excess_dist`(16万样本)，非拍脑袋。
3. **持仓口径**：主盈亏改「持仓盈亏=毛−已付买入费」(对齐券商)，卖出费按股单列「今日离场费」；
   逐笔展开 `lots_view`。
4. **agent 个体记忆**：每 agent 只召回自己的教训(`for_ai(agent_id)`) + 自己的战绩(`_agent_history_block`)。
5. **费率校正**：查实用户是**万2.5+5元最低**(非「免最低」，曾误记)；画像 6 已改。
6. **行情图**：日K 六条可选均线(MA5/10/20/60/120/240，芯片显当日值) + 分时/5日均价线(VWAP)。
7. **app.py 拆分**：1552→1187 行，抽 `screening.py`/`ai_blocks.py`，**消除 agent_loop 的 `import app` 循环依赖**。
8. **agent 舰队 12→20**：补齐 5 档 × 3 风险；画像选择器隐藏 agent 系列(不删)。

### 🔴 下一步该做的（按价值排序）

1. **让 20 个 agent 攒数据（唯一的主线待办，不卡代码、卡时间）** —— 教训库**仍是空的**，
   「学失败→反哺提示词」+ 个体记忆 + 结果导向判罪 **全部机制就位但零真实数据验证**。
   开 app 即自动跑（**盘中调度器每 5 分钟探一次，每桶一次；何时启动 app 都行**，2026-07-17 修）。
   - **第一批 20 日结算约 2026-08-13 落地**（今日建的仓满 20 交易日）；教训要「跑输历史 90%」才产生，稀疏。
   - ⚠️ 数据面修好 ≠ 一定成交：实测 07-16 大盘跌，AI **正确**拒绝逆势做多 → 0 成交。
     **要失败样本得等 AI 真愿出手的行情，别为攒数据松风控。**
   - ⚠️ 20 agent × 2 桶/天 × v4-pro，token 成本比 12 个时翻倍。

2. **舰队→提示词提炼（D，用户最终目标，等 #1 有数据再做）** —— 读全舰队教训+结算 →
   AI 提一版新提示词 → 用户审核 → `template_store` 入库 A/B。机制可建，但现在教训库=0、
   留痕=1，提炼无输入。见 `plan/2026-07-16-agent-evolution-design.md` 末尾。

3. **浏览器 DOM 交互验证** —— 本会话所有前端(MA六线/VWAP/持仓逐笔展开/画像隐藏/Agent modal/
   净盈亏)都只做了 **node 渲染测**(HTML 无 NaN/标签闭合)，**真实浏览器点击/展开一次没验**。

### ⚠️ 已知未验证 / 未做

- **未验证的阈值**（能验但没验）：`CHASE_HIGH_POS=85`、`STALE_DAYS=20`、`LOSS_CUT_PCT=-12`、
  `_PRESCREEN=600`。用 `factor_lab` 那套方法论可验。见 `plan/PITFALLS.md#1`。
- **纪律参数**（不需数据验证，但需用户认可）：`MAX_VOL=120`、`MAX_POS_PCT=30`、
  `MIN_CASH_PCT=10`、`VOL_FLOOR/CEIL=15/130`、`STOP_LOSS_PCT=-10`(已回测)、
  `LESSON_PCT=10`(教训判罪线=超额底部十分位，**用户 2026-07-16 已认可**，改前需重新征询；
  分布是 16 万样本的事实，「取底部 10%」是选择性取舍)。
- **测试盲区**：`fees` / `portfolio`(现金扣减) / `template_store` / `paper_store` 撮合 /
  `provenance` **均无单测**（本会话新增的 `structure`/`outcome`/`excess_dist`/`agent_memory` 已有测）。
- **`DebateDecider` 默认不启用**（UI 可选）：先用 single 拿基线，用数据证明需要再切。
- **launchd 定时全不挂**（用户决定）：agent「不开 app 就意味着那天不炒股」——但**只要 app 开着**，
  盘中调度器就每桶自动跑（无需在交易时段启动，2026-07-17 修）。
  板块统计「每交易日都开 app，`_universe_boot` 已覆盖」。

### 🔒 卡在数据源（代码已就位，拿到源即接；用户要求提醒他加）

见 `plan/BACKLOG.md`：
1. **`net20` 资金分量无法回测** —— 新浪 MoneyFlow 只给 30 天，因子回测要 ≥600 天。
   故 `_pa_score` 四分量里唯独资金项未验方向、不参与打分。
2. **宏观缺 美元指数/VIX** —— 新浪外盘没有。接上只需 `ds.global_markets()` 加两条目。

### 本地数据文件（全部 gitignore）

`watchlist.json` · `portfolio.json`(按画像隔离+lot 模型) · `ai_cache.json` ·
`data/`: `news.db` `notes.db` `rules.db` `paper.db` `profiles.db` `universe.db`
`templates.db` `agents.db` `factors.db`

### 用户侧待办（我做不了）

- **佣金已落定(2026-07-16 生效)**：画像 6 = 万2.5 + 5元最低（券商拒免最低；印花/过户法定）。
  对用户单笔~1400 元不省（仍触 5 元最低）；单笔 >20000 元才吃到费率。已配置，无待办。
  - 可选后续：**逐笔费率快照**——费率随时间变，现画像只存单一费率。用户资金变大、单笔超
    5556 元后，历史笔与新笔费率不同才需要；小单因都触最低而无影响，暂不做。
