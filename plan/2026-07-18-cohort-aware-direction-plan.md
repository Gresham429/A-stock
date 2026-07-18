# cohort-aware 因子方向 实施方案（2026-07-18）

> 承接 `plan/2026-07-18-prescreen-coverage-analysis.md` 的「建议(1)：让 `_pa_score` 方向跟池子走」。
> 目标：修「方向在小盘主导的全池上学、用到大盘池」的符号错配。**只改打分与教训门用的方向，
> 不动 excess_dist / 判罪线 / 冻结分位 / 5 个用户面 AI 的默认全池方向。**

## 为什么做（数据坐实，非拍脑袋）

`_pa_score` 用 `directions(horizon=10)`。实测当前（2026-07-18 live）：

| 因子 | 全池 direction() | 大盘 cohort (dense 198×575) | 差异 |
|------|-----------------|----------------------------|------|
| vol | +1 (近60日 t6.96) | +1 (t7.89) | 同 |
| cum20 | +1 (近60日 t2.99) | +1 (t7.39) | 同 |
| **range_pos** | **−1 (全样本 t−5.48)** | **+1 (近60日 t4.11)** | **反！** |

即：全池给 range_pos 判反转（奖励近低点），但在**实际参与打分的大盘池**里 range_pos 是动量
（奖励近高点，t+4.11 显著）。当前 shortlist 对 range_pos **方向标反**。h=20 两者一致，故差异
恰在打分用的 h=10。→ 有牙、非 YAGNI。

## 顺带必须处理的一致性约束（PITFALLS「教训不得与因子数据矛盾」）

`detect_failures` 的 `bad("range_pos")` 用全池方向（h=10 → −1 → range_pos>85 记「追高」）。
若只改打分（大盘 range_pos +1 → 奖励近高点）而教训门不改，就成了「打分鼓励、教训惩罚同一行为」
——CLAUDE.md 最要命三类之#2 明确禁止。故**打分与教训门必须一起 cohort 化**。

## 设计（最小爆炸半径）

**不碰**：`backtest()` 的抽样与 excess_dist 产出（判罪线/冻结分位敏感）；`ic_daily`（全池 IC，
默认 direction 与 5 AI 依赖）；`direction()/directions()` 的**默认**（cohort='all'）行为。

**新增**：
1. 表 `ic_cohort(date,factor,horizon,cohort,ic,n, PK(date,factor,horizon,cohort))`——大盘 cohort 的逐日 IC。
   与 `ic_daily` 并存、互不干扰。`init()` 建表（IF NOT EXISTS，旧库零迁移）。
2. `LARGE_COHORT_N = 600`（镜像 `screening._PRESCREEN`，注释互链）。
3. `backtest_large(n=200)`：从「按流通市值前 600」密集抽 ~200 只 → 拉日K → 逐日横截面 IC（所有 horizon）
   → 存 `ic_cohort` cohort='large'。**不产 excess_dist**（判罪线永远全池）。独立一次网络（~200 拉K，
   与 `backtest()` 隔离；boot 时 stale 才跑，+~20s 可接受）。IC 循环与 `backtest()` 同口径——刻意不重构
   `backtest()`（避免碰 excess_dist），复用纯 helper `_daily_ic_rows(per_day, cohort, now)`。
4. `direction(factor, horizon=10, cohort='all')`：cohort='all' 读 `ic_daily`（不变）；否则读 `ic_cohort`。
5. `directions(horizon=10, cohort='all')`：透传 cohort。
6. `scoring_directions(cohort='large', horizon=10)`：cohort **无 IC 数据**（未跑 backtest_large、冷启动）
   → **回退全池**（避免打分全体中性）；有数据但不显著 → 尊重 sign 0（数据说中性、不回退）。

**接线**：
7. `_pa_score(m, dirs=None)`：dirs 为空 → `factor_lab.directions()`（向后兼容）；否则用传入 dirs。
8. `screening._screen_rows` 无 focus（预筛后大盘池）：`dirs = factor_lab.scoring_directions('large')` 传入
   `_pa_score`。**focus 路径不变**（板块成分股混市值，用全池方向合理，且限爆炸半径）。
9. `agent_loop.detect_failures`：按被买股的 cohort 选方向——`large_set=set(codes_of()[:LARGE_COHORT_N])`，
   大盘买入用 `scoring_directions('large')`、否则全池。`bad(factor, code)` 据此选。→ 打分与教训同源、不矛盾。
10. `refresh_if_stale`/boot：full backtest 重跑时**顺带** `backtest_large()`。`purge()` 也清 `ic_cohort`。

## 任务分解（TDD）

### Task 1 — factor_lab：ic_cohort 表 + cohort 参数 + scoring_directions
- 改 `_SCHEMA` 加 `ic_cohort` 表；`LARGE_COHORT_N=600`。
- `direction(factor,horizon=10,cohort='all')` / `directions(horizon=10,cohort='all')` 加 cohort（读对应表）。
- 纯 helper `_daily_ic_rows(per_day, now)` → [(date,factor,horizon,ic,n)]（从 backtest 抽出、backtest 改用它、backtest_large 也用它）。
  ⚠️ 抽取时**只动 IC 行那段**（lines 273-282），excess_dist 段原样不动。
- `scoring_directions(cohort='large',horizon=10)`：无数据回退全池。
- 测试 `test_factor_cohort.py`：注入 ic_cohort/ic_daily 行 → direction(cohort='large') 读对表；
  cohort 无数据 → scoring_directions 回退全池；cohort 有数据不显著 → 不回退（sign 0）；_daily_ic_rows 纯函数正确。

### Task 2 — factor_lab：backtest_large 落 ic_cohort
- `backtest_large(n=200,workers=8)`：sample 大盘 cohort → _series_of → per_day → `_daily_ic_rows` → 存 cohort='large'。
- `refresh_if_stale` 末尾调 `backtest_large`；`purge()` 加 `DELETE FROM ic_cohort WHERE date<?`。
- 测试：`_daily_ic_rows` 已在 Task1 测；backtest_large 网络部分走 Task4 live 集成验证（不进离线套件）。

### Task 3 — screening + agent_loop 接线
- `_pa_score(m, dirs=None)`；`_screen_rows` 无 focus 传 `scoring_directions('large')`。
- `detect_failures` 按被买股 cohort 选方向。
- 测试 `test_screen_branches`（已有，改 _pa_score 必跑）+ 新增 `test_pa_score_dirs`：
  同一 m、range_pos 高，传全池(range_pos −1)得低分、传大盘(range_pos +1)得高分 → 证明方向真的跟 dirs 走。
  `test_agent_gates` 里补：detect_failures 大盘买入高 range_pos + 大盘方向为正 → **不记**追高（一致性）。

### Task 4 — live 集成验证 + 回归
- 全 12+1 离线套件绿。
- live：`backtest_large()` 落数 → `scoring_directions('large')` 得 range_pos +1 → 用真候选跑 `_screen_rows`
  对比「改前(全池)vs 改后(大盘)」top-N 是否真的变（range_pos 高的排名上移）。
- 重启 app 验 boot（含 backtest_large 不炸、boot 时间可接受）。
- 实跑一个 debate 档 dry_run（决策数据面/打分变了 → CLAUDE.md 铁律）。

## 判定合不合适（做完据实回答）

做完据以下判：(a) 离线全绿 + 一致性测试过；(b) live 确认 range_pos 排名方向翻正、shortlist 实变；
(c) boot 不显著变慢、debate 不炸。**任一不满足或收益不抵风险 → 不 ship，回退并如实报告**。

## 明确不做（YAGNI / 限爆炸半径）
- excess_dist / 判罪线 / 冻结分位：永远全池，不 cohort 化。
- focus（选板块）路径打分：保持全池方向。
- 小盘单独 cohort：只做 large（预筛保留的那群才是打分对象）。
- 权重仍等权（只让方向随 cohort，不重拟合权重）。
