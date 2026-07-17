# 盘中 agent 调度器（长期挂机也能每桶自动跑）

> 2026-07-17。解决「app 在非交易时段启动、此后当日决策永不自动触发」。

## 问题

日循环唯一的自动触发点是 `_agent_boot()`，只在 **app 启动时**跑一次（`_universe_boot` 末尾）。
用户长期挂机、常在盘前（如 00:13）启动 app：那一次启动带 `require_open=True` 命中非交易
时段 → 只补条件单、跳过决策；此后**没有任何再触发**，于是当天早盘/尾盘两个桶全空。

实测 2026-07-17：app 00:13 启动，到 09:58 仍 `runs=179`（全是昨天的）、当日 0 claims、
0 挂单。`current_slot()` 已是「早盘」、门全通，但没有东西去调用它。

## 方案（A：后台定时线程 + 单飞锁）

新增守护线程 `_agent_scheduler()`，在 `_universe_boot` 末尾**替代**原来那一次 `_agent_boot()`：

1. **先立刻跑一次** `_agent_tick()`——完全保留「启动即尝试」的旧行为。
2. 之后每 `_AGENT_TICK_SEC=300`（5 分钟）再 `_agent_tick()` 一次。

`_agent_tick()` = 非阻塞抢单飞锁 → `_agent_boot()`（即 `run_all(require_open=True)`）→ 释放。

### 为什么安全（全部复用现有门）

- **每桶只真跑一次**：`run_day` 用 `claim_slot(agent,date,slot)` 原子占位；某 agent 跑过本桶后，
  后续 tick 的 `run_day` 在 `claim_slot` 处即刻返回，**不烧 LLM**。
- **非交易时段零决策**：`require_open=True` 时非交易时段只 `sweep_conditions`（补历史触发的
  条件单，是既成事实，无未来函数），不跑决策。
- **不违反「错过不补」**：只认 `current_slot()` 的**当前活桶**，从不重建过去的桶。
- **单飞锁**：20 个 agent 一轮 workers=3 可能 7~20 分钟 > 5 分钟 tick。锁保证一轮没结束就
  跳过下次 tick，不叠出第二个并发 `run_all`（否则行情/LLM 并发翻倍、易被限流）。

### 成本

与「设计意图」完全一致：每桶恰好一次真决策 = 20 agent × 2 桶/天。多出来的 tick 近乎零成本
（占位已满 → 秒返回；或非交易时段只扫条件单）。**5 分钟间隔不增加 LLM 花费。**

### 效果

无论 app 何时启动：开盘后（或开 app 后）≤5 分钟内，当前活桶的 agent 决策就会跑。

## 改动面

- `app.py`：新增 `_AGENT_TICK_SEC` / `_agent_tick_lock` / `_agent_tick()` / `_agent_scheduler()`；
  `_universe_boot` 末尾 `_agent_boot()` → 起 `_agent_scheduler` 守护线程。约 25 行，纯增量。
- 冒烟：AST 语法 + `import app` 不炸 + 现有 74 例离线测试仍全过。
- 落地验证：交易时段重启 app，观察当前桶 5 分钟内产生当日 runs/claims。

## 未做（YAGNI）

- 不给挂单加**独立的**高频扫单线程：现有 per-run 扫单（`sweep_orders` 在 `run_day` 内）已满足
  「当日分时 / 隔夜日K」判定；用户诉求是「让日循环在盘中触发」，不是加快扫单频率。
- 不挂 launchd/cron（用户已决定不用 OS 调度）。
