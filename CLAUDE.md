# CLAUDE.md — A股观察台 / A-Share Watchdesk

> 给未来会话（换 session）的项目上下文。读完这份即可无缝接手，不必重新摸索。

## 这是什么

一个**本地运行**的 A 股看板：多股对比 + 点击深挖（含多周期行情图：分时+K线蜡烛）+ 顶部大盘研判条 + 全市场两级选股 + 持仓盈亏 + DeepSeek AI 推荐/建议（结果落盘缓存带时间戳）+ 近1年新闻/政策库 + 私域笔记 + 交易规则库（价格行为体系 + A股制度特性，可增删改、注入 AI）+ **投资画像本金分级玩法** + **公司叙事(做过/在做/要做)** + **AI 溯源与依据校验** + **全球宏观→板块指向** + 模拟盘 + 名词解释。
纯本地 Flask 后端代理各数据源，前端零构建（HTML+CSS+原生 JS）。**为什么不是托管网页**：深挖/加股票/调 AI/联网搜索都要实时外部请求，而托管型 Artifact 的 CSP 禁止一切外部请求，做不到。

## 快速开始

```bash
pip install -r requirements.txt      # 只依赖 flask
python app.py                        # http://127.0.0.1:5000
```

数据文件（`watchlist.json` / `portfolio.json`）首次运行自动生成，均已 gitignore。

## 文件地图

| 文件 | 职责 |
|------|------|
| `app.py` | Flask 路由（后端入口） |
| `datasources.py` | 数据层：行情/指标/研报/龙虎榜/解禁/K线/财报/新闻/快讯 + `index_quotes`(五大指数+两市成交额)/`market_breadth`(涨跌家数/涨停跌停/行业冷热) + `tencent_minute`(当日分时)/`sina_kline(scale=)`(日K/5分钟) + `announcements`(东财公告)/`concept_tags`(东财概念板块，供公司叙事) + `global_markets`(新浪外盘：WTI/布伦特原油·黄金·铜·道指/纳指/标普，供全球宏观 digest；该源无美元指数/VIX) + `sina_all_stocks`(**全A名单+快照** 5527只/70页并发~12s，自带涨跌幅/成交额/换手/市值/PE/PB → 名单与板块统计共用一份数据) |
| `llm.py` | DeepSeek 集成：`daily_recommendation` / `market_screen` / `position_advice` / `entry_advice`(深度入场分析) / `market_overview` / `structure_note`(v4-flash) / `company_profile`(v4-flash 公司叙事三段) / `macro_digest`(v4-flash 全球宏观→A股板块指向)；温度默认 0.15 |
| `ai_cache.py` | L1 AI 输出短期缓存（`ai_cache.json`，智能命中 + 时间戳） |
| `news_store.py` | L2 新闻/政策库（SQLite `data/news.db`，滚动1年）+ 抓取/去重/清理 + `is_trading_day`(动态节假日) |
| `fetch_news.py` | 新闻抓取入口（供 launchd `launchd/com.astock.news.plist` 定时 / 命令行调用） |
| `notes_store.py` | L5 私域笔记（SQLite `data/notes.db`，永久保留） |
| `rules_store.py` | 交易规则库（SQLite `data/rules.db`，84 条：67 蒸馏自 PA_Agent + 17 A股制度特性，13 类，带场景标签，注入 AI）；`for_ai` 注入带 `[R{id}]` 供引用，`active_rule_map` 供校验；`templates/prompts/` 存归档提示词 |
| `provenance.py` | AI 建议溯源与依据校验（仅 entry/position）：闭集信号字典 `SIGNAL_DEFS`（单一事实源）+ `build_provenance`（A 确定性溯源）+ `verify_basis`（B 校验 AI 引用的信号名/规则ID→✓可核对/⚠对不上/·名对值缺） |
| `paper_store.py` | 模拟委托交易（SQLite `data/paper.db`，多存档，按真实行情+A股规则[整手/涨跌停/T+1/手续费]撮合） |
| `websearch.py` | 博查联网搜索（B 方案，可选）+ key 健康检测/到期提醒 |
| `portfolio.py` | 持仓 + 总盈亏 + 当日盈亏。**按投资画像隔离**（`portfolio.json={by_profile:{pid:[...]}}`，内部按 `profile_store` active 画像 scope）+ **多笔 lot 模型**（`{code, lots:[{shares,cost,date}], note}`：同股 `add` **追加一笔**不覆盖 → 总股数Σ、成本加权平均；**当日盈亏逐笔**：当天买入的用「该笔买入价」作基准、之前持有的才用昨收）。旧格式（裸列表/扁平单笔）**只读时内存迁移**、写入时落盘 |
| `profile_store.py` | 本地多档投资画像（SQLite `data/profiles.db`）：每档=现金本金+风险偏好，一个 active；**5 档 TIERS**（微/小/中/大/超大，按总资产=现金+持仓 落档）+ 每档玩法 template + `block_for_ai` 注入块 |
| `universe_store.py` | **全市场股票池**（SQLite `data/universe.db`）：全A名单 5527(新浪 hs_a) + 板块归属(东财 slist 逐股回填) + 板块日变化统计。对外 `codes_of(focus)`/`sector_of`→(申万一级,细分)/`sectors_map(codes)`批量/`taxonomy()`/`status()`/`snapshot_daily()`/`sector_ranking()`/`sector_history()`。**签名与 `universe.py` 一致，未回填时自动降级手工池** |
| `universe.py` | **降级为「精选龙头」标记**（10 一级 → 48 二级 → 170 只）。`universe_store` 未就绪/未回填时的 fallback + `is_leader` 标记。不再是主候选池 |
| `tests/test_universe_store.py` | 板块解析回归测试（7 例，零依赖，`python3 tests/test_universe_store.py` 直接跑；项目无 pytest） |
| `store.py` | 自选股持久化 |
| `config.py` | 读 `.env`（DeepSeek + 博查 key，绝不硬编码） |
| `templates/index.html` | UI 结构 + 全部 CSS（深色终端风） |
| `static/app.js` | 全部前端逻辑（对比/深挖/波动/大盘条/持仓/AI+缓存/新闻/笔记/规则/名词） |

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

## 约定

- **红涨绿跌**（A 股惯例，与美股相反）——所有涨跌/盈亏配色遵此。
- **情景区间** = 年化波动率反推（±1σ≈68%、±2σ≈95%），**只描述波动幅度，不预测方向**。
- 所有 AI 输出必须标注「参考信号，不构成投资建议」。
- 前端字体用系统栈（`PingFang SC`/`Microsoft YaHei`…），**不加载 Google Fonts**（国内会失败）；图表纯内联 SVG，零外部依赖。
- **异步渲染必须带请求令牌**：多个慢请求（daily/screen/openDetail/大盘研判）写同一 DOM 目标时，用单调计数器 `recSeq`/`detailSeq`/`mktSeq`——发起即 `++`，`await` 回来后 `if(gen!==seq) return` 丢弃过期响应，否则切换时旧响应会覆盖新视图（错位 bug）。新增此类异步入口时照做。
- **大盘研判**：`GET /api/market/overview`（`?ai=0` 只取指数走腾讯快返；完整版含东财情绪 + AI 研判，走 `ai_cache` 短期缓存 5 分钟）。选股 `market_screen` 前先取（缓存的）大盘结论注入，让个股筛选与大盘攻防一致。前端顶部常驻「大盘研判条」，`market_breadth` 走东财失败即降级 None、不阻断。
- **新闻/政策资讯库（L2）+ 实时热点（L4）**：`news_store.py`（SQLite `data/news.db`，gitignore）滚动近 1 年；表 `news` 按 `UNIQUE(title_hash)` 去重，可按 板块/个股/kind/天数 查；抓全市场快讯(财联社+东财7×24) + 自选股新闻 + 研报。抓取入口 `fetch_news.py`（供 launchd `launchd/com.astock.news.plist` 每日 08:40/11:40/14:00/15:30/20:30 调；非交易日门控见 `is_trading_day`（法定节假日按年动态抓 holiday-cn `{年}.json` 经 jsDelivr、缓存 db meta、抓不到退化为纯工作日，自动跨年无需手改），仅晚间真抓）。看盘时前端 `maybeRefreshNews()` 交易时段每 15 分钟惰性增量；手动「📰 新闻」modal + 🔄刷新（`POST /api/news/refresh` 后台线程）。首次启动 `__main__` 后台 `backfill()` 回填 1–2 季度。**L4**：`_ai_web_context` 先注入本地库相关新闻（带日期供 AI 按新鲜度加权）再叠加实时快讯 + 博查。`GET /api/news` / `/api/news/status`。**L3 按需远期**：`news_store.deepen(code)` 深抓单只更久新闻+研报；个股 AI 分析时若本地稀疏(<5 条)自动深抓；`POST /api/news/deepen{code}` + 新闻 modal 代码框手动拉。
- **单股深度入场分析**：`llm.entry_advice` + `POST /api/recommend/entry/<code>`（不必持仓，深挖抽屉「🎯 深度入场分析」）：是否/何时/怎么买 + 未来卖出策略预判，严格遵循规则、结合资金；`ai_cache` kind=entry。与「🤖 该买还是该卖」(position) 并列。
- **AI 溯源与依据校验（provenance，仅 entry/position）**：两层，主次分明。**A 数据溯源**（后端确定性、100%稳定）：响应带 `provenance`＝本次喂了哪些源+新鲜度+条数（行情/波动资金/60日波动史/财报/新闻/规则[场景]/大盘/联网），前端面板顶部溯源条常驻、点开展值级明细。**B 结论依据**（AI 自述+后端校验）：提示词给【可引用信号】闭集（`provenance.signal_vocab`），AI 每条关键结论在 `basis` 里只引用**信号名 + 规则ID（不写值）**；`verify_basis` 后端**权威填值**（值不可能被 AI 编）并校验（名在闭集/规则ID在注入集→`ok`✓；否则`bad`⚠；名对源缺→`na`·），前端每条结论「依据」默认收起、✓绿可核对/⚠红对不上。范围仅这两个单股分析，daily/screen/market 暂不做。设计见 `plan/2026-07-14-ai-provenance-attribution-design.md`。
- **公司叙事（做过/在做/要做，company_profile）**：两层。**单股深版**：entry/position 顶部「公司叙事」卡（做过/在做/要做 + 题材标签）+ 注入主分析——`llm.company_profile`(**v4-flash**) 据 近期新闻 + 财报多期 + 东财公告(`ds.announcements`) + 概念板块(`ds.concept_tags`) 合成 `{did,doing,will,tags}`；`ai_cache` kind=`profile` **当日缓存 12h**（叙事变化慢，同日不重复调），`app._profile_block` 拼进 entry/position 的 `web_ctx`。**每日推荐简版**：`daily_recommendation` 给每只自选股附本地新闻库近期标题（`news_store.query`）→ 输出每 pick 一句话 `narrative`，**0 额外 LLM**（同一次 daily 调用）。数据源纯 HTTP（避开 mootdx，海外可用）；叙事定性、不进 provenance 的 basis 校验，只作输入与展示。设计见 `plan/2026-07-14-company-narrative-design.md`。
- **投资画像 · 本金分级玩法（profile_store）**：本地多档建档（现金本金+风险偏好，一个 active，`GET/POST/PUT/DELETE /api/profiles`、`POST /api/profiles/active/<id>`）；**持仓每档独立**（方案 B，`portfolio` 按 active 画像隔离）。**分级 = 总资产（active 现金 + 该画像持仓市值）** 自动落 **5 档**（微型<3万/小型3–30万/中型30–200万/大型200–2000万/超大≥2000万），每档一套玩法 template（持仓只数/单标的仓位上限/标的池/周期/单笔风险/现金/正期望锚），核心=期望为正（小搏赔率、大求稳）。`app._tier_block()` 拼【本金玩法档】块**前置注入 5 个 AI**（daily/screen/position/entry/大盘）；**entry/screen 资金改用 active 画像现金**（全市场选股不再手选资金档、跟随画像，前端只显示「X万（画像名）· 跟随画像」，`body.capital` 仍可覆盖）；切画像自动同步 `rules_store` 场景本金维。前端「💼 画像」modal（切换/新建/改本金/删 + 当前档+玩法 + 5档表）。`data/profiles.db` gitignore。**踩坑**：超大档 `hi=inf` 要 `_safe_tier` 转 None 再 jsonify，否则浏览器 `JSON.parse` 崩。设计见 `plan/2026-07-14-capital-profiles-templates-design.md`。
- **全球宏观/地缘 digest（macro_digest）**：外围（中东/俄乌地缘、大宗商品、美联储、汇率、外围股市）对 A股有指向性，但原来只混在通用快讯里、AI 抓不住。现 `llm.macro_digest`(**v4-flash**) 据 `ds.eastmoney_global_news`(东财7×24全球) + `ds.cls_telegraph`(财联社) + **`ds.global_markets`(外围实时数值：油价/黄金/铜/美股三大指数)** 提炼 `{points(要点), sector_map(→利好/利空哪些A股板块), bias(外围倾向)}`；`app._macro_block()` 拼块**前置注入 5 个 AI**（与 `_tier_block` 并列）；`ai_cache` kind=`macro` **当日 6h 缓存**（首次≈5s，之后 0s）。**传导由 AI 推理**（喂全球要点 + 个股板块/概念），不硬编「事件→板块」映射表。实测能自动产出「WTI涨1.13%/布伦特涨1.45%→利好石油开采/油服，利空航空/化工下游」「黄金跌0.73%→压制黄金板块」，并能识别内外背离。**数值优先于新闻措辞**（提示词明确要求）。
- **模拟委托交易**：`paper_store.py`（SQLite `data/paper.db`）多存档；`_market_open()` 交易时段门控；`order()` 即时撮合按真实 quote + A股规则（整手/涨跌停封板/T+1[lock_date 结算]/手续费[佣金万2.5最低5+卖印花0.05%+过户0.001%]），拒单也记 orders。`GET/POST /api/paper/accounts`、`DELETE /api/paper/accounts/<id>`、`GET /api/paper/account/<id>`、`POST /api/paper/order/<id>`。前端「💰 模拟盘」modal（存档增删 + 下单 + 持仓/委托）。
- **交易规则库（PA_Agent 蒸馏）**：`rules_store.py`（SQLite `data/rules.db`，gitignore）存规则卡（蒸馏自 [PA_Agent](https://github.com/rosemarycox5334-debug/PA_Agent) 的 Al Brooks 价格行为体系，适配 A股、剔除"不依赖成交量"；另加 **A股制度特性** 17 条：T+1/涨跌停/集合竞价/打板生态/情绪市/换手率T+1含义等）。分 **13 类**（…含「资金与周期」「风控执行」「A股制度特性」），共 **84 条**，可增删改 + 启用停用；`seed()` **加性补入**（只补缺失，不动用户改动，并回填场景标签）。**规则按场景灵活启用**：每条带 `scenarios` 标签（本金 小/中/大 × 周期 短线/波段/长线，空=通用）；`get_scenario()`/`set_scenario()`（存 meta，默认「小,波段」，`POST /api/rules/scenario`）；`for_ai(scenario)` 只注入「通用 或 命中当前场景」且启用的规则。`signature()`（场景+生效规则集哈希）进 4 个 AI 调用的**缓存指纹**→改场景/改规则即失效重算。`GET/POST/PUT/DELETE /api/rules`；前端「📐 规则」modal（场景选择器 + 分类 + 增删改启停 + 场景标签，未命中当前场景的规则灰显）。**daily/position 分层**：`daily_recommendation` 是组合速览（持仓默认 hold/watch、不硬 sell，标 held），`position_advice` 是持仓权威判断（加 `hold_horizon`/`rule_basis`）；两者提示词都显式要求「严格遵循【交易分析框架规则】」，`_DISCLAIMER` 加硬性⑥。前端对比表/推荐面板加 `.ov-note` 明确「对比是总览≠买卖意见」。
- **私域信息笔记（L5）**：`notes_store.py`（SQLite `data/notes.db`，gitignore，**永久保留不清理**，每条带 `created_at`）。打字/贴文本记录；`llm.structure_note`（**deepseek-v4-flash 快模型**，`_chat(model=)` 覆盖）把笔记 AI 结构化为 `{summary,codes,sectors,tags,kind}`，存前确认。`GET/POST /api/notes`、`POST /api/notes/structure`、`DELETE /api/notes/<id>`；前端「📝 笔记」modal。`_ai_web_context` 注入相关笔记标注【我的私域笔记·日期】与客观数据区分。**隐私**：AI 整理会把内容发 DeepSeek，纯手动存不外发。
- **AI 输出短期缓存（L1）**：`ai_cache.py` 把 4 个 AI 调用（daily/screen/position/market）结果落盘 `ai_cache.json`（gitignore）。key = `kind:输入指纹:当日`，**指纹只哈希影响结论的输入（自选/持仓/资金/板块/代码），排除实时价格**；TTL 个股/每日/选股 30min、大盘 5min；命中且未过期→秒回（跳过取数+LLM），输入变/跨交易日/TTL 过期/带 `force`(body 或 `?refresh=1`)→重算。响应带 `cached/analyzed_at/age_min`，前端每个 AI 面板显示 `aiMeta()` 时间戳行 +「🔄 强制刷新」。`position` 缓存与 `FOLIO_ADV` 持久化叠加（`folioAdvice` 切换开合、`loadFolioAdvice` 真正拉取）。这是「知识与缓存架构 L1–L5」的第一层，见 `plan/2026-07-13-knowledge-cache-architecture.md`。
- **全市场股票池 + 板块日变化（universe_store，2026-07-15）**：手工 170 只龙头池 → **全A 5527 只**（eligible 4989，自动排除 ST 211/北交所 327/停牌 3）。三层：**数据层**（`refresh_roster()` 新浪 hs_a 12s，每日；`backfill_sectors()` 东财 slist 逐股回填 ~100min，**断点续传**，`sectors_at` 空的才抓）→ **统计层**（`snapshot_daily()` 按板块聚合当日快照 → `sector_daily` 表，432+ 板块/5.4s，支撑跨日对比）→ **选股层**（`codes_of(板块)` → 板块内算形态 → `_pa_score` 排序取 36）。API：`GET /api/universe/status`、`POST /api/universe/refresh`、`GET /api/sectors?kind=sw1|sub|concept`、`POST /api/sectors/snapshot`、`GET /api/sectors/<name>`。前端「🧭 板块」modal。`_universe_boot()` 启动后台预热。设计+踩坑见 `plan/2026-07-15-full-market-universe-design.md`。
  - **板块分类**：东财 `slist` 返回混合标签 → `parse_tags()` 解析。**申万一级靠硬编码 31 类闭集 `SW1_SET` 匹配**（标签顺序不稳定、`Ⅱ/Ⅲ` 后缀只有部分股票带，靠顺序/后缀解析不可靠）；**细分**取前 4 标签里第一个非一级/非地域(结尾「板块」)/非指数(结尾「_」)——不严格等于申万二级（中芯国际得「集成电路制造」而申万二级是「半导体」）但语义正确，**措辞用「细分」勿称「申万二级」**。多对多归属（一股多板块），但 `stocks.sw1/sub` 存主行业保住 `sector_of` 单归属契约。
  - **`_pa_score`（形态初筛，0–100）**：四项各 25 分，**权重是启发式先验、非回测**：可分析性(`vol=None` 出局 → 次新/停牌在此自然剔除)/波动(年化30–90%满分)/资金(主力20日净流入)/动量(20日涨幅0–30%)/位置(区间位置30–70，避追顶接刀)。只是**粗筛**（决定谁占 AI 的 36 名额），真正 PA 判断仍归 `rules_store` 84 条规则。形态初筛**只在 focus 板块且池≤200 时生效**；全市场路径退回 `_PRESCREEN=600` 流通市值预筛 + 均衡采样。
- **选股候选池两级化**（历史，现由 universe_store 接管）：`focus` 可传一级板块名 / 二级细分名 / 空（全市场）；`_screen_rows` 按 `codes_of(focus)` 取数 + 可负担过滤 + `_balanced_pick` 跨一级轮询均衡采样（每二级≤3 只、全市场 cap 36；下钻二级时放宽到 6）。前端 `scr_focus` 用 `<optgroup>` 由 `taxonomy` 动态渲染。
- **单股行情多周期**（深挖抽屉「行情」标签，原「波动」折线 + 「K线/箱形」两标签已合并）：`GET /api/wave/<code>` 一次并发返回 `{intraday(腾讯分时), min5(新浪5分钟×5日), daily(新浪日K×260, 含 OHLC+成交量), prev_close}`；前端周期 `分时/5日/近1月/近3月/近半年/近1年`——**分时/5日走内联SVG折线**（`waveHover` 十字准星），**日K四档走蜡烛图**（`candlestick`：MA5橙/MA20蓝 + 成交量柱，`klHover` 悬停显示当日 OHLC+涨跌%+成交量(手)+换手率(近似)，下方箱形图）。日K四档统一按**自然日窗口**（`今天−30/−90/−180/−365 天` 过滤 daily，≈20/60/117/242 根各不同——**修掉旧版 60日≈90天 单位混用导致的重叠**）；MA5/MA20 在**完整日K序列预计算再截窗**，防窗口内均线缺头。**分时自动刷新**：抽屉开 + 当前分时标签 + 北京时间交易时段（`_cnTradingNow`，用 `Asia/Shanghai` 判定、不看本地时区）时，`tickMinute` 每 30s 拉轻量 `GET /api/minute/<code>`（返回 `{intraday,prev_close}`）重渲染，带 `detailSeq` 令牌防串；关抽屉/切走标签/非交易时段停刷。分时基准=昨收，日线周期算年化波动。异步随 `detailSeq`。
- **换手率指标**：**实时换手率**来自 `tencent_quote.turnover`(腾讯行情第38位，`_build_row` 的 `{**q}` 本就带)，进**对比表「换手%」列**(可排序)+**深挖概览**，并注入 `daily_recommendation`/`position_advice`/`entry_advice` 三个 AI 提示词(标「交易活跃度」)。**历史每日换手率**不接封IP风险的东财，改用**近似**：`/api/wave` 透出 `float_shares`(流通股本=`float_mcap_yi`×1e8÷现价)，前端日K蜡烛 `klHover` 现算 `成交量÷float_shares` 显示。换手率∝成交量(流通股本近似恒定)，故不画独立副图(会与成交量柱重复)、只在 hover 补。

## 冒烟测试（改完自测）

```bash
python app.py &                                   # 起服务
curl -s localhost:5000/api/config                 # llm/news/web_search 状态
curl -s localhost:5000/api/overview | head -c 300 # 自选股对比
curl -s "localhost:5000/api/websearch/status?probe=1"   # 博查健康(到期提醒)
curl -s localhost:5000/api/universe/status        # 全市场池：总数/eligible/板块回填进度
curl -s "localhost:5000/api/sectors?kind=sw1&limit=5"   # 板块日排行
# AI 类接口慢(推理 30~90s)，用 --max-time 200
```

Python 语法：`python3 -c "import ast; [ast.parse(open(f).read()) for f in [...]]"`；JS：`node --check static/app.js`。
回归测试：`python3 tests/test_universe_store.py`（板块解析 7 例，零依赖、离线，改 `parse_tags` 必跑）。

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

- **代码全部已推 GitHub（main），工作区干净。** 已上线（★=2026-07-14/15 这轮新增）：
  - ★**全市场股票池**：手工 170 只 → **全A 5527 只**（eligible 4989）动态名单 + **884+ 板块**（申万一级/细分/概念）+ **板块每日变化统计**（可跨日对比）+ 形态初筛（`_pa_score`）。概念级选股（如「商业航天」）为手工池不可能做到
  - 全市场选股（★资金规模跟随投资画像、不再手选；★候选池换 universe_store）+ 大盘研判
  - ★**单股多周期行情图**：分时折线 + 日K蜡烛(MA5/MA20+成交量)，分时交易时段自动刷新；原「波动」「K线/箱形」两标签已合并为「行情」
  - ★**换手率**：对比表列(可排序) + 深挖概览 + 注入 3 个 AI；日K hover 给近似历史换手
  - ★**AI 溯源与依据校验**（provenance，仅 entry/position）：A 确定性溯源条 + B 闭集可验证依据(✓/⚠)
  - ★**公司叙事**（做过/在做/要做）：单股深版(flash 合成+当日缓存+注入) + 每日推荐一句话简版(0 额外 LLM)
  - ★**投资画像 · 本金分级**：多档本地建档 + 总资产(现金+持仓)落 5 档玩法 template + **每档独立持仓** + 注入 5 个 AI
  - ★**全球宏观 digest**：外围数值(油价/黄金/铜/美股) + 全球快讯 → A股板块指向，注入 5 个 AI
  - ★交易规则库 **84 条** = PA_Agent 蒸馏 67 + **A股制度特性 17**（T+1/涨跌停/集合竞价/换手率T+1含义…）
  - 知识与缓存架构 L1–L5、launchd 定时抓取（动态交易日历）、模拟盘
- **⚠️ 待用户浏览器验证**（后端均已 LIVE 实测通过，DOM 交互未验）：★**🧭 板块 modal**（一级/细分/概念切换、点板块看成分股、点成分股跳深挖）；💼 画像 modal（切换/新建/改本金）；单股分析顶部「公司叙事卡 + 溯源条 + 结论依据 ✓/⚠」；每日推荐每只一句话叙事；AI 结论是否体现「全球宏观 + 本金玩法档」。
- **⏳ 板块归属回填**：首次需 ~100 分钟（4989 只 × em_get 限流 1.25s）。**断点续传**，app 启动时 `_universe_boot()` 自动续跑；也可 `curl -X POST localhost:5000/api/universe/refresh`。回填期间板块口径是混的（已回填→申万、未回填→降级手工池、都没有→其他/其他），跑完自动一致，**不需要改代码**。进度看 `GET /api/universe/status` 的 `sectors_tagged/eligible`。
- 本地数据文件（gitignore，用户机上）：`watchlist.json` / `portfolio.json`（新格式 `{by_profile:{pid:[...]}}` **按画像隔离**，旧格式自动迁移）/ `ai_cache.json` / `data/news.db` / `data/notes.db` / `data/rules.db` / `data/paper.db` / `data/profiles.db` / ★`data/universe.db`（全A名单+板块归属+板块日统计）。
- **backlog 已清空**，只剩**可选**后续（见 `plan/BACKLOG.md`，均未承诺）：美元指数/VIX 换源补齐（新浪外盘无此二者）、溯源推广到 daily/screen、5 档 template 文案做成 UI 可编辑。
- launchd 定时任务需**用户在自己终端** `launchctl bootstrap` 安装（本环境无 `~/Library` 写权限）；见 README「自动抓取新闻库」。
- 设计文档在 `plan/`（各特性 spec + 知识缓存架构总纲 + `BACKLOG.md`）。
- **踩坑备忘**：① 端口 **5000 被 macOS AirPlay 占**，本地起服务前关「隔空播放接收器」，或测试用 5001；② 涉及用户数据文件（`portfolio.json` 等）的测试**必须用临时副本**，别写真实数据；③ AI 类接口是推理模型，单次 30~90s，`curl` 用 `--max-time 200`；终端有代理时 `curl` 加 `--noproxy '*'`（python 脚本要 `os.environ.pop` 掉 `*_proxy`）。
- **⚠️ 全市场池踩过的坑（改这块前必读）**：
  - **`ds.market_prefix()` 有 bug**：把 `9` 开头判为 `sh`（沪B 老逻辑），但**北交所新号段是 920xxx**（新浪返回里有 `bj920000`）→ 会误判成上海；`4xxxxx` 也错落 `sz`。**未修**（怕影响既有调用），`universe_store.is_bj()` 独立判 `4/8/92` 绕开。**这函数仍是雷**。
  - **扩池会引爆手工池永不触发的崩溃**（已修，勿回退）：`tencent_quote` 单 URL 拼全部代码 → 全池 44KB 报 **HTTP 414**（实测 800 只可过、2000 只失败）→ 已 `QUOTE_CHUNK=400` 分批并发；`_annualized_vol` 遇 0 收盘价 → **math domain error 穿透 `executor.map` 打崩整个选股**；`sina_metrics` 的 `closes[-21]==0` ZeroDivisionError + try 块外的 `float(x['trade'])` ValueError。兜底见 `app._safe_metrics`。
  - **slist 标签去重顺序坑**：东财同板块会以 `银行Ⅱ` 与 `银行` 两种形态返回。若按**原始标签**分类却按**去后缀名**去重，`银行Ⅱ` 判成 concept 抢占去重位 → **`sw1` 标记永久静默丢失**（`codes_of('银行')` 照常能用、只有 `sector_ranking(kind='sw1')` 漏板块，极隐蔽）。已固化为回归用例。
  - **回填必须跨进程互斥**：`ds.em_get` 限流器是**进程内**全局（`_em_last_call`），app 与命令行同时回填 → 东财请求速率翻倍 → clist 就是这么被封的，slist 再被封功能全废。故用 db 心跳锁（`meta.backfill_hb = pid,ts`，超 120s 可抢占）+ `threading.Lock` 双重互斥。
