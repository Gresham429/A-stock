# Backlog / 待办

> 已确认方向的功能。✅=已上线，⏳=待做。

## ✅ 全球宏观/地缘 digest 层（方案 B）— 已上线 2026-07-15

`llm.macro_digest`(v4-flash) 据 东财7×24全球 + 财联社 提炼 `{points, sector_map, bias}`；
`app._macro_block()` 前置注入 5 个 AI；`ai_cache` kind=`macro` 当日 6h 缓存。
传导由 AI 推理（不硬编映射表）。实测产出「中东紧张→利好石油开采/军工，利空航空/化工下游」。

## ✅ 本金可配 + 按总资产分级玩法 template — 已上线 2026-07-14

多档本地画像（现金+风险偏好）、每档独立持仓、总资产落 5 档、玩法 template 注入 5 个 AI。
见 `plan/2026-07-14-capital-profiles-templates-design.md`。

## ⏳ P2 · 大宗商品/外围市场 数据指标（方案 C）— TODO

在 B 的基础上叠加**客观数值指标**（而不只是新闻文本）：
- **油价 Brent/WTI · 美元指数 · 美股期货/三大指数 · 黄金 · VIX**。
- 需新 HTTP 数据源（避开 mootdx，海外可用）；接进 `macro_digest` 的输入 + 可能在大盘条展示。
- 价值：把「中东紧张→油价」从**新闻推断**升级为**看到油价实际涨了多少**，传导更硬。
- 依赖：B 已跑通，可随时开工。
