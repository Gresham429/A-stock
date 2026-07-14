# 设计：公司叙事（Company Narrative）—「做过 / 在做 / 要做」

> 日期：2026-07-14 ｜ 状态：已确认，实现中 ｜ 类型：功能（新数据源 + llm + 缓存 + 注入 + 显示）

## 目标 / 两层（用户定）

让分析"懂这家公司"：把"做过什么 / 在做什么 / 要做什么"结合新闻+指标+规则纳入分析并到处输出。**两层，成本分级**：

- **Tier 1 · 每日推荐 = 一句话简版**（零额外 LLM）。
- **Tier 2 · 单股 entry/position = 三段深版**（flash 合成 + 缓存）。

数据源走**路线 A：纯 HTTP，避开 mootdx**（用户机器海外，通达信 TCP 连不上）。本环境已实测：东财概念 slist ✓、东财公告 np-anotice ✓。

## Tier 1 — 每日推荐一句话叙事（0 额外 LLM）

- `daily_recommendation` 提示词里，每只自选股附 **本地新闻库近期标题**（`news_store.query(code, days=30, limit=3)`——便宜、无 live、无封 IP；已由 launchd 定时填充）。
- daily 输出 JSON 每个 pick 增 `narrative` 字段：**一句话**"这公司近期在做什么/什么题材"。
- 前端：每日推荐每只 pick 下显示这句叙事。
- 成本：**同一次 daily 调用**（0 额外 LLM），仅 N 次本地 DB 查询。冷门股本地稀疏→该股 narrative 可空。

## Tier 2 — 单股三段深版（flash 合成 + 当日缓存）

### 新数据源（`datasources.py`，走 `em_get` 限流）
- `announcements(code, n=8)` — 东财 `np-anotice-stock` 公告（沪 `H2_` / 深自动），返回 `[{date,title}]`。→ 支撑"在做/要做"。
- `concept_tags(code)` — 东财 `slist`（`spt=3`）概念/板块归属，返回 `[板块名…]`。→ 支撑"所属方向/题材"。

### 新 llm 函数 `company_profile(...)`（**flash 模型**，`_chat(model=FLASH_MODEL)`）
输入：name/code + 近期新闻 + 财报多期趋势 + 公告 + 概念。输出 JSON：
```json
{"did":"做过什么(主营+历史里程碑,一句)",
 "doing":"在做什么(近期动作/公告/新闻,一句)",
 "will":"要做什么(规划/在建/题材催化,一句;无则写『暂无明确公开规划』)",
 "tags":["概念/方向标签"]}
```
- 只据给定数据、不编造（复用 `_DISCLAIMER` 口径）。

### 缓存
- `ai_cache` kind=`profile`，指纹=`{code}`（+当日）；**TTL 长**（公司叙事变化慢，当日复用）。新闻的时效性由 entry/position 主分析本身每次刷的 live 新闻兜底。

### 注入 + 显示
- entry/position 路由：取 `profile`（缓存优先）→ 拼成【公司叙事·做过/在做/要做+题材】块加进主分析提示词（AI 纳入考虑）→ 响应带 `profile`。
- 前端：entry/position 面板**顶部**「公司叙事」卡（做过/在做/要做 三行 + 题材标签），在溯源条之上。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `datasources.py` | +`announcements(code)` +`concept_tags(code)`（东财 HTTP，em_get 限流） |
| `llm.py` | +`company_profile(...)`（flash）；`daily_recommendation` 提示词加每股本地新闻 + 输出 `narrative` |
| `app.py` | entry/position：取 profile(缓存)+注入+返回；daily：每股附本地新闻标题 |
| `static/app.js` | daily pick 一句话叙事；entry/position 顶部叙事卡 |
| `templates/index.html` | 叙事卡 CSS |

## 测试计划
- 语法 + node --check。
- 数据源：`announcements`/`concept_tags` 实拉 600519 非空。
- LIVE：`company_profile` 出 {did,doing,will,tags}；entry 响应带 `profile`；daily 响应每 pick 带 `narrative`。
- 浏览器：entry/position 顶部叙事卡；每日推荐每只一句话。

## 风险 / 边界
- 公告/概念偶发空/限流 → 该段留空、叙事降级（提示"数据不足"），不阻断主分析。
- 本地新闻库冷门股稀疏 → daily 该股 narrative 空。
- flash 质量弱于 v4-pro，但叙事是概括性、flash 够用（`structure_note` 已用 flash）；主买卖判断仍 v4-pro。
- profile 与 provenance 独立：叙事不进 `basis` 校验（定性、非闭集信号），只作输入与展示。
