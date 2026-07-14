# 设计：AI 建议的数据溯源 + 可验证依据（Provenance + Verified Attribution）

> 日期：2026-07-14 ｜ 状态：待用户 review ｜ 类型：功能（llm 提示词/schema + 新校验模块 + rules_store + 2 路由 + 前端）

## 背景 / 目标

`position_advice`（何时买卖）和 `entry_advice`（深度入场）现在返回结论，但**看不出依据什么**。目标：每次 AI 提议都带**稳定、可核对**的来源与依据，且**可信部分和不可信部分视觉分级**。

范围：**仅 entry + position**（用户核心诉求、结构最适合逐条挂依据）。daily/screen/market 暂不做，跑通后同模式可推广。

## 两层结构

### A · 数据溯源（provenance）——后端确定性生成，100% 稳定
后端拼 prompt 时**已经掌握全部输入**，同步生成一个 `provenance` 对象随 advice 返回。它是"这份分析用了哪些材料 + 多新 + 多少"，**不经过 AI、不会说谎**。

面板顶部常驻一行 chips（源级），点开展开值级明细：
```
本次依据：行情15:03 · 财报26Q1 · 新闻12条(最新2h前) · 规则8条[小/波段] · 大盘研判 · 波动/资金 · 笔记3条 · 联网✓
```

### B · 结论依据（verified attribution）——AI 结构化输出 + 后端校验
每条关键结论挂它的依据，**默认折叠**，点开展开。信任两档：
- `✓ 已核对`：引用的信号名在闭集里 / 规则 ID 在本次注入集里。
- `⚠ 对不上`：AI 引用了**闭集外的信号**或**没注入的规则**（= AI 在编，标红不删，让用户看见）。
- `AI 未给依据`：该结论 AI 没引用任何信号（中性灰，不算对错）。

**关键设计（比"AI 报值 + 容差"更稳）**：AI **只引用信号名 + 规则 ID**，**值由后端权威填充**。
- 你看到的仍是值级（`区间位置=92%`），但值来自后端喂进去的数据、AI 不可能编。
- **省掉容差比对**，不会有四舍五入误报。`⚠` 只对"引用了不存在的信号/规则"亮 —— 这正是"编"的主要形式。

## 闭集信号字典（AI 只能从这里引用；后端据此填值 + 校验）

| 信号名 | 取值来源 | 展示 |
|--------|----------|------|
| 现价 / PE / PB / 换手率 / 涨跌% | `quote.{price,pe_ttm,pb,turnover,chg_pct}` | 名=值 |
| 年化波动 / 20日涨幅 / 区间位置 / 主力20日净流入 | `metrics.{vol,cum20,range_pos,net20}` | 名=值 |
| 60日高 / 60日低 / 20日振幅 | `vol_hist.{hi,lo,atr_pct}` | 名=值 |
| 营收同比 / 净利同比 | `financials[0].{revenue_yoy,profit_yoy}` | 名=值 |
| 大盘研判 | `market_ctx`（仅 entry，定性） | 名=结论文字 |
| 规则 | `R{id}`（rules_store 注入集） | R12·规则标题 |

> 定性源（新闻/笔记/联网）**不进 B 归因**（无法硬核对），只在 A 溯源条体现。

## 实现

### 新模块 `provenance.py`
- `SIGNAL_DICT: {信号名: (source_key, field, formatter)}` —— 上表的机器可读版；同时用于**注入 prompt 的可引用清单** + **后端填值/校验**（单一事实源，避免 prompt 与校验漂移）。
- `build_provenance(quote, metrics, vol_hist, financials, news, rules_meta, market_ctx, web_on, notes_n) -> dict`：拼 A 溯源对象（源 + 新鲜度 + 条数 + 值级 detail）。
- `verify_basis(ai_basis, ctx, injected_rule_ids) -> list`：遍历 AI 的 basis，对每个 ref：
  - `signal`：名在 `SIGNAL_DICT`？→ 后端填 `value`，标 `ok`；否则 `bad`（闭集外）。
  - `rule`：`R{n}` 的 n ∈ `injected_rule_ids`？→ `ok` + 附规则标题；否则 `bad`。
  - 返回富化后的 basis（带 value/label/status）。

### `rules_store.py`
- `for_ai()` 每条规则前缀 `[R{id}]`（现在是 `{title}：{content}` 无 ID）→ AI 才能引用、后端才能校验。
- 新增 `active_rule_ids(scenario) -> set[int]`（复用 `signature()` 里已算的注入 ID 集），供 `verify_basis`。

### `llm.py`（2 个提示词 + schema）
- 提示词加一段【可引用信号】= `SIGNAL_DICT` 的名字列表 + 规则以 `[R{id}]` 注入；要求："每条关键结论在 `basis` 里列依据；`signal` 只能从【可引用信号】选，`rule` 用 `R+编号`；**不要写值**（值由系统填）；无把握就留空。"
- 输出 JSON 加：
  ```json
  "basis":[
    {"claim":"action","signals":["区间位置"],"rules":["R12"]},
    {"claim":"stop_loss","signals":["60日低"],"rules":["R7"]}
  ]
  ```
  `claim` ∈ 该函数的关键结论字段名（position：action/sell_trigger/add_trigger/stop_loss/take_profit；entry：verdict/entry_when/entry_zone/entry_how/stop_loss/targets/future_sell_plan）。
- 旧 `rule_basis`（自由文本）保留兼容，但 UI 以结构化 `basis` 为准。

### `app.py`（2 路由）
- `recommend_position` / `recommend_entry`：拿到 `advice` 后 →
  1. `verify_basis(advice.get("basis"), ctx, rules_store.active_rule_ids(scen))` 富化；
  2. `build_provenance(...)` 生成 A；
  3. 返回 `{advice, provenance, ...}`（`advice.basis` 已富化）。
- **缓存**：`provenance` 与富化 basis 一起进 `ai_cache`（随 advice 存），命中直接回，不重算。

### 前端 `static/app.js`（+ 必要 CSS）
- position/entry 面板顶部渲染 **溯源条**（chips + 点开展开值级明细）。
- 每条关键结论后渲染 **依据**（默认折叠；点结论/"看依据"展开）：`区间位置=92% · R12结构止损  ✓` / `⚠对不上` / `AI未给依据`。
- A（确定性，实色）与 B（AI 自述已校验，灰底 + 角标）**视觉分级**。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `provenance.py`（新） | SIGNAL_DICT + build_provenance + verify_basis |
| `rules_store.py` | for_ai 加 `[R{id}]` 前缀；新增 active_rule_ids |
| `llm.py` | position/entry 提示词加可引用信号清单 + `basis` schema |
| `app.py` | 2 路由：verify_basis + build_provenance，随响应/缓存返回 |
| `static/app.js` | 溯源条 + 结论依据渲染（默认收起）；A/B 视觉分级 |
| `templates/index.html` | 溯源条/依据的 CSS |

## 测试计划

- 语法：python ast（provenance/rules_store/llm/app）+ node --check。
- 起 5001，`POST /api/recommend/entry/<code>`：响应含 `provenance`（源+新鲜度+条数）+ `advice.basis`（每条带 status）。
- 构造校验用例：
  - AI 引用合法信号「区间位置」→ 后端填 92% + `✓`；
  - AI 引用「R99」(未注入) → `⚠`；引用「玄学指标」(闭集外) → `⚠`；
  - AI basis 空 → 结论标「AI 未给依据」。
- 浏览器：深挖 → entry/position 面板 → 溯源条可见、点开展开；结论依据默认收起、点开出 `✓/⚠`。

## 风险 / 边界

- **AI 不配合结构化**：DeepSeek 可能少给或给错 `basis` → 有把握才要求填、空则「未给依据」、错则 `⚠`，不阻断主结论。
- **闭集要与 prompt 同源**：信号清单从 `SIGNAL_DICT` 生成注入，避免 prompt 写了但校验没有（或反之）。
- **max_tokens**：basis 增加输出长度，position=5000/entry 当前值可能要略调大（推理模型，别调小）。
- **缓存指纹**：provenance/basis 不改变缓存 key（仍是 rules signature + 输入指纹）；富化结果随 advice 一起缓存。
- 定性源不进 B（已决），避免"确认喂过≠确认用了"的模糊信任。
