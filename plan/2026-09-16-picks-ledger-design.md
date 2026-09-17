# 三周期选股 + 自选股买卖点 + 观点账本：设计（2026-09-16）

状态：已实现并合并 main（2026-09-16）

## 要解决的问题

现在的推荐（每日推荐 / 全市场筛选 / 入场分析）是无状态的：每次重新生成，不知道自己上次说过什么，
所以会出现"今天说买、明天说卖"却不解释。用户要的是：短 / 中 / 长三个周期各有一份推荐；自选股每只给
买点、卖点和时间窗；模型对自己之前的结论负责，改口要有理由；这些买卖点之后要能触发手机提醒（子项目 C）。

## 用户已确认的决定

1. 周期定义：短线 1 到 5 个交易日（交易活跃、低吸高抛、给具体价位）；中线 2 到 8 周（板块轮动、趋势
   刚起、价位区间加趋势破坏即离场）；长线 3 到 12 个月（基本面、估值，分批建仓区间）。
2. 候选范围：三个周期各从全市场选 5 只；自选股每只都给它最适合周期下的买点 / 卖点 / 时间窗。
3. 改口规则：有效期内只在触发事件时允许修改，触发事件四种：止损触发、目标达成、周期到期、新信息推翻。
   模型必须点名是哪一种，否则只能维持。
4. 运行时刻：交易日 16:00 全量（三周期 + 自选股 + 账本核对）；09:05 公共三周期只跑短线，自选股不分早晚、两个时刻都是同一份覆盖全部周期的完整提示词；页面手动按钮随时可跑，计入个人日额度。
5. 记忆注入方式（用户补充）：长滑动窗口放全文，远期只放摘要加相关性挑选，控制开销。

## 设计

### 1. 观点账本（picks_store.py）

每人一个库 `data/users/<uid>/picks.db`（个人数据，走 userctx 路径解析）。全市场三周期的公共结论放
`data/picks_public.db`（公共数据）。两个库同一张表结构：

```
calls(
  id, run_id, created_at,
  scope        -- public | watchlist
  code, name, horizon        -- short | mid | long
  stance       -- buy | watch | avoid | sell
  entry_lo, entry_hi, exit_lo, exit_hi, stop,
  valid_from, valid_until,
  thesis, trigger_note,      -- 理由；什么情况下会改
  basis_json,                -- signals / rules，同现有溯源格式
  decision     -- new | keep | revise | withdraw
  trigger      -- none | stop_hit | target_hit | expired | thesis_broken
  parent_id,                 -- 修改链：新行指向被它替换的旧行
  status       -- open | superseded | withdrawn | expired
  px_at_call, max_up, max_dn, ret_5d, ret_20d, touched  -- 结果贴回（每日收盘后更新）
)
```

修改不覆盖旧行：新行 `parent_id` 指向旧行，旧行 `status=superseded`。撤销只改旧行 `status=withdrawn`。
到期由每日结算把 `valid_until < today` 的 open 行标 `expired`。

改口规则在代码里强制：`decision=revise` 且 `trigger=none` 的返回按 `keep` 落库并记 warning；
`trigger=stop_hit / target_hit` 还要与结果贴回的 `touched` 字段一致，不一致同样降为 `keep`。

有效期按周期：短线 5 个交易日，中线 40 个交易日，长线 250 个交易日（用 news_store.is_trading_day 数）。

### 2. 记忆注入（picks_store.memory_block）

给模型的"上次说过什么"分两层，每只股票一段：

- 近窗：最近 60 个自然日内的记录全文，最多 12 条（按时间倒序），每条一行：日期、周期、立场、买点、卖点、
  止损、决定与触发、结果（给出后最高 / 最低 / 5 日收益 / 是否碰到）。
- 远期：60 天以外只放两样。一是确定性汇总，从账本直接算、零 LLM：共几次观点、买点被碰到的比例、
  给出后 20 日收益均值与最差、最近一次立场；二是相关性挑选最多 3 条旧记录，条件是买点或卖点落在今日价
  正负 5% 内，或周期相同且结果为止损触发 / 目标达成（这两类最值得提醒模型）。

每只股票控制在 300 token 左右；20 只自选股约 6k token，一次运行的记忆开销固定在这个量级，不随历史增长。
不用 LLM 生成远期摘要：确定性汇总不漂移、不花钱；需要时再加一档"每周一次 flash 摘要"。

### 3. 买卖点定价（picks_levels.py，纯函数）

不让模型报无来源的价格。代码先算候选价位：

- 近 5 / 10 / 20 / 60 日高低点；MA5 / MA20 / MA60（复用 structure.digest）；
- 最近的摆动高低点（局部极值，左右各 2 根确认）；
- ATR 上下带（20 日平均振幅，正负 1 倍、2 倍）；
- 短线专用：近 10 日低点簇（相距 1% 内合并）作买点，高点簇作卖点。

候选连同当前价、换手率、振幅一起给模型；模型只能从候选里挑买点区间、卖点区间、止损并说明理由。
返回后校验：每个价位必须落在某个候选价正负 1% 内，否则替换成最近的候选价并在 `basis_json` 标记
`adjusted`。这是现有 provenance 的同一思路。

### 4. 三周期候选池（picks_pipeline.py）

| 周期 | 候选来源 | 压到 30 只的打分 |
|---|---|---|
| 短线 | 当天复盘的涨停 / 炸板 / 题材池（review.store 最新一份）+ 全市场按换手率、振幅前列 | 换手率、振幅、区间位置低、题材命中 |
| 中线 | universe_store.sector_ranking 前列板块里的龙头 + 主力 20 日净流入持续为正 | 均线多头、板块强度、资金 |
| 长线 | 全市场预筛（现有 _PRESCREEN）后取 PE / PB 分位低且营收与利润同比双正 | 财报（新浪三表，只对前 30 只取）、估值 |

每个周期 30 只候选喂一次提示词选 5 只。自选股单独一次提示词，带候选价位与记忆块。
一次全量运行的 LLM 调用：3 次周期选股 + 1 次自选股 + 账本核对包含在这 4 次里，共 4 次；
09:05 只跑短线周期 + 自选股，共 2 次。

### 5. 提示词与返回（llm_picks.py，复用 llm._chat）

两个函数：`horizon_picks(horizon, candidates, market_ctx, memory)` 与
`watchlist_points(rows, candidates_by_code, memory_by_code, market_ctx)`。返回按条校验，不整批作废：

```
{ "calls": [ { "code", "name", "horizon", "stance",
               "entry": [lo, hi], "exit": [lo, hi], "stop",
               "decision": "new|keep|revise|withdraw",
               "trigger": "none|stop_hit|target_hit|expired|thesis_broken",
               "thesis", "trigger_note", "basis": {"signals": [], "rules": []} } ] }
```

系统提示沿用现有价格行为框架规则与免责声明。

### 6. 运行与归属

- 公共三周期：全站每天一次，`scheduler.py` 16:00 触发，在站长上下文（`as_fleet()`）里跑，LLM 计费记 fleet 桶，结果进 `data/picks_public.db`；各账号自选股买卖点各跑一次、计费记本人额度。
- 个人自选股：按账号各跑一次，`with userctx.as_user(uid)`，记该用户名下；服务器上遍历 `auth.list_uids()`，
  本地 `python3 app.py` 只跑当前站长。09:05 自选股同样各跑一次完整提示词。
- 手动：`POST /api/picks/run`（个人）与 `POST /api/picks/run_public`（管理员），走现有 ai 门与日额度。
- 结果贴回：16:00 运行前先做一次结算（拉日 K，更新 open 行的结果字段与到期）。
- 模型失败：账本不动，上一版有效，页面标"今日未更新"。预算门拒绝整轮跳过并记日志。
- 跨进程：运行前拿 `data/.picks-running-<scope>` 文件锁，与复盘同一写法。

### 7. 接口（picks_routes.py，Flask Blueprint）

```
GET  /api/picks/public            三周期各 5 只（最新 run）
GET  /api/picks/watchlist         自选股每只的当前有效观点 + 结果
GET  /api/picks/chain/<code>      某只股票的观点链（含结果）
POST /api/picks/run               手动跑个人自选股（ai 门）
POST /api/picks/run_public        手动跑公共三周期（管理员）
GET  /api/picks/status            上次运行时间、是否在跑、失败原因
```

`app.py` 只加一行 `app.register_blueprint(picks_routes.bp)`；路由在 auth 闸门之后自动受保护。

### 8. 界面（static/app.js + templates/index.html）

- 选股页新增「三周期推荐」区：三列各 5 张卡（立场、买点、卖点、止损、有效到、一句理由、角标
  "上次维持 / 本次修改：目标达成"）。
- 自选股表加三列：买点、卖点、时间窗；点击展开观点链与实际走势。
- 先按现有样式做；浅色与手机适配归子项目 D。

### 9. 测试（离线，零网络）

- `tests/test_picks_levels.py`：候选价位、摆动点、簇合并、校验替换。
- `tests/test_picks_store.py`：修改链、到期、改口规则强制、结果贴回、记忆块（近窗上限、远期汇总、
  相关性挑选、token 量级）。
- `tests/test_picks_pipeline.py`：候选池压缩、返回按条校验、失败时账本不动（monkeypatch llm._chat）。

### 10. 文件

| 文件 | 行数目标 |
|---|---|
| picks_store.py | 300 |
| picks_levels.py | 200 |
| picks_pipeline.py | 见 `python3 tools/status.py`（仍在 400 行目标内） |
| llm_picks.py | 200 |
| picks_routes.py | 150 |
| tests/test_picks_*.py | 各 150 |

`app.py` 加 1 行，`scheduler.py` 加两个定时点，`app.js` 加渲染约 200 行。

## 明确不做（本子项目）

- 手机提醒（子项目 C）；复盘里的个股分析（A）；浅色与手机适配（D）。
- 用 LLM 写远期摘要（先用确定性汇总，需要再加）。
- 回测这套买卖点的历史胜率（PITFALLS 第 3 条：LLM 回测不可信）。结果贴回只记既成事实。
