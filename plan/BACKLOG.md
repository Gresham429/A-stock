# 待办（唯一一份）

待办只记在这里；CLAUDE.md 只留一行指向。做完一项就删掉那一项，git 留历史。
每做一项：补离线单测，跑 `python3 tools/status.py`（含全部离线测试），改后端则重启 app 验 boot，单独 commit。

分四类：正在推进的子项目、可直接做的收尾、需用户签字才改、卡在外部条件。

## 一、子项目（用户 2026-09-16 提出，顺序 B、C、A、D）

- B 三周期选股 + 自选股买卖点 + 观点账本：已完成并部署，设计见 `2026-09-16-picks-ledger-design.md`。
  已知待调：长线候选池的 pe 乘 pb 规则把整池选成银行股，要换成行业内分位或加行业分散约束。
- C 到点提醒：预测价接近买点或卖点时通知手机。渠道未定（阿里云短信、Bark、Telegram 三选一），
  需用户决定；数据来源是 picks 账本的 open 行加实时行情。
- A 复盘加个股与自选股：复盘从板块层面往下走一层，板块资金流找资金聚集和涨幅榜标的，
  再给这些标的和自选股补基本面、舆情、财务、驱动；不给买卖点（买卖点归 B）。
- D 浅色主题 + 手机适配：整站浅白色；手机与电脑都能正常用。

## 二、可直接做的收尾（修法明确，时间自定）

- 登录失败按 ip 键锁定：NAT 共享出口时朋友连错 5 次会把同 IP 的站长也锁 5 分钟（设计取舍，
  README-deploy 已写明）。
- 大文件：`app.py`、`agent_loop.py`、`datasources.py`、`static/app.js` 超过 400 行目标
  （行数见 `python3 tools/status.py`）。可先把 `datasources` 的 `_em_*` 五个函数挪 `em_throttle.py`，
  `app.py` 的 `_fleet_route`/`ensure_user_stores` 挪 `fleet_routes.py`。高风险纯维护性收益，
  真做先拆 `agent_loop`（deciders 挪 `agent_deciders.py`、风控与结算挪 `agent_risk.py`），独立分支。
- 仍无单测：`template_store` / `provenance` / `rules_store` / `profile_store` / `news_store` /
  `notes_store` / `astockctl` / `scheduler`。
- 运行时输出里还有装饰符号（`tests/test_review_metrics.py` 的通过提示、前端芯片文案）；文档已清完，
  代码按同一口径改时顺手处理。

## 三、需用户签字才改（自主会话只分析、列建议，不直接改）

- `sample_codes` 市值分层实为代码分层（PITFALLS #5b）：`codes_of()` 无 focus 按代码号排序，
  所以全池 IC 与 `excess_dist` 的样本是代码分层。未修，因为动它会碰判罪线与冻结分位。
  要修须重排样本、重跑分布、确认冻结分位不受影响。
- `_PRESCREEN=600` 是否扩池纳入小盘：覆盖分析见 `2026-07-18-prescreen-coverage-analysis.md`，
  方向问题已用 cohort-aware 打分修掉，池子本身没动。
- 阈值 `CHASE_HIGH_POS` 85 或 80：只用于 detect_failures 教训门，85 是线性惩罚上的任意切点。
  建议保持 85（降到 80 只会多记追高教训）。
- 纪律参数（不需数据验证，但改前需用户认可）：`MAX_VOL=120`、`MAX_POS_PCT=30`、`MIN_CASH_PCT=10`、
  `VOL_FLOOR/CEIL=15/130`、`STOP_LOSS_PCT=-10`（已回测）、`LESSON_PCT=10`（用户 2026-07-16 认可）。
  权威表在 `2026-07-18-agent-logic-map.md` 第 13 节。
- agent 画像（1/8/9/10）维持「免最低佣金」不改（用户 2026-07-16 决定）。后果：agent 的保本涨幅与
  `below_breakeven` 教训基于比用户更低的成本，模拟盈亏偏乐观。要改再确认。
- `DebateDecider` 默认不启用（UI 可选）：先用 single 拿基线，用数据证明需要再切。
- 逐笔费率快照：画像只存单一费率、无 as-of 快照。用户单笔超过 20000 元（万 2.5 的 5 元分界）之后
  历史笔与新笔才会用到不同费率；小单都触 5 元最低，无影响，暂不做。
- `llm.py` 的 `daily_recommendation` 与 `market_screen` 提示词里硬编码「本金约 1 万、偏好科技股」的画像
  文字（llm.py 约 296/510 行），与注入的 `_tier_block` 动态档位块可能互相矛盾。删掉硬编码或改成从画像
  取值会改变这两类 AI 输出，改前需用户认可。
- 条件单补判被撮合拒绝时（典型是跌停封板卖不出）现在记 `cancelled`，止损保护就此消失；另一种语义是
  放回 `live`、次日继续尝试。属交易语义选择，不是 bug，改前需用户定。

## 四、卡在外部条件

等数据（代码全就位）：
- 让 20 个 agent 攒数据：记忆闭环 P1 到 P3 已落地，`journal` 与教训库靠真实交易日填充。
  本地开 app 即自动跑；服务器上 agent 不自动跑（用户决定），要站长手动触发。
  别为攒数据松风控：07-16/17 早盘 AI 正确拒绝逆势、0 成交，尾盘转好才成交。
- 舰队到提示词提炼（用户的最终目标）：等教训库有数据再做，见 `2026-07-16-agent-evolution-design.md`。

卡在数据源（用户要求：找到可用源时提醒他加）：
- 资金分量 `net20` 无法回测。`ds.sina_metrics()` 的 `series` 只回 30 天（源头
  `MoneyFlow.ssl_qsfx_zjlrqs` 传 `num=260` 也只给 30），因子回测要 600 天以上。所以 `_pa_score`
  四个分量里唯独资金项未验方向、不参与打分、仅展示。需要能给 600 天以上每日主力净流入的接口，
  候选（均未验证）：东财 `push2his`（住宅 IP 间歇封锁，服务器 IP 可试）、通达信 `mootdx`
  （海外可用性存疑）、同花顺 / 聚宽 / Tushare（多需 token）。接上后：`factor_lab.FACTORS` 加
  `net20`，重跑 `backtest()`，`direction()` 定方向，`_pa_score` 纳入。
- 宏观缺美元指数与 VIX。`ds.global_markets()` 用的新浪外盘没有这两个（`hf_DX` 返回空，VIX 无代码）。
  `macro_digest` 有油价、黄金、铜、美股三大指数，缺这两个风险指标。需要海外可用的实时 HTTP 接口，
  候选（均未验证）：东财外盘、腾讯外盘、Yahoo Finance（`^VIX` / `DX-Y.NYB`）。接上后
  `ds.global_markets()` 加两个条目即可，提示词已写明数值优先于新闻措辞。

用户侧（我做不了）：
- 服务器：阿里云安全组只放 22 与 Tailscale；`deploy/harden_ssh.sh` 需另开终端验证后再跑。
- Mac 上 Clash 等系统代理要把 `*.ts.net` 与 `100.64.0.0/10` 加进直连，否则 Safari 打不开站点。
- 服务器上站长的自选股为空：先在页面加自选股，再点「刷新买卖点」。

## 可选后续（未承诺）

- 溯源（provenance）推广：现仅 entry/position；daily/screen/market 也可加；picks 自带 basis 但没走
  `provenance.verify_basis`。
- 玩法 template 文案可编辑：现 5 档玩法是代码常量，只读展示。
- 新闻增量抓取只轮询精选龙头池（`universe.all_codes()`）+ 自选股（news_store 的 `_universe_slice` 与研报
  路径），全市场其余个股新闻靠深挖时惰性抓取。要全市场滚动新闻需扩池（抓取量约 30 倍），未承诺。
- `agent_store.runs.detail` 原文截断 20000 字符（agent_store.py:232）：debate 原始输出较长时被截，
  复盘追溯能力受存储上限约束。可调大或改摘要策略，未承诺。
