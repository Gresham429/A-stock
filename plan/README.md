# plan/ 索引

项目上手看仓库根 `CLAUDE.md`；这里放三种东西：两份活文档（踩坑、待办）、各特性的设计存档、一份分析记录。
每份设计文档第 3 行是「状态：」，`python3 tools/status.py` 会把它们列出来。

## 先读（活文档）

| 文档 | 内容 |
|------|------|
| [PITFALLS.md](PITFALLS.md) | 改代码前必读：真踩过并付出代价的坑，按危害分节，编号不变、只追加 |
| [BACKLOG.md](BACKLOG.md) | 唯一的待办：子项目、多用户收尾、需用户签字的改动、卡在外部条件的项 |

## 设计存档（按主题）

一个主题一份当前文档，原地重写，不叠历史；已完成的实施计划不保留（git 里有）。

### Agent 与进化

| 文档 | 一句话 |
|------|--------|
| [2026-07-18-agent-logic-map](2026-07-18-agent-logic-map.md) | 权威、先读：agent 交易与学习闭环的运行时全景（pipeline、门、调度、决策、订单、结算判罪、记忆、量化接入点、红线、多用户下的舰队归属） |
| [2026-07-16-agent-evolution-design](2026-07-16-agent-evolution-design.md) | 总设计：多 agent 模拟盘 + 失败归因驱动的提示词进化，用户原话与红线只在这里 |
| [2026-07-16-outcome-driven-lessons-design](2026-07-16-outcome-driven-lessons-design.md) | 结果导向教训：超额结算 + 判罪线来自分布 |
| [2026-07-16-decision-data-plane-design](2026-07-16-decision-data-plane-design.md) | 决策数据面：候选股补 K 线结构 + 真大盘块 |
| [2026-07-18-agent-memory-redesign](2026-07-18-agent-memory-redesign.md) | agent 记忆 P1 到 P3：一份 journal、多个视图（冻结分位、house-view、舰队只读层） |
| [2026-07-17-intraday-agent-scheduler-design](2026-07-17-intraday-agent-scheduler-design.md) | 盘中调度器：每 5 分钟一 tick，长期挂机也每桶自动跑 |

### 数据与选股

| 文档 | 一句话 |
|------|--------|
| [2026-07-15-full-market-universe-design](2026-07-15-full-market-universe-design.md) | 全市场股票池 + 板块归属 + 板块日变化 |
| [2026-07-17-sector-history-backfill-design](2026-07-17-sector-history-backfill-design.md) | 板块走势历史回填：逐股日 K 聚合补一个季度 |
| [2026-07-13-all-sector-screening-and-market-overview](2026-07-13-all-sector-screening-and-market-overview.md) | 两级选股 + 大盘研判条的最初设计；候选池部分已被全市场池取代 |
| [2026-07-18-prescreen-coverage-analysis](2026-07-18-prescreen-coverage-analysis.md) | 分析记录：mcap 预筛的覆盖偏差，以及 cohort-aware 方向落地后的对比结果 |
| [2026-09-16-picks-ledger-design](2026-09-16-picks-ledger-design.md) | 三周期选股 + 自选股买卖点 + 观点账本（改口只在触发事件时、两层记忆块） |
| [2026-09-17-review-stocks-design](2026-09-17-review-stocks-design.md) | 复盘往下走一层到个股：挑股透明不打分、数据全部复用现有面、不给买卖点 |
| [2026-09-17-screening-factor-refactor-design](2026-09-17-screening-factor-refactor-design.md) | 选股与因子重构：三周期各一套、候选池按市值分三层、两层验收、中长线攒够数据后自动上线。六步已落地，验收数字在文档第七节 |

### AI 知识、缓存与溯源

| 文档 | 一句话 |
|------|--------|
| [2026-07-13-knowledge-cache-architecture](2026-07-13-knowledge-cache-architecture.md) | 知识与缓存架构 L1 到 L5（含 L1 AI 输出缓存的当前口径） |
| [2026-07-14-ai-provenance-attribution-design](2026-07-14-ai-provenance-attribution-design.md) | AI 建议的数据溯源 + 可验证依据 |

### 资金、画像与叙事

| 文档 | 一句话 |
|------|--------|
| [2026-07-14-capital-profiles-templates-design](2026-07-14-capital-profiles-templates-design.md) | 多档投资画像 + 总资产分级玩法 |
| [2026-07-14-company-narrative-design](2026-07-14-company-narrative-design.md) | 公司叙事：做过 / 在做 / 要做 |

### 前端

| 文档 | 一句话 |
|------|--------|
| [2026-07-14-market-chart-merge-design](2026-07-14-market-chart-merge-design.md) | 波动 + K 线合并为多周期行情图 |

### 图

`diagrams/serving-vs-agent.svg`：服务面与 agent 舰队的分离，`framework.md` 引用。

## 约定

- 文件名 `YYYY-MM-DD-<主题>-design.md`（分析记录用 `-analysis.md`）。日期是主题首次成文的日期，
  之后原地改写不换名。
- 演进中的坑记进 PITFALLS.md，待办记进 BACKLOG.md，实现细节以代码与 docstring 为准。
- 文档里不写路由数、测试数、行数这类可从代码得到的数字，用 `python3 tools/status.py` 看。
- 不用装饰符号（emoji、对勾、箭头），用文字。
