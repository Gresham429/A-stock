# plan/ — 设计文档与踩坑索引

> 项目的设计决策、待办、踩坑都在这。改代码前先看 **PITFALLS.md**。
> 权威的项目上手文档在仓库根 `CLAUDE.md`；这里是各特性的**详细设计**存档。

## 🔴 先读（活文档，持续更新）

| 文档 | 内容 |
|------|------|
| [PITFALLS.md](PITFALLS.md) | **改代码前必读**：真踩过并付出代价的坑，按「会不会让你写出错的东西」排序 |
| [BACKLOG.md](BACKLOG.md) | 待办：已确认方向的功能，含卡在数据源的两项 |

## 设计文档（按主题，一次性存档）

### Agent 与进化
| 文档 | 一句话 |
|------|--------|
| ⭐ [2026-07-18-agent-logic-map](2026-07-18-agent-logic-map.md) | **权威·先读**：agent 交易/学习闭环端到端运行时全景（pipeline/门/调度/决策/订单/结算判罪/记忆/量化接入点/红线） |
| [2026-07-16-agent-evolution-design](2026-07-16-agent-evolution-design.md) | Multi-Agent 模拟交易 + 失败归因驱动的提示词进化（总设计） |
| [2026-07-16-outcome-driven-lessons-design](2026-07-16-outcome-driven-lessons-design.md) | 结果导向教训：从「买入时长得像错」到「事后证明错」（超额结算 + 判罪线来自分布） |
| [2026-07-16-decision-data-plane-design](2026-07-16-decision-data-plane-design.md) | 决策数据面：给候选股补 K 线结构 + 真·大盘块，让提示词能满足自己注入的规则 |
| [2026-07-18-agent-memory-redesign](2026-07-18-agent-memory-redesign.md) | **agent 记忆重构 P1–P3**：共享底座 journal(冻结分位/情节自视图/个股 house-view/舰队只读层) |
| [2026-07-17-intraday-agent-scheduler-design](2026-07-17-intraday-agent-scheduler-design.md) | 盘中调度器：守护线程每 5 分钟探一次，长期挂机也每桶自动跑 |

### 数据与选股
| 文档 | 一句话 |
|------|--------|
| [2026-07-15-full-market-universe-design](2026-07-15-full-market-universe-design.md) | 全市场股票池 + 板块日变化统计 |
| [2026-07-17-sector-history-backfill-design](2026-07-17-sector-history-backfill-design.md) | 板块走势历史回填：逐股日 K 补 ~250 交易日 + 面板分栏/走势窗口 |
| [2026-07-13-all-sector-screening-and-market-overview](2026-07-13-all-sector-screening-and-market-overview.md) | 全板块两级选股 + 大盘局势研判 |
| [2026-07-18-prescreen-coverage-analysis](2026-07-18-prescreen-coverage-analysis.md) | **_PRESCREEN 选股偏差覆盖分析**：mcap 预筛丢弃 88%，反转因子只在被丢的小盘有效、方向在保留的大盘翻转（只出结论、改动归 🟡） |
| [2026-07-18-cohort-aware-direction-plan](2026-07-18-cohort-aware-direction-plan.md) | **cohort-aware 因子方向**（上文的修复实施）：无 focus 全市场选股改用大盘 cohort 方向打分（ic_cohort/backtest_large/scoring_directions）；agent 不受影响 |

### AI 知识、缓存与溯源
| 文档 | 一句话 |
|------|--------|
| [2026-07-13-knowledge-cache-architecture](2026-07-13-knowledge-cache-architecture.md) | 知识与缓存架构总纲（L1–L5） |
| [2026-07-13-L1-ai-output-cache](2026-07-13-L1-ai-output-cache.md) | L1：AI 输出短期缓存 + 时间戳 |
| [2026-07-14-ai-provenance-attribution-design](2026-07-14-ai-provenance-attribution-design.md) | AI 建议的数据溯源 + 可验证依据 |

### 资金、画像与叙事
| 文档 | 一句话 |
|------|--------|
| [2026-07-14-capital-profiles-templates-design](2026-07-14-capital-profiles-templates-design.md) | 多档本地投资画像 + 总资产分级玩法 template |
| [2026-07-14-company-narrative-design](2026-07-14-company-narrative-design.md) | 公司叙事：做过 / 在做 / 要做 |

### 前端
| 文档 | 一句话 |
|------|--------|
| [2026-07-14-market-chart-merge-design](2026-07-14-market-chart-merge-design.md) | 波动 + K线 合并为「行情」图（多周期蜡烛 + 分时自动刷新） |

---

> 命名：`YYYY-MM-DD-<主题>-design.md`。设计文档写完即存档、一般不再改；
> 演进中的决策记进 PITFALLS.md / BACKLOG.md 或对应模块的 docstring。
