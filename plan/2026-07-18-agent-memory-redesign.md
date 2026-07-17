# agent 记忆重构（共享底座：一份 journal、多个视图）

> 2026-07-18。用户反馈旧 mem「互相隔离、不知道自己之前的判断、给出的记忆总是变动」，聊完重做。
> 事后补记（brainstorm 时用户不要正式 spec，落地后补此设计 note）。

## 三个症状 = 三件被旧设计混为一谈的事

旧 mem 只喂两样薄东西：**教训计数**（"追高 3 次"）+ **已结算战绩**（20 日超额 + 每次 render 重算的分位）。
诊断（含 file:line 见当时 Explore 报告）：

1. **「不知道自己之前的判断」= 缺情节连续性**：raw 决策写进 `runs.detail`/`pending.reason` 但**从不读回**。
2. **「记忆总是变动」= 缺稳定性**：战绩分位每次用 `factor_lab.rank_of` 对**漂移的判罪线**重算 → 同一笔跳动。
3. **「互相隔离」= 隔离是**故意**的**（多-agent A/B 干净），用户想要连续性+共享，但不想丢 A/B。

## 设计：分开三层

- **情节 journal（immutable, append-only）**：每次决策一行，含理由+结果 → 连续性 + 结构上不会 flicker。
- **派生教训（结算时锁死判罪分位）** → 单调事实。
- **确定性检索**（显式 ORDER BY + 固定 limit + 平局键）。
- **隔离正交**：每 agent 主看自己的 journal（A/B 保住）；「共享」是独立的只读层。

## 三个当时没定、用户拍板/我判断的点

1. **理由内容**：复用**已有**的 reason/skip_reason 原话，**不新增 LLM 反思**（否则回到"5 样本拟合噪音"）。
   关键 realization：理由本就已生成、只是没读回 → P2 主要是**接线**，不是"让 AI 多想"。
2. **交易 agent 读多少别人的**（用户选）：**自视图为主 + 全舰队只读层**。市场真理(追高普遍跑输)该共享、
   标签区分「你本账户」vs「全体账户」；策略身份在档位/自视图、不在这层 → 不趋同。
3. **regime tag**：粗粒度 `趋势/情绪`(~9 桶)，从上证 vs MA20 + 涨停−跌停净数算；缺数据降级 flat/neutral。
4. **自视图 8 条上限 + 压过度锚定**（我判断）：末尾一句"别把弱势里连续观望演成再不敢动"。

## 落地（P1–P3，均已推 main、有测）

- **P1**(`e00c3d5`)：`entries.x20_pctile` 冻结列 + 迁移；`settle_entries` 算一次传 `_judge_entry`；
  `_agent_history_block` 读冻结值；`for_ai`/`lesson_rollup` 平局键。
- **P2**(`4ea20fe`)：`agent_store.journal` 表 + `journal_add/of/staple_outcome`；`agent_loop` 买入/观望写
  journal + 结算贴结果 + `_agent_journal_block` 注入自视图；`_regime_tag` 随大盘缓存更新。
- **P2c+P3**(`6166763`)：`ai_blocks._stock_house_view(code)` 注入深挖/持仓；`_agent_blocks` 加全舰队只读层。
- 测试：`test_agent_memory`(13，含冻结/确定性/journal 自视图/staple 幂等/house-view/regime)。

## 未来函数安全 / 未做

- journal 只记既成事实 + 复用当时理由，结算走既成日 K（同 PITFALLS#3），无泄漏。
- **未做**：`regime-view`（大盘研判按同类行情回看 journal，稀疏+接线重，暂缓）；debate token 校验（journal 满后）；
  舰队→提示词提炼(D)。见 CLAUDE.md「下一步 🟢」。
