# L1 spec：AI 输出短期缓存 + 时间戳

- 日期：2026-07-13
- 属架构总纲 L1（先做，最小独立、立即见效）
- 影响文件：新增 `ai_cache.py`；改 `app.py`（4 个 AI 路由 + config）、`static/app.js`、`templates/index.html`、`.gitignore`、`CLAUDE.md`

## 目标
1. 4 个 AI 调用（daily / screen / position / market_overview）结果**落盘缓存 + 时间戳**。
2. **智能命中**：输入未变且未过期 → 秒回缓存，不重复调 30–90s 的 DeepSeek；输入变 / 跨交易日 / 过期 / 点强制刷新 → 重算。
3. 每个 AI 面板显示「分析于 X（Y 分钟前）· 命中缓存/实时」+「🔄 强制刷新」。
4. 顺手把 market_overview 现有 5min 内存缓存统一进来（落盘 + 时间戳）。

非目标：不做长历史（短期缓存、latest-per-key）；"结合大量知识"属 L2/L4，本层不碰。

## 新模块 `ai_cache.py`
```
存储：ai_cache.json（项目根，gitignore），dict：
  key -> {result, ts(ISO), model, kind}
key = f"{kind}:{input_hash}:{date}"    # date=YYYY-MM-DD，跨日自然失效
input_hash = sha1(canonical(inputs))[:12]
TTL(秒) = {daily:1800, screen:1800, position:1800, market:300}
```
**输入指纹**（只取"会影响结论"的输入，不含实时价格，否则每次报价跳动都 miss）：
- `daily`：sorted(自选股代码) + sorted([(code,shares,cost) for 持仓])
- `screen`：capital + focus
- `position`：code + 该持仓(shares,cost,buy_date)
- `market`：无用户输入 → key 用 date + `floor(now/5min)` 桶（等价现有 5min）

**API**：
- `get(kind, inputs) -> {result, ts, age_min} | None`（命中且未过期才返回）
- `put(kind, inputs, result, model)`
- `invalidate(kind=None)`（改自选/持仓时可主动失效 daily/position）
- 线程安全：`threading.Lock` + 原子写（写 tmp 再 `os.replace`）
- 自清理：load/put 时丢弃 key 里 date≠今天 的条目，文件恒定很小

## app.py 集成（4 路由统一模式）
每个 AI 路由：
```
force = 请求带 refresh=1 / body.force
if not force:
    hit = ai_cache.get(kind, inputs)
    if hit: return {..., result:hit.result, cached:True,
                    analyzed_at:hit.ts, age_min:hit.age_min}
result = llm.xxx(...)            # 未命中才真调
ai_cache.put(kind, inputs, result, model)
return {..., result, cached:False, analyzed_at:now, age_min:0}
```
- `market_overview`：去掉 `_MKT_CACHE`，改走 `ai_cache`（kind=market）；`?refresh=1` 已有，复用为 force。
- `daily`/`screen`/`position`：POST 加 `body.force`（或 `?refresh=1`）。
- 自选股增删、持仓增删的路由里调 `ai_cache.invalidate("daily")`/`invalidate("position")`，避免下次命中旧结论（也可不主动失效，靠输入指纹变化自然 miss——**采用后者，更简单**，指纹已含自选/持仓集合）。

## 前端（每个 AI 面板一致）
- 结果区顶部加一行 meta：`🕐 分析于 2026-07-13 14:30（8 分钟前）· 命中缓存` 或 `· 实时`，右侧「🔄 强制刷新」按钮。
- daily（recBody）/ screen（recBody）/ position（folioAdvice 卡片）/ market（大盘条 detail）都显示。
- 「强制刷新」= 用 force 重新请求；沿用各自请求令牌（recSeq/detailSeq/mktSeq）。
- position 的缓存 meta 与上一步修的 `FOLIO_ADV` 持久化叠加：缓存命中时也把 analyzed_at 一起缓存进 FOLIO_ADV。

## 安全 / 约定
- `.gitignore` 增 `ai_cache.json`。
- CLAUDE.md 补：AI 结果落盘缓存策略 + 强制刷新。

## 验证（改完自测）
```bash
python app.py &   # 注意 5000 可能被 AirPlay 占，测试可用 5001
# 同参数连打两次：第 2 次应秒回且 cached:true
curl -s -XPOST :5000/api/recommend/screen -d '{"capital":10000,"focus_sector":"锂电池"}' -H 'Content-Type: application/json' --max-time 200 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('cached'),d.get('analyzed_at'))"
curl -s -XPOST :5000/api/recommend/screen -d '{"capital":10000,"focus_sector":"锂电池"}' -H 'Content-Type: application/json' | python3 -c "import json,sys;d=json.load(sys.stdin);print('2nd:',d.get('cached'))"   # 期望 True 且快
# 强制刷新绕过
curl -s -XPOST :5000/api/recommend/screen -d '{"capital":10000,"focus_sector":"锂电池","force":true}' ... # cached:false
# 改自选后 daily 应 miss（指纹变）
python3 -c "import ast;[ast.parse(open(f).read()) for f in ['ai_cache.py','app.py']]"
node --check static/app.js
```
验收：同参第 2 次 `cached:true` 且秒回；`force`/`refresh=1` 重算；改自选/持仓/资金档 → 对应 miss；每面板显示"分析于 X + 强制刷新"。

## 风险
- 输入指纹要**排除实时价格**（否则秒秒 miss，缓存失效）——只哈希自选集合/持仓/资金/板块/代码。
- 缓存里是"当时"的结论，可能与最新行情有偏差 → 靠"分析于 X（Y 分钟前）"如实标注 + 强制刷新兜底。
- 多线程写 json → 锁 + 原子写，避免写坏。
