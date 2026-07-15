# Backlog / 待办

> 已确认方向的功能。✅=已上线，⏳=待做。**当前 backlog 已清空。**

## ✅ 全球宏观/地缘 digest 层（方案 B）— 已上线 2026-07-15

`llm.macro_digest`(v4-flash) 据 东财7×24全球 + 财联社 提炼 `{points, sector_map, bias}`；
`app._macro_block()` 前置注入 5 个 AI；`ai_cache` kind=`macro` 当日 6h 缓存。
传导由 AI 推理（不硬编映射表）。

## ✅ 外围市场数值指标（方案 C）— 已上线 2026-07-15

`ds.global_markets()`（新浪外盘 HTTP，海外可用）：**WTI/布伦特原油 · 黄金 · 铜 · 道指/纳指/标普500**，
喂进 `macro_digest`，提示词要求**数值优先于新闻措辞**。
实测：「WTI涨1.13%/布伦特涨1.45%→利好石油开采/油服，利空航空/化工下游」「黄金跌0.73%→压制黄金板块」，并能识别内外背离。
**已知缺口**：该源无**美元指数**(`hf_DX`)与 **VIX**（实测返回空）——想要得另找源（东财/腾讯外盘），价值中等，暂不做。

## ✅ 本金可配 + 按总资产分级玩法 template — 已上线 2026-07-14

多档本地画像（现金+风险偏好）、每档独立持仓、总资产落 5 档、玩法 template 注入 5 个 AI。
见 `plan/2026-07-14-capital-profiles-templates-design.md`。

## 可选后续（未承诺）

- **美元指数 / VIX**：换源补齐外围数值（见上「已知缺口」）。
- **溯源(provenance) 推广**：现仅 entry/position；daily/screen/market 也可加。
- **template 文案 UI 编辑**：现 5 档玩法是代码常量，只读展示；可做成可编辑。
