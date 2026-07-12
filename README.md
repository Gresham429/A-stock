# 观察台 · A-Share Watchdesk

一个**本地运行**的 A 股看板：多股对比 + 点击深挖（研报/龙虎榜/解禁/资金流）+ 随时增删自选股 + **持仓盈亏跟踪** + **DeepSeek AI 每日推荐与买卖时机建议** + **每个名词的新手解释**。

> 为什么是本地应用而非托管网页？深挖、加股票、调 AI 都需要**实时外部请求**，而托管型 Artifact 有严格 CSP 禁止一切外部请求。本地 Flask 后端代理即可。

## 快速开始

```bash
pip install -r requirements.txt      # 只依赖 flask
python app.py                        # 启动后端
# 浏览器打开 http://127.0.0.1:5000
```

AI 功能读取根目录 `.env` 里的 DeepSeek 配置（已 `.gitignore`，不进 git）：

```
DEEPSEEK_API_KEY=sk-xxxx
DEEPSEEK_MODEL=deepseek-v4-pro       # 也可用 deepseek-v4-flash（更快更省）
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

改 key 后重启后端生效。若 `.env` 未配置，AI 按钮自动隐藏，其余功能照常。

## 功能

| 区域 | 能力 |
|------|------|
| **自选股对比** | 现价、涨跌、PE、PB、年化波动、20日涨幅、区间位置条、主力20日净流入、1手成本、30日走势迷你图。点表头排序，点行深挖。 |
| **点击深挖** | 右侧抽屉 5 页：概览（指标+情景区间+走势）、研报（评级/EPS/PDF直链）、龙虎榜（席位TOP5+机构标注）、解禁风险（等级）、资金流（30日柱状图）。 |
| **我的持仓** | 记录代码/股数/成本价/买入日 → 实时算市值与盈亏（红赚绿亏），组合汇总。持久化 `portfolio.json`。 |
| **🤖 每日推荐** | 把全部自选股指标 + 持仓喂给 DeepSeek，返回每只 buy/sell/hold/add/reduce/watch + 理由 + 风险 + 整体市场观点 + 持仓提醒。 |
| **🤖 何时卖** | 单只持仓 → DeepSeek 结合你的成本给出卖出条件/加仓条件/止损/止盈价位。 |
| **📖 名词解释** | 每个名词都有新手向说明：表头 ⓘ 悬浮提示 + 顶部「名词解释」总表弹窗。 |
| 增删自选/持仓 | 输入代码即加；一键删除。 |
| 自动刷新 | 可选 60 秒轮询。 |

约定：**红涨绿跌**（A 股惯例）。所有 AI 输出均标注「参考信号，不构成投资建议」。

## 数据源

| 数据 | 源 | 说明 |
|------|-----|------|
| 行情/估值 | 腾讯财经 | 不封 IP |
| 波动率/资金流 | 新浪 MoneyFlow | 含每日收盘价，一份数据两用 |
| 研报/龙虎榜/解禁 | 东财 reportapi + datacenter | 走 `em_get` 串行限流 |
| AI 分析 | DeepSeek `deepseek-v4-pro`（推理模型） | OpenAI 兼容 HTTP，密钥来自 `.env` |

## 文件结构

```
A-stock/
├── app.py              # Flask 路由（后端）
├── datasources.py      # 行情/指标/研报/龙虎榜/解禁
├── llm.py              # DeepSeek 集成：每日推荐 / 持仓建议
├── portfolio.py        # 持仓记录 + 盈亏计算
├── store.py            # 自选股持久化
├── config.py           # 读取 .env（密钥不硬编码）
├── templates/index.html# 看板结构 + 样式（深色终端风）
├── static/app.js       # 前端逻辑（对比/深挖/持仓/AI/名词解释）
├── .env                # DeepSeek 密钥（gitignore，勿提交）
├── .gitignore
├── watchlist.json / portfolio.json   # 本地数据（自动生成）
└── requirements.txt
```

## 安全

- **密钥只存在 `.env`，源码不含任何 key**；`.gitignore` 已排除 `.env`、`watchlist.json`、`portfolio.json`。
- 若 key 曾在聊天/截图等处明文暴露，建议到 DeepSeek 后台重置后写回 `.env`。

## 注意

- **交易时段**：盘中实时；非交易时段显示最近交易日数据。
- **AI 速度/成本**：`deepseek-v4-pro` 是推理模型，每日推荐约 20~40 秒；嫌慢可在 `.env` 换 `deepseek-v4-flash`。已给足 `max_tokens`（推理会占用额度）。
- **东财限流**：深挖偶发空数据是东财对当前 IP 的间歇风控，重试或换网络即可（页面显示「无数据」不崩）。
- **仅供研究，不构成投资建议**；AI 与情景区间只描述客观信号与波动幅度，不保证收益。

## 免责声明 / Disclaimer

本项目仅用于技术学习与个人研究，**不构成任何投资建议**。所有行情、指标、情景区间及 AI 生成内容均基于公开数据的客观计算或模型输出，可能存在错误或延迟，不保证准确性、完整性或收益。据此进行的任何投资决策及其后果由使用者自行承担。数据来源于各第三方公开接口，其可用性与合规性由相应提供方负责。

## License

[MIT](LICENSE) © 2026 Gresham429
