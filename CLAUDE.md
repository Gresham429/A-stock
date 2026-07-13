# CLAUDE.md — A股观察台 / A-Share Watchdesk

> 给未来会话（换 session）的项目上下文。读完这份即可无缝接手，不必重新摸索。

## 这是什么

一个**本地运行**的 A 股看板：多股对比 + 点击深挖 + 持仓盈亏 + DeepSeek AI 推荐/建议 + 名词解释。
纯本地 Flask 后端代理各数据源，前端零构建（HTML+CSS+原生 JS）。**为什么不是托管网页**：深挖/加股票/调 AI/联网搜索都要实时外部请求，而托管型 Artifact 的 CSP 禁止一切外部请求，做不到。

## 快速开始

```bash
pip install -r requirements.txt      # 只依赖 flask
python app.py                        # http://127.0.0.1:5000
```

数据文件（`watchlist.json` / `portfolio.json`）首次运行自动生成，均已 gitignore。

## 文件地图

| 文件 | 职责 |
|------|------|
| `app.py` | Flask 路由（后端入口） |
| `datasources.py` | 数据层：行情/指标/研报/龙虎榜/解禁/K线/财报/新闻/快讯 |
| `llm.py` | DeepSeek 集成：`daily_recommendation` / `market_screen` / `position_advice` |
| `websearch.py` | 博查联网搜索（B 方案，可选）+ key 健康检测/到期提醒 |
| `portfolio.py` | 持仓记录 + 总盈亏 + 当日盈亏 |
| `universe.py` | 科技股候选池（9 板块，供全市场筛选） |
| `store.py` | 自选股持久化 |
| `config.py` | 读 `.env`（DeepSeek + 博查 key，绝不硬编码） |
| `templates/index.html` | UI 结构 + 全部 CSS（深色终端风） |
| `static/app.js` | 全部前端逻辑（对比/深挖/K线/持仓/AI/名词/到期提醒） |

## 数据源 & 坑（改代码前必读）

优先级：能用**腾讯/新浪/mootdx**（不封 IP）就别用东财；东财仅用于其独有数据且走限流。

| 数据 | 源 | 备注 / 坑 |
|------|-----|----------|
| 实时行情/估值 | 腾讯 `qt.gtimg.cn`（GBK） | `tencent_quote` 必须含 `last_close`(f4)/`change_amt`(f31)，否则**当日盈亏恒为 0** |
| 波动率/资金流 | 新浪 MoneyFlow | 返回里带每日收盘价 `trade` → 波动率与资金流一份数据两用 |
| 日K线 OHLC | 新浪 `getKLineData` | 腾讯 `hqkline` 端点已失效（`code:11`）；用新浪 |
| 研报/龙虎榜/解禁 | 东财 `reportapi`/`datacenter` | 走 `em_get()` 串行限流（间隔≥1s） |
| 个股资金流 push2/push2his | 东财 | **部分住宅 IP 间歇封锁** → 资金流一律用新浪，别依赖 push2his |
| 财报三表 | 新浪 | `financial_summary` 取营收/归母净利 + 同比 |
| 新闻 | 东财个股新闻 + 财联社快讯 + 东财7×24 | 财联社走 v1 API + 本地签名（`md5(sha1(sorted query))`），零 key |

## AI 配置（DeepSeek + 博查）

- **DeepSeek**：`deepseek-v4-pro`（该账号**只有** v4-pro / v4-flash 可用）。是**推理模型**——`max_tokens` 同时覆盖「思考+正文」，太小会导致思考耗尽、正文返回空。已设 daily=8000 / position=5000 / screen=9000，**别调小**。OpenAI 兼容 `POST /chat/completions`，支持 `response_format:{type:json_object}`，零 SDK（纯 urllib）。
- **博查（可选，B 方案）**：`POST api.bochaai.com/v1/web-search`，Bearer 鉴权，body `{query,freshness,summary,count}`，成功响应 `webPages.value[].{name,url,siteName,snippet,summary}`，错误体 `{"code":"401","message":"Invalid API KEY"}`。
  - **变量名**：`.env` 里用 `BOCHAAI_API_KEY`（博查惯例，README/`.env` 以此为准）；`config` 也兼容 `BOCHA_API_KEY`（`os.environ.get("BOCHA_API_KEY") or os.environ.get("BOCHAAI_API_KEY")`）。
  - **到期提醒**：`websearch._classify()` 把 401→key 无效/过期、402/403/余额→余额不足、429→限流；`/api/websearch/status?probe=1` 主动探测；前端启动时探测，失效则顶部红条 + 芯片 `🌐联网⚠`。
- **密钥只在 `.env`（gitignore），源码零硬编码。** 后端**不能**调用 Claude 的 WebSearch（那是对话侧工具）——看板联网靠自己的源（A 新闻 API + B 博查）。

## 联网知识 A+B（AI 每次分析前先抓实时资讯）

- **A（免费默认开）**：`market_news_digest()` = 财联社快讯 + 东财7×24，喂进三个 AI 提示词。
- **B（可选）**：配了博查 key 时，`app._ai_web_context()` 追加博查搜索结果。
- 三个 llm 函数都有 `web_context` 参数；`/api/config` 暴露 `news_augment`/`web_search`；芯片显示 `📰新闻` / `🌐联网`。

## 约定

- **红涨绿跌**（A 股惯例，与美股相反）——所有涨跌/盈亏配色遵此。
- **情景区间** = 年化波动率反推（±1σ≈68%、±2σ≈95%），**只描述波动幅度，不预测方向**。
- 所有 AI 输出必须标注「参考信号，不构成投资建议」。
- 前端字体用系统栈（`PingFang SC`/`Microsoft YaHei`…），**不加载 Google Fonts**（国内会失败）；图表纯内联 SVG，零外部依赖。
- **异步渲染必须带请求令牌**：多个慢请求（daily/screen/openDetail）写同一 DOM 目标时，用单调计数器 `recSeq`/`detailSeq`——发起即 `++`，`await` 回来后 `if(gen!==seq) return` 丢弃过期响应，否则切换时旧响应会覆盖新视图（错位 bug）。新增此类异步入口时照做。

## 冒烟测试（改完自测）

```bash
python app.py &                                   # 起服务
curl -s localhost:5000/api/config                 # llm/news/web_search 状态
curl -s localhost:5000/api/overview | head -c 300 # 自选股对比
curl -s "localhost:5000/api/websearch/status?probe=1"   # 博查健康(到期提醒)
# AI 类接口慢(推理 30~90s)，用 --max-time 200
```

Python 语法：`python3 -c "import ast; [ast.parse(open(f).read()) for f in [...]]"`；JS：`node --check static/app.js`。

## 安全（提交前必做）

- `.gitignore` 排除：`.env` / `.env.*` / `.claude/` / `.vscode/` / `__pycache__/` / `watchlist.json` / `portfolio.json`。
- **提交前铁律**：`git add -A` 后跑
  ```bash
  git ls-files --error-unmatch .env    # 必须报错(=未跟踪)
  git ls-files -z | xargs -0 grep -lE "sk-[A-Za-z0-9]{16,}"   # 必须无输出
  ```
- 绝不把 key / 个人邮箱 / 本地绝对路径写进被跟踪文件。
- 远程 `git@github.com:Gresham429/A-stock.git`（main 分支，MIT，Conventional Commits）。`gh` 未登录，推送走 SSH（已配好）。

## 代码风格

- 小文件（目标 <400 行）、类型注解、模块级 `logger`、不要裸 `except`、不硬编码密钥。
- 所有东财请求走 `em_get()`（内置限流）；新数据源优先选不封 IP 的腾讯/新浪。

## 当前状态 / 待办

- 功能已全部实测通过并推送 GitHub。
- **待用户提供真实持仓**（代码+股数+成本价）→ 才能跑「逐只持仓」的深度建议（`portfolio.json` 目前为空）。
