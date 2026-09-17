# 决策数据面 —— 让提示词能满足它自己注入的规则

状态：已实现（2026-07-16）

> 起因：早盘 12 个 agent 跑完，**11 个产出 0 条意向**，且非风控否决。

## 问题（有证据）

`runs` 表 `2026-07-16` 决策阶段的 `skip_reason` 高度一致：

| agent | skip_reason（节选） |
|---|---|
| 12 | 数据不足：缺少K线、趋势、均线、信号棒等必要技术数据 |
| 20 | 数据不足：缺乏K线图形和板块指数走势…根据 R6 看不懂就等 |
| 21 | **仅凭静态形态分与20日涨跌数据**，无法判断市场状态、趋势方向与关键结构位 |
| 23 | 缺少K线数据、趋势状态无法判定，不符合任何入场规则，观望 |

**AI 是对的**，两个缺口都真实存在：

### 缺口①：候选股无任何 K 线/结构数据

`agent_loop._decide_prompt` 每只候选只给
「现价/形态分/波动/区间位置/20日涨/1手成本/板块」——全是**静态标量**。

而 `blocks` 注入的规则库是**价格行为体系**：84 条启用规则里 **37 条**需要
K线/均线/趋势/信号棒（市场状态识别8 + 形态结构7 + 趋势与通道6 + 突破与失败6
+ K线信号4 + 入场时机4 + 计数与结构4 + 区间震荡3）。提示词还写着
「严格遵循【交易分析框架规则】」。

于是 AI 只能援引 **R6「看不懂就等」**（原文：*信号不清晰、上下文矛盾时不动手，
等下一根 K 线*）——**它是在正确地服从规则**。

### 缺口②：【今日大盘】传的是一个板块名

`agent_loop.py` 原为
`(universe_store.sector_ranking(kind="sw1", limit=3) or [{}])[0].get("sector","")`，
所以 AI 看到的「大盘」字面上就是「传媒」两个字。

「研判」阶段只挑了最强板块 + 扫挂单，**从不调**
`llm.market_overview` / `ds.index_quotes` / `ds.market_breadth`——这些都写好了没接。

### 这与 PITFALLS#2 是同一类错误

注入的**规则**与注入的**数据**互相矛盾。上次是教训与因子矛盾，这次是规则与数据矛盾。
代价：每天 12 agent × 48s v4-pro 全部烧掉换回「数据不足」，且**教训库结构上永远是空的**
——直接卡死 CLAUDE.md 列的第一优先级「让 agent 跑起来攒数据」。

## 方案

**只补数据，不替 AI 下判断。** 给结构原料（均线/高低/K线），让 AI 自己读趋势
——避免 PITFALLS#1（拍脑袋定方向被数据打脸 5 次）。我方**不**计算「趋势=涨/跌」
这类带阈值的结论。

### 新增 `structure.py`（纯函数、零网络、可离线测）

- `digest(bars) -> dict|None`：MA5/20/60、近20日最高/最低、最近3根 OHLC
- `fmt_stock(d) -> str` / `fmt_market(...) -> str`：紧凑成行

单独建模块而非塞进 `datasources.py`/`app.py`：
两者都已远超 400 行目标，且纯函数好测（契合本项目零依赖离线测试风格）。

### 数据源（均已实测，见下「实测结论」）

| 用途 | 源 | 耗时 |
|---|---|---|
| 个股日K | `ds.sina_kline(code, num=70)` | 0.2s/只 |
| 指数日K | 新增 `ds.index_kline(sym)`（**前缀须硬编码**） | 0.2s |
| 指数点位/涨跌/成交额 | `ds.index_quotes()` | 0.3s |
| 涨停/跌停家数 | `ds.market_breadth()` 的 push2ex 部分 | 2.8s |
| 板块强弱 | `universe_store.sector_ranking()` | 已在用 |

**缓存**：mirror 既有 `app._metric_cache` / `_METRIC_TTL=900` 模式——12 个 agent
候选高度重叠（focus 相同），不缓存会打 240 次请求。

### token 预算

每候选约 +60 字符 × 20 只 ≈ **+1200 字**，提示词约 8000 到 9200 字符。
v4-pro 决策档 `max_tokens=8000` 覆盖思考+正文（PITFALLS#13）；每次扩容数据面后要重测辩论档
`finish_reason`，若逼近上限则加预算而非砍数据。

## 实测结论

- `sina_kline('002354', num=60)` 通过：60 bars / 0.2s
- `index_quotes()` 通过：5 指数齐全 + 两市成交额 14803.7 亿 / 0.3s
- 注意：**`market_breadth()` 一半是坏的**（文档未记）：行业 `clist` 请求
  `Remote end closed connection`，`advancers`/`decliners`=None、
  `top_industries`=[]；仅 push2ex 的涨停48/跌停5 可用。
  **PITFALLS#14「东财按端点封 IP」的影响范围比文档写的大**：不止个股 `clist`，
  行业板块 `clist`(`fs=m:90+t:2`) 同样被封。涨跌家数改由 `sector_ranking` 侧面反映。
- 注意：`ds.market_prefix('000001')` 判成 `'sz'`（指数误判，故 `index_kline` 前缀硬编码）

## 步骤

1. `structure.py` + 单测（先测后写，纯函数）
2. `ds.index_kline()`
3. `app._safe_kline()`（TTL 缓存 + 异常兜底，mirror `_safe_metrics`）
4. `agent_loop`：研判阶段产出真·大盘块；`_decide_prompt` 候选行加结构
5. 冒烟：离线回归 + `dry_run` 实跑，验 `skip_reason` 是否消失、`finish_reason` 是否够用

## 验收标准

**不是**「代码跑通」，而是：`dry_run` 后决策 `skip_reason` **不再出现「数据不足/缺少K线」**。
若 AI 仍拒绝出手但理由变成实质判断（如「趋势向下不做多」），那是**正确行为**，算通过。
