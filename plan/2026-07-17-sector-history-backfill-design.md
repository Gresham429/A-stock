# 板块走势历史回填（补齐一个季度）

> 2026-07-17。用户「看不到板块走势」——`sector_daily` 只有 3 天，趋势线是平的。

## 结论

板块走势 UI（`sparkline()` 画 `avg_chg` 折线）早就有，缺的是**数据深度**。回填过去 ~一个季度的
`sector_daily`，现有 UI 立刻出趋势。方案 A（用户已批准）：逐股拉日 K、按当前板块归属逐日聚合，
**口径与 live `snapshot_daily` 完全一致**。

## 未来函数安全性（PITFALLS#3）

某历史日板块 `avg_chg` = 当日成分股 `收盘/昨收-1` 的平均，只用**既成事实的日 K 收盘**——
与「用日 K low 补条件单」同性质，不重建任何决策环境，无泄漏。属允许的「既成事实」回填。

## 实现

### universe_store.py
1. **抽 `_agg_sector_payload(date, snap, memb)`**：把 `snapshot_daily` 里的按板块聚合循环抽成
   纯函数（live 与回填共用 → 口径不漂移、可离线单测）。`snap: code->{chg_pct,amount,price,name}`。
2. **`backfill_sector_daily(days=95, workers=10)`**：
   - 读 `stock_sectors`（归属）+ `stocks`（名字）+ 已有日期集合。
   - 并发逐股 `ds.sina_kline(code, num=days+5, scale=240)`（新浪 getKLineData，不封 IP）→
     每根 K 算 `chg_pct=(close/prev_close-1)*100`、`amount≈close*volume`。
   - 按日归组 → 逐日调 `_agg_sector_payload` → 写入。
   - **只填缺失日期**（`have` 集合 + `ON CONFLICT DO NOTHING`）→ 不覆盖 live 行、可重复运行。
   - `_backfill_lock` 单飞；每 500 只打进度；末尾 `purge()`。

### app.py
- `POST /api/sectors/backfill`：后台线程起 `backfill_sector_daily`，即时返回。

### static/app.js
- 板块面板 `secStatus` 加「回填历史」按钮 → POST → 提示后台进行、完成后刷新。
- **UI 优化**：`sparkline()` 加高 + 标注首/末/最高/最低 `avg_chg` 值 + 起止日期，`n<3` 标数据不足。

### 测试
- 新增 `tests/test_sector_backfill.py`：构造已知 `snap`+`memb` → 验 `_agg_sector_payload` 的
  `avg_chg`/涨跌家数/涨停家数（主板 9.7 / 创业板科创 19.7 阈值）/领涨股。零网络，纳入套件。

## 已知近似（会在面板注明）

- **归属错配**：用**当前**板块归属套历史日 → 期间改行业/新上市/退市的个股有轻微错配。非泄漏，
  只是历史归类近似。
- **前复权口径**：`sina_kline` 前复权，除权日 `收盘/昨收` 与原始涨跌幅略偏（多数日无影响）。
- **amount 估算**：日 K 无成交额，用 `close×volume` 估（单位在冒烟测试中用真实数据校准）。

## 不做（YAGNI）

- 不改 live 口径去用板块指数（会与现有 3 天不接、且动到 agent 依赖的板块强弱）。
- sparkline 不做完整 hover 十字线（放大+数值标注已够看趋势）。
