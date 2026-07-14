# 设计：波动 + K线 合并为「行情」图（多周期蜡烛 + 分时自动刷新）

> 日期：2026-07-14 ｜ 状态：待用户 review ｜ 类型：功能改动（前端为主 + 后端 1 处透传 + 1 个轻量新端点）

## 背景 / 问题

深挖抽屉现在有两个独立标签：
- **「波动」**（`pane_wave`）：多周期折线（当日/5日/30日/60日/90天/近1年），只画收盘价连线。
- **「K线/箱形」**（`pane_kl`）：完整蜡烛图（红涨绿跌 + MA5/MA20 + 成交量）+ 箱形图，但**固定看最近 60 天**、单独走 `/api/kline`。

两个已知问题：
1. **60日 与 90天 几乎一样**（`static/app.js:281-283`）：60日=最近 60 个**交易日**（`slice(-60)`），90天=最近 90 个**自然日**（`date>=today-90`≈62 个交易日）。单位混用导致两档装的是几乎同一批 K 线。
2. **功能割裂**：想看蜡烛就没有周期切换，想切周期就只有折线。用户要的是交易软件那种「一个带周期按钮的行情图」。

## 目标

把两个标签**合并成一个「行情」标签**，做成交易软件式的多周期行情图：
- 周期：`分时 · 5日 · 近1月 · 近3月 · 近半年 · 近1年`。
- 分时 / 5日 → 折线+面积（复用现有 `waveChart`，保留十字准星 hover）。
- 近1月 / 近3月 / 近半年 / 近1年 → **蜡烛图 + MA5(橙)/MA20(蓝) + 成交量柱**（复用 `candlestick`），下方保留箱形图 + 统计行。
- 修掉 60/90 重叠：四个日K档**统一按自然日窗口**。
- 蜡烛图**新增鼠标悬停**：显示当日 开/高/低/收 + 涨跌%。
- **分时自动刷新**：抽屉开着 + 当前是分时 + 处于北京时间交易时段时，每 30s 自动重拉分时并重渲染。

## 非目标（YAGNI）

- 不做周K/月K、不做复权切换、不做画线工具、不做 MACD/KDJ 等副图指标。
- 分时不做逐笔 tick（数据源是腾讯分时，准实时即可）。
- 不给日K档做自动刷新（日线收盘才变，盘中刷没意义）。
- 不动其它标签（概览/研报/龙虎榜/解禁/资金）。

## 设计

### ① 周期定义（修 60/90 重叠）——`waveSeries(period)`

| 周期 key | 标签 | 图形 | 数据来源 | 取数 |
|----------|------|------|----------|------|
| `day`  | 分时   | 折线 | `WAVE.intraday`（腾讯分时） | 全部，base=昨收 |
| `5d`   | 5日    | 折线 | `WAVE.min5`（新浪5分钟） | 全部 |
| `1m`   | 近1月  | 蜡烛 | `WAVE.daily` | `date >= today-30`  ≈20 根 |
| `3m`   | 近3月  | 蜡烛 | `WAVE.daily` | `date >= today-90`  ≈62 根 |
| `6m`   | 近半年 | 蜡烛 | `WAVE.daily` | `date >= today-180` ≈122 根 |
| `1y`   | 近1年  | 蜡烛 | `WAVE.daily` | `date >= today-365` ≈244 根 |

- 四个日K档**全部按自然日窗口过滤**，不再用 `slice(-N)` 交易日根数 → 各档 K 线数明显不同，60/90 重叠消失。
- `WAVE_PERIODS` 常量与默认 `WAVE_PERIOD='day'` 相应更新。

### ② 均线全序列预计算——避免窗口内 MA 缺头

- 在**完整 `WAVE.daily` 序列**上算好每根的 MA5 / MA20，挂到 bar 上（`{...bar, ma5, ma20}`），**再按周期窗口截取**。
- 这样看「近1月」时 MA20 也是完整的（否则窗口前 19 根没有 MA20）。
- `candlestick()` 改为**读取 bar 上预计算的 ma5/ma20**，不再自己按窗口内 index 算。

### ③ 渲染分派——`renderWavePeriod()`

```
series = waveSeries(WAVE_PERIOD)
if series.kind === 'intra':   body = waveChart(series)            // 折线+面积，带十字准星
else (kind === 'daily'):      body = candlestick(bars)            // 蜡烛+MA5/MA20+量+hover
                              + boxplot(bars.map(close))          // 箱形图保留在下方
                              + waveStats(...)                    // 振幅/年化波动 统计行
```

### ④ 蜡烛图鼠标悬停——`candlestick()` 内新增

- 复用 `#tip` 浮层（与折线 hover 同一套）。
- 鼠标横向定位到最近一根 K 线，显示：`日期　开O 高H 低L 收C　涨跌%（相对前一日收盘）`。
- 加十字准星竖线（复用折线的 `wave_cross` 样式）。

### ⑤ 分时自动刷新——新增 `waveTimer` + 后端 `/api/minute`

**后端**（`app.py`）：新增轻量端点，只取分时，避免每 30s 重拉整个 `/api/wave`（那还含 min5 + 260 根日K，太重）：
```
GET /api/minute/<code>  ->  {"intraday": ds.tencent_minute(code), "prev_close": <昨收>}
```

**前端**（`static/app.js`）：
- 模块级 `let waveTimer=null;`
- `openDetail()`：进入时（在 `++detailSeq` 之后）`clearInterval(waveTimer)`，然后启动 `waveTimer=setInterval(tickMinute, 30000)`。
- `closeDrawer()`：`clearInterval(waveTimer); waveTimer=null;`
- `tickMinute()` 逻辑：
  1. 若 `WAVE_PERIOD!=='day'` 或抽屉未打开 → return（不刷）。
  2. 若非北京时间交易时段 → return（见下）。
  3. `const gen=detailSeq;` fetch `/api/minute/<当前code>`；回来后 `if(gen!==detailSeq) return;`（**请求令牌**，防切股票错位，遵守项目「异步渲染必须带请求令牌」约定）。
  4. 更新 `WAVE.intraday` / `WAVE.prev_close`，若当前仍是 `day` 则 `renderWavePeriod()`。
- **北京时间交易时段判定** `_cnTradingNow()`：用 `Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',...})` 取北京时/分/星期，判周一~周五 且 时间落在 `09:25–11:35` 或 `12:55–15:05`。
  - **为什么用北京时区而非本地时区**：用户机器可能在美西时区（UCSB），若按本地钟判断交易时段会永远错过 A 股盘中。法定节假日不在前端判（漏判无害：`tencent_minute` 非交易时段返回上一交易日的静态数据，重复拉一次不出错、只多一个请求）。
- **可见反馈**：分时视图底部 caption 加「刷新于 HH:MM:SS · 交易时段每30s」；非交易时段显示「快照（非交易时段）」。

### ⑥ UI / 标签合并——`templates/index.html`

- 删掉 `<button data-p="kl">K线/箱形</button>` 标签和 `<div id="pane_kl">` 面板。
- `data-p="wave"` 标签文字由「波动」改为「**行情**」（cosmetic，可再定）。
- `openDetail()` 删掉对 `/api/kline` 的独立请求（`app.js:163-164`）与 `renderKline()` 包装函数；`candlestick()`/`boxplot()` 保留复用。后端 `/api/kline` 路由**留着不删**（无害，可能他用）。

## 涉及文件与函数

| 文件 | 改动 |
|------|------|
| `app.py` | `/api/wave` 的 daily 从 `{date,close}` 改为 `{date,open,high,low,close,volume}`（`sina_kline(260)` 本就返回这些，只是被丢了）；新增 `GET /api/minute/<code>`。 |
| `static/app.js` | `WAVE_PERIODS` 周期表；`waveSeries()` 周期窗口 + 返回 OHLC bar；MA 全序列预计算；`renderWavePeriod()` 折线/蜡烛分派 + 箱形；`candlestick()` 读预计算 MA + 加 hover；新增 `waveTimer`/`tickMinute`/`_cnTradingNow`；`openDetail`/`closeDrawer` 起停定时器 + 删 `/api/kline` 请求；删 `renderKline` 包装。 |
| `templates/index.html` | 合并标签（删 kl tab 与 pane_kl，wave→行情）。 |

零新依赖，纯内联 SVG，遵守看板约定（红涨绿跌 / 无外部资源 / 异步带请求令牌）。

## 测试计划

- 语法：`python3 -c "import ast; ast.parse(open('app.py').read())"`；`node --check static/app.js`。
- 起服务（先释放 5000：关 AirPlay Receiver）→ 开任一自选股深挖 →「行情」标签：
  1. 切 6 个周期，确认 **近1月/近3月/近半年/近1年 蜡烛根数各不同**（≈20/62/122/244）；
  2. MA5/MA20 在近1月也**画满**、成交量红绿正确；
  3. 蜡烛 hover 出 **OHLC + 涨跌%**；
  4. 分时/5日 仍是折线 + 十字准星；箱形图在蜡烛下方；
  5. 交易时段观察分时「刷新于」时间每 30s 跳动、曲线延长；非交易时段不刷、标注快照。
- `curl --noproxy '*' localhost:5000/api/wave/<code>`：daily 含 OHLC；`/api/minute/<code>`：返回 intraday + prev_close。

## 风险 / 边界

- **非交易时段/新股**分时可能为空 → 已有空态提示，保留。
- **窗口内 K 线过少**（新股「近1年」也许只有几十根）→ 蜡烛照画，MA 缺头正常跳过。
- **自动刷新时区**：靠 `Asia/Shanghai` 显式判定，跨时区机器也正确；节假日漏判无害（数据静态）。
- **请求令牌**：`tickMinute` 必须校验 `detailSeq`，否则快速切股票时旧分时会覆盖新股票视图。
