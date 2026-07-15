"""A股观察台 —— 本地看板后端。

启动:  python app.py   然后浏览器打开 http://127.0.0.1:5000

路由:
    GET  /                          看板页面
    GET  /api/config                LLM 是否可用 + 模型名
    GET  /api/overview              自选股全量对比数据(行情+指标+情景区间)
    GET  /api/detail/<code>         单只深挖(研报/龙虎榜/解禁/资金流)
    GET  /api/watchlist             当前自选股列表
    POST /api/watchlist/add|remove  增/删自选股 {code}
    GET  /api/portfolio             持仓 + 盈亏 + 汇总
    POST /api/portfolio/add|remove  记录/卖出持仓
    POST /api/recommend/daily       DeepSeek 每日推荐(自选股+持仓)
    POST /api/recommend/position/<code>  DeepSeek 单只持仓卖出/买入建议
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import ai_cache
import config
import datasources as ds
import llm
import news_store
import notes_store
import paper_store
import portfolio
import profile_store
import provenance
import rules_store
import store
import universe
import websearch

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
news_store.init()  # 确保 news.db 表存在（廉价，幂等）
notes_store.init()  # 私域笔记表
rules_store.init()  # 交易规则库（首次灌入蒸馏种子）
paper_store.init()  # 模拟交易存档
profile_store.init()  # 本地多档投资画像（现金本金→按总资产分级玩法）
_news_refreshing = [False]


def _build_row(code: str, quote: dict) -> dict:
    """合并单只股票的行情 + 波动/资金指标 + 情景区间。"""
    m = ds.sina_metrics(code)
    q = quote or {}
    band = ds.scenario_band(q.get("price", 0), m.get("vol"))
    return {**q, **m, "code": code, "band": band}


def _overview_rows(codes: list[str]) -> list[dict]:
    """构建自选股全量对比行（overview 与 每日推荐 共用）。"""
    if not codes:
        return []
    quotes = ds.tencent_quote(codes)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda c: _build_row(c, quotes.get(c, {})), codes))
    order = {c: i for i, c in enumerate(codes)}
    rows.sort(key=lambda r: order.get(r["code"], 999))
    return rows


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/api/config")
def api_config():
    """前端据此决定是否显示 AI 功能。"""
    return jsonify({"llm_enabled": config.llm_enabled(),
                    "model": config.DEEPSEEK_MODEL if config.llm_enabled() else None,
                    "news_augment": True,
                    "web_search": config.bocha_enabled(),
                    "taxonomy": universe.taxonomy()})


@app.route("/api/websearch/status")
def websearch_status():
    """博查 key 健康检测（用于到期/失效提醒）。?probe=1 主动测一次。"""
    if not config.bocha_enabled():
        return jsonify({"configured": False})
    if request.args.get("probe") == "1":
        return jsonify(websearch.probe())
    return jsonify({"configured": True, **websearch.status()})


@app.route("/api/overview")
def overview():
    """自选股全量对比：一次腾讯批量行情 + 并发拉各自波动/资金指标。"""
    return jsonify({"rows": _overview_rows(store.load_watchlist()), "updated": _now()})


# ── 大盘研判（指数 + 情绪 + AI 局势分析，走 ai_cache 短期缓存） ────────────
def _market_overview_payload(force: bool = False, with_ai: bool = True) -> dict:
    """组装大盘研判：指数(腾讯) + 情绪(东财) + AI 局势分析。带 5 分钟缓存。

    with_ai=False：只取指数（快，供前端先渲染行情条），不碰东财情绪与慢 AI、不写缓存。
    """
    if not with_ai:
        idx = ds.index_quotes()
        return {"ok": True, "indices": idx.get("indices", []),
                "amount_liang_yi": idx.get("amount_liang_yi"),
                "breadth": None, "ai": None, "partial": True,
                "model": config.DEEPSEEK_MODEL if config.llm_enabled() else None,
                "updated": _now(), "cached": False}
    mkt_inputs = {"rules": rules_store.signature()}
    if not force:
        hit = ai_cache.get("market", mkt_inputs)
        if hit:
            return {**hit["result"], "cached": True,
                    "analyzed_at": hit["ts"], "age_min": hit["age_min"]}
    idx = ds.index_quotes()
    breadth = ds.market_breadth()
    ai = None
    if config.llm_enabled() and idx.get("indices"):
        try:
            ai = llm.market_overview(idx["indices"], breadth, _tier_block() + _macro_block() + _ai_web_context("market"))
        except llm.LLMError as e:
            logger.warning("大盘研判 AI 失败: %s", e)
    model = config.DEEPSEEK_MODEL if config.llm_enabled() else None
    core = {"ok": True, "indices": idx.get("indices", []),
            "amount_liang_yi": idx.get("amount_liang_yi"),
            "breadth": breadth, "ai": ai, "model": model, "updated": _now()}
    ts = ai_cache.put("market", mkt_inputs, core, model or "")
    return {**core, "cached": False, "analyzed_at": ts, "age_min": 0}


@app.route("/api/market/overview")
def market_overview_api():
    """大盘研判：五大指数 + 两市成交额 + 市场情绪 + AI 局势分析。

    ?ai=0 只返回指数(快)；?refresh=1 强制重算 AI 研判。
    """
    return jsonify(_market_overview_payload(
        force=request.args.get("refresh") == "1",
        with_ai=request.args.get("ai") != "0"))


# ── L2 新闻/政策资讯库 ─────────────────────────────────────────────────────
def _arg_int(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@app.route("/api/news")
def news_list():
    """查询新闻库：?sector=&code=&kind=&days=&limit= 。"""
    return jsonify({
        "news": news_store.query(
            sector=request.args.get("sector", ""), code=request.args.get("code", ""),
            kind=request.args.get("kind", ""), days=_arg_int("days", 365),
            limit=_arg_int("limit", 60)),
        "status": news_store.stats()})


@app.route("/api/news/refresh", methods=["POST"])
def news_refresh():
    """触发增量抓取（后台线程、不阻塞）。看盘惰性刷新 + 手动刷新共用。"""
    if _news_refreshing[0]:
        return jsonify({"ok": True, "running": True})

    def _job() -> None:
        _news_refreshing[0] = True
        try:
            news_store.fetch_incremental()
        except Exception as e:  # noqa: BLE001 后台任务兜底，不抛给主线程
            logger.warning("news 增量抓取失败: %s", e)
        finally:
            _news_refreshing[0] = False

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({"ok": True, "running": True})


@app.route("/api/news/status")
def news_status():
    return jsonify(news_store.stats())


@app.route("/api/news/deepen", methods=["POST"])
def news_deepen():
    """L3：按需深抓单只股票更久历史（后台线程）。body: {code}。"""
    code = ds.normalize((request.get_json(silent=True) or {}).get("code", ""))
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"ok": False, "msg": "代码无效"}), 400
    threading.Thread(target=news_store.deepen, args=(code,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "code": code})


# ── L5 私域笔记 ────────────────────────────────────────────────────────────
def _csv(v: Any) -> str:
    if isinstance(v, list):
        return ",".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


@app.route("/api/notes")
def notes_list():
    return jsonify({"notes": notes_store.list_notes(
        code=request.args.get("code", ""), sector=request.args.get("sector", ""),
        q=request.args.get("q", ""), limit=_arg_int("limit", 100)),
        "count": notes_store.count()})


@app.route("/api/notes/structure", methods=["POST"])
def notes_structure():
    """AI 结构化草稿（v4-flash）。注意：会把笔记内容发给 DeepSeek 云端。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    content = ((request.get_json(silent=True) or {}).get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "msg": "笔记为空"}), 400
    try:
        draft = llm.structure_note(content)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    return jsonify({"ok": True, "draft": draft})


@app.route("/api/notes", methods=["POST"])
def notes_add():
    body = request.get_json(silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "msg": "笔记为空"}), 400
    nid = notes_store.add(content, codes=_csv(body.get("codes")),
                          sectors=_csv(body.get("sectors")), tags=_csv(body.get("tags")),
                          kind=body.get("kind", ""), ai_summary=body.get("summary", ""))
    return jsonify({"ok": True, "id": nid})


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def notes_delete(note_id: int):
    notes_store.delete(note_id)
    return jsonify({"ok": True})


# ── 交易规则库（蒸馏自 PA_Agent，可增删改；启用中的注入 AI） ─────────────
@app.route("/api/rules")
def rules_list():
    return jsonify({"rules": rules_store.list_rules(category=request.args.get("category", "")),
                    "categories": rules_store.CATEGORIES, "count": rules_store.count(),
                    "scenario": rules_store.get_scenario(),
                    "capital_scenarios": rules_store.CAPITAL_SCENARIOS,
                    "horizon_scenarios": rules_store.HORIZON_SCENARIOS})


@app.route("/api/rules", methods=["POST"])
def rules_add():
    b = request.get_json(silent=True) or {}
    title = (b.get("title") or "").strip()
    content = (b.get("content") or "").strip()
    category = (b.get("category") or "").strip() or "总则纪律"
    if not title or not content:
        return jsonify({"ok": False, "msg": "标题和内容必填"}), 400
    return jsonify({"ok": True, "id": rules_store.add(
        category, title, content, scenarios=(b.get("scenarios") or "").strip())})


@app.route("/api/rules/<int:rule_id>", methods=["PUT"])
def rules_update(rule_id: int):
    b = request.get_json(silent=True) or {}
    fields = {k: b[k] for k in ("category", "title", "content", "enabled", "scenarios") if k in b}
    rules_store.update(rule_id, **fields)
    return jsonify({"ok": True})


@app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def rules_delete(rule_id: int):
    rules_store.delete(rule_id)
    return jsonify({"ok": True})


@app.route("/api/rules/scenario", methods=["POST"])
def rules_scenario():
    """设置当前场景（本金档+周期），影响哪些规则注入 AI。body: {scenario:'小,波段'}。"""
    v = (request.get_json(silent=True) or {}).get("scenario", "")
    rules_store.set_scenario(v)
    return jsonify({"ok": True, "scenario": rules_store.get_scenario()})


# ── 模拟委托交易（多存档，按真实行情+A股规则撮合） ────────────────────────
def _market_open() -> bool:
    now = datetime.now()
    if not news_store.is_trading_day(now.date()):
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t <= (11 * 60 + 30) or (13 * 60) <= t <= (15 * 60)


def _account_summary(acct: dict, quotes: dict) -> dict:
    rows = []
    mv = 0.0
    for p in paper_store.positions_of(acct["id"]):
        q = quotes.get(p["code"], {})
        price = q.get("price") or p["avg_cost"]
        val = price * p["shares"]
        mv += val
        rows.append({**p, "price": price, "value": round(val, 2),
                     "pnl": round((price - p["avg_cost"]) * p["shares"], 2),
                     "pnl_pct": round((price / p["avg_cost"] - 1) * 100, 2) if p["avg_cost"] else 0,
                     "chg_pct": q.get("chg_pct")})
    total = acct["cash"] + mv
    init = acct["init_capital"] or 1
    return {"positions": rows, "market_value": round(mv, 2), "cash": round(acct["cash"], 2),
            "total": round(total, 2), "pnl": round(total - acct["init_capital"], 2),
            "pnl_pct": round((total / init - 1) * 100, 2)}


@app.route("/api/paper/accounts")
def paper_accounts():
    accts = paper_store.list_accounts()
    codes = {p["code"] for a in accts for p in paper_store.positions_of(a["id"])}
    quotes = ds.tencent_quote(list(codes)) if codes else {}
    out = [{**a, **_account_summary(a, quotes)} for a in accts]
    return jsonify({"accounts": out, "market_open": _market_open()})


@app.route("/api/paper/accounts", methods=["POST"])
def paper_account_add():
    b = request.get_json(silent=True) or {}
    try:
        capital = float(b.get("capital", 100000))
    except (TypeError, ValueError):
        capital = 100000.0
    if capital <= 0:
        return jsonify({"ok": False, "msg": "本金须大于 0"}), 400
    return jsonify({"ok": True, "id": paper_store.create_account((b.get("name") or "").strip(), capital)})


@app.route("/api/paper/accounts/<int:aid>", methods=["DELETE"])
def paper_account_del(aid: int):
    paper_store.delete_account(aid)
    return jsonify({"ok": True})


@app.route("/api/paper/account/<int:aid>")
def paper_account_detail(aid: int):
    acct = paper_store.get_account(aid)
    if not acct:
        return jsonify({"ok": False, "msg": "存档不存在"}), 404
    codes = [p["code"] for p in paper_store.positions_of(aid)]
    quotes = ds.tencent_quote(codes) if codes else {}
    return jsonify({"ok": True, "account": {**acct, **_account_summary(acct, quotes)},
                    "orders": paper_store.orders_of(aid), "market_open": _market_open()})


@app.route("/api/paper/order/<int:aid>", methods=["POST"])
def paper_order(aid: int):
    b = request.get_json(silent=True) or {}
    code = ds.normalize(b.get("code", ""))
    side = b.get("side", "buy")
    otype = b.get("otype", "market")
    try:
        shares = int(b.get("shares", 0))
        req_price = float(b.get("price", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "股数/价格须为数字"}), 400
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"ok": False, "msg": "代码无效"}), 400
    q = ds.tencent_quote([code]).get(code, {})
    if not q.get("name"):
        return jsonify({"ok": False, "msg": f"查不到该股票: {code}"}), 404
    res = paper_store.order(aid, code, q["name"], side, otype, req_price, shares, q, _market_open())
    return jsonify(res)


@app.route("/api/detail/<code>")
def detail(code: str):
    """单只深挖：研报 + 龙虎榜 + 解禁 + 资金流 + 财报 + 新闻。"""
    code = ds.normalize(code)
    quotes = ds.tencent_quote([code])
    q = quotes.get(code, {})
    metrics = ds.sina_metrics(code)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {
            "reports": pool.submit(ds.eastmoney_reports, code),
            "lhb": pool.submit(ds.dragon_tiger, code),
            "lock": pool.submit(ds.lockup_expiry, code),
            "fin": pool.submit(ds.financial_summary, code),
            "news": pool.submit(ds.stock_news, code),
        }
        out = {k: f.result() for k, f in futs.items()}
    return jsonify({
        "code": code,
        "quote": q,
        "metrics": metrics,
        "band": ds.scenario_band(q.get("price", 0), metrics.get("vol")),
        "reports": out["reports"],
        "dragon_tiger": out["lhb"],
        "lockup": out["lock"],
        "financials": out["fin"],
        "news": out["news"],
    })


@app.route("/api/kline/<code>")
def kline(code: str):
    """日K线 OHLC（蜡烛图 + 箱形图用）。"""
    return jsonify({"code": ds.normalize(code),
                    "kline": ds.sina_kline(ds.normalize(code), num=120)})


@app.route("/api/wave/<code>")
def wave(code: str):
    """波动多周期：当日分时 + 5日(5分钟) + 日K(~260, 供30/60/当季/当年切片) + 昨收基准。

    一次并发返回全部序列，前端切换周期纯前端切片、不再请求。
    """
    code = ds.normalize(code)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_intra = pool.submit(ds.tencent_minute, code)
        f_min5 = pool.submit(ds.sina_kline, code, 240, 5)     # num=240 ≈ 近5交易日
        f_daily = pool.submit(ds.sina_kline, code, 260, 240)  # num=260 覆盖当年/当季
        f_quote = pool.submit(ds.tencent_quote, [code])
        intraday, min5, daily, quotes = (f_intra.result(), f_min5.result(),
                                         f_daily.result(), f_quote.result())
    q = quotes.get(code, {})
    return jsonify({
        "code": code, "name": q.get("name", code),
        "price": q.get("price"), "prev_close": q.get("last_close"),
        # 流通股本(股) = 流通市值(亿元)×1e8 ÷ 现价，供前端把成交量(股)换算成换手率%
        "float_shares": (q.get("float_mcap_yi") * 1e8 / q.get("price"))
                        if q.get("float_mcap_yi") and q.get("price") else None,
        "intraday": intraday,
        "min5": [{"t": k["date"], "close": k["close"]} for k in min5],
        "daily": [{"date": k["date"], "open": k["open"], "high": k["high"],
                   "low": k["low"], "close": k["close"], "volume": k["volume"]}
                  for k in daily],
        "updated": _now(),
    })


@app.route("/api/minute/<code>")
def minute(code: str):
    """当日分时（轻量端点，供前端分时视图交易时段自动刷新，避免重拉整个 /api/wave）。"""
    code = ds.normalize(code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_intra = pool.submit(ds.tencent_minute, code)
        f_quote = pool.submit(ds.tencent_quote, [code])
        intraday, quotes = f_intra.result(), f_quote.result()
    q = quotes.get(code, {})
    return jsonify({"code": code, "intraday": intraday,
                    "prev_close": q.get("last_close"), "updated": _now()})


@app.route("/api/watchlist")
def watchlist():
    return jsonify({"codes": store.load_watchlist()})


@app.route("/api/watchlist/add", methods=["POST"])
def watchlist_add():
    """新增股票：先校验代码在腾讯能查到名称，再入库。"""
    raw = (request.json or {}).get("code", "")
    code = ds.normalize(raw)
    if not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "msg": f"代码格式无效: {raw}"}), 400
    q = ds.tencent_quote([code])
    if code not in q or not q[code].get("name"):
        return jsonify({"ok": False, "msg": f"查不到该股票: {code}"}), 404
    codes = store.add_code(code)
    return jsonify({"ok": True, "codes": codes, "name": q[code]["name"]})


@app.route("/api/watchlist/remove", methods=["POST"])
def watchlist_remove():
    code = ds.normalize((request.json or {}).get("code", ""))
    return jsonify({"ok": True, "codes": store.remove_code(code)})


# ── 持仓 ──────────────────────────────────────────────────────────────────
@app.route("/api/portfolio")
def portfolio_list():
    """持仓 + 实时盈亏 + 组合汇总。"""
    codes = portfolio.codes()
    quotes = ds.tencent_quote(codes) if codes else {}
    rows = portfolio.with_pnl(quotes)
    return jsonify({"holdings": rows, "summary": portfolio.summary(rows), "updated": _now()})


@app.route("/api/portfolio/add", methods=["POST"])
def portfolio_add():
    """记录一笔买入：{code, shares, cost_price, buy_date?, note?}。

    同代码**追加一笔 lot**（不覆盖）→ 总股数累加、成本加权平均；当日盈亏按笔算基准。
    """
    body = request.json or {}
    code = ds.normalize(body.get("code", ""))
    if not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "msg": f"代码无效: {body.get('code')}"}), 400
    try:
        shares = float(body.get("shares", 0))
        cost = float(body.get("cost_price", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "股数/成本价必须是数字"}), 400
    if shares <= 0 or cost <= 0:
        return jsonify({"ok": False, "msg": "股数与成本价需大于0"}), 400
    q = ds.tencent_quote([code])
    if code not in q or not q[code].get("name"):
        return jsonify({"ok": False, "msg": f"查不到该股票: {code}"}), 404
    portfolio.add(code, shares, cost, body.get("buy_date", ""), body.get("note", ""))
    return jsonify({"ok": True, "name": q[code]["name"]})


@app.route("/api/portfolio/remove", methods=["POST"])
def portfolio_remove():
    code = ds.normalize((request.json or {}).get("code", ""))
    return jsonify({"ok": True, "holdings": portfolio.remove(code)})


# ── DeepSeek 推荐 ─────────────────────────────────────────────────────────
@app.route("/api/recommend/daily", methods=["POST"])
def recommend_daily():
    """每日推荐：把自选股指标 + 持仓喂给 DeepSeek（走 ai_cache 智能命中）。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    watchlist = store.load_watchlist()
    holds_raw = portfolio.load()
    inputs = {"wl": sorted(watchlist), "rules": rules_store.signature(),
              "hold": sorted([[h.get("code", ""), h.get("shares"), h.get("cost_price")]
                              for h in holds_raw], key=lambda x: x[0])}
    if not force:
        hit = ai_cache.get("daily", inputs)
        if hit:
            return jsonify({"ok": True, **hit["result"], "model": hit["model"],
                            "web_search": config.bocha_enabled(), "cached": True,
                            "analyzed_at": hit["ts"], "age_min": hit["age_min"]})
    rows = _overview_rows(watchlist)
    quotes = ds.tencent_quote(portfolio.codes()) if portfolio.codes() else {}
    holdings = portfolio.with_pnl(quotes)
    web_ctx = _tier_block() + _macro_block() + _ai_web_context("market")
    news_map = {c: [n.get("title", "") for n in news_store.query(code=c, days=30, limit=3)]
                for c in watchlist}   # 每股本地库近期标题 → 一句话叙事(0 额外 LLM)
    try:
        result = llm.daily_recommendation(rows, holdings, web_ctx, news_map)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ts = ai_cache.put("daily", inputs, {"result": result}, config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, "result": result, "model": config.DEEPSEEK_MODEL,
                    "web_search": config.bocha_enabled(), "cached": False,
                    "analyzed_at": ts, "age_min": 0})


def _total_assets() -> tuple[float, int]:
    """(总资产, 持仓只数) = active 画像现金 + 全局真实持仓市值（腾讯实时价）。"""
    prof = profile_store.get_active() or {}
    cash = float(prof.get("cash") or 0)
    holds = portfolio.load()
    val = 0.0
    if holds:
        quotes = ds.tencent_quote([h["code"] for h in holds])
        for h in holds:
            price = (quotes.get(h["code"], {}) or {}).get("price") or 0
            val += float(price) * float(h.get("shares") or 0)
    return cash + val, len(holds)


def _tier_block() -> str:
    """当前画像的【本金玩法档】注入块（据总资产落档）。无画像返回空串。"""
    prof = profile_store.get_active()
    if not prof:
        return ""
    total, hn = _total_assets()
    return profile_store.block_for_ai(prof.get("cash") or 0, total, hn)


def _macro_block() -> str:
    """全球宏观/地缘 digest 注入块（据全球快讯 flash 合成「要点+板块指向」，ai_cache kind=macro 当日缓存）。"""
    if not config.llm_enabled():
        return ""
    hit = ai_cache.get("macro", {})
    if hit:
        d = hit["result"].get("digest") or {}
    else:
        news = ds.eastmoney_global_news(30) + ds.cls_telegraph(30)
        markets = ds.global_markets()      # 外围数值：油价/黄金/铜/美股三大指数
        try:
            d = llm.macro_digest(news, markets)
        except llm.LLMError:
            return ""
        ai_cache.put("macro", {}, {"digest": d}, llm.FLASH_MODEL)
    pts = "；".join(d.get("points") or [])
    smap = "；".join(d.get("sector_map") or [])
    if not (pts or smap):
        return ""
    return (f"\n【全球宏观/地缘·对A股板块指向(每日更新)】\n"
            f"要点：{pts}\n板块指向：{smap}\n外围倾向：{d.get('bias', '')}\n")


def _sync_scenario() -> None:
    """按 active 画像的档位自动设 rules 场景的本金维（保留用户选的周期维）。"""
    try:
        total, _ = _total_assets()
        scen_cap = profile_store.tier_of(total)["scen"]
        cur = [t.strip() for t in rules_store.get_scenario().split(",") if t.strip()]
        horizon = [t for t in cur if t in rules_store.HORIZON_SCENARIOS]
        rules_store.set_scenario(",".join([scen_cap] + horizon))
    except Exception:
        logger.debug("同步场景失败", exc_info=True)


def _safe_tier(t: dict) -> dict:
    """inf → None，供前端 JSON.parse（浏览器不接受 Infinity）。"""
    d = dict(t)
    if d.get("hi") == float("inf"):
        d["hi"] = None
    return d


@app.route("/api/profiles")
def profiles_list():
    prof = profile_store.get_active() or {}
    total, hn = _total_assets()
    return jsonify({"profiles": profile_store.list_profiles(), "active_id": prof.get("id"),
                    "active": prof, "total_assets": round(total, 2), "holdings_n": hn,
                    "tier": _safe_tier(profile_store.tier_of(total)),
                    "tiers": [_safe_tier(t) for t in profile_store.TIERS],
                    "risk_prefs": profile_store.RISK_PREFS})


@app.route("/api/profiles", methods=["POST"])
def profiles_create():
    b = request.get_json(silent=True) or {}
    try:
        cash = float(b.get("cash", 0))
    except (TypeError, ValueError):
        cash = 0.0
    pid = profile_store.create(b.get("name", ""), cash, b.get("risk_pref", "均衡"))
    if b.get("activate"):
        profile_store.set_active(pid)
        _sync_scenario()
    return jsonify({"ok": True, "id": pid})


@app.route("/api/profiles/<int:pid>", methods=["PUT"])
def profiles_update(pid: int):
    b = request.get_json(silent=True) or {}
    fields = {k: b[k] for k in ("name", "cash", "risk_pref") if k in b}
    if "cash" in fields:
        try:
            fields["cash"] = float(fields["cash"])
        except (TypeError, ValueError):
            fields.pop("cash")
    profile_store.update(pid, **fields)
    _sync_scenario()
    return jsonify({"ok": True})


@app.route("/api/profiles/<int:pid>", methods=["DELETE"])
def profiles_delete(pid: int):
    profile_store.delete(pid)
    return jsonify({"ok": True})


@app.route("/api/profiles/active/<int:pid>", methods=["POST"])
def profiles_activate(pid: int):
    profile_store.set_active(pid)
    _sync_scenario()
    return jsonify({"ok": True})


def _profile_block(prof: dict | None) -> str:
    """公司叙事拼成注入 AI 主分析的文本块。"""
    if not prof:
        return ""
    tags = "、".join(prof.get("tags") or [])
    return (f"\n【公司叙事·据公开数据】做过：{prof.get('did', '')}；在做：{prof.get('doing', '')}；"
            f"要做：{prof.get('will', '')}" + (f"；题材：{tags}" if tags else "") + "\n")


def _company_profile(code: str, name: str, news: list[dict],
                     financials: list[dict]) -> dict | None:
    """公司叙事(做过/在做/要做+题材)：ai_cache kind=profile 当日复用；失败降级 None。"""
    hit = ai_cache.get("profile", {"code": code})
    if hit:
        return hit["result"].get("profile")
    anns = ds.announcements(code)          # em_get 已串行限流
    concepts = ds.concept_tags(code)
    try:
        prof = llm.company_profile(name, code, news, financials, anns, concepts)
    except llm.LLMError:
        return None
    ai_cache.put("profile", {"code": code}, {"profile": prof}, llm.FLASH_MODEL)
    return prof


@app.route("/api/recommend/position/<code>", methods=["POST"])
def recommend_position(code: str):
    """单只持仓的卖出/加仓/止损建议（结合波动史 + 财报 + 新闻）。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    code = ds.normalize(code)
    holding = next((h for h in portfolio.load() if h.get("code") == code), None)
    if not holding:
        return jsonify({"ok": False, "msg": "该股票不在持仓中"}), 404
    force = bool((request.get_json(silent=True) or {}).get("force"))
    inputs = {"code": code, "shares": holding.get("shares"), "rules": rules_store.signature(),
              "cost": holding.get("cost_price"), "buy_date": holding.get("buy_date", "")}
    if not force:
        hit = ai_cache.get("position", inputs)
        if hit:
            return jsonify({"ok": True, "advice": hit["result"]["advice"],
                            "provenance": hit["result"].get("provenance"),
                            "profile": hit["result"].get("profile"),
                            "financials": [], "news": [], "model": hit["model"],
                            "web_search": config.bocha_enabled(), "cached": True,
                            "analyzed_at": hit["ts"], "age_min": hit["age_min"]})
    quotes = ds.tencent_quote([code])
    q = quotes.get(code, {})
    enriched = [h for h in portfolio.with_pnl(quotes) if h.get("code") == code]
    if enriched:
        holding = enriched[0]
    # 并发拉取：波动指标 / 财报 / 新闻 / K线(算60日高低+振幅)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_metrics = pool.submit(ds.sina_metrics, code)
        f_fin = pool.submit(ds.financial_summary, code)
        f_news = pool.submit(ds.stock_news, code)
        f_kline = pool.submit(ds.sina_kline, code, 60)
        metrics, financials, news, kl = (f_metrics.result(), f_fin.result(),
                                         f_news.result(), f_kline.result())
    vol_hist = _vol_hist(kl)
    prof = _company_profile(code, q.get("name", ""), news, financials)
    web_ctx = _tier_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", ""))
    scen = rules_store.get_scenario()
    rule_map = rules_store.active_rule_map(scen)
    try:
        advice = llm.position_advice(holding, q, metrics, financials, news, vol_hist, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ctx = {"quote": q, "metrics": metrics, "vol_hist": vol_hist, "financials": financials}
    advice["basis"] = provenance.verify_basis(advice.get("basis"), ctx, rule_map)
    prov = provenance.build_provenance(q, metrics, vol_hist, financials, news,
                                       {"count": len(rule_map), "scenario": scen},
                                       None, config.bocha_enabled(), _now())
    ts = ai_cache.put("position", inputs, {"advice": advice, "provenance": prov, "profile": prof},
                      config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, "advice": advice, "provenance": prov, "profile": prof,
                    "financials": financials,
                    "news": news[:5], "web_search": config.bocha_enabled(),
                    "model": config.DEEPSEEK_MODEL, "cached": False,
                    "analyzed_at": ts, "age_min": 0})


@app.route("/api/recommend/entry/<code>", methods=["POST"])
def recommend_entry(code: str):
    """单股深度入场分析（是否/何时/怎么买 + 未来卖出策略），不必持仓。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    code = ds.normalize(code)
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    _act = profile_store.get_active() or {}
    try:
        capital = float(body.get("capital") or _act.get("cash") or 10000)
    except (TypeError, ValueError):
        capital = float(_act.get("cash") or 10000)
    inputs = {"code": code, "capital": capital, "rules": rules_store.signature()}
    if not force:
        hit = ai_cache.get("entry", inputs)
        if hit:
            return jsonify({"ok": True, "advice": hit["result"]["advice"],
                            "provenance": hit["result"].get("provenance"),
                            "profile": hit["result"].get("profile"), "model": hit["model"],
                            "web_search": config.bocha_enabled(), "cached": True,
                            "analyzed_at": hit["ts"], "age_min": hit["age_min"]})
    quotes = ds.tencent_quote([code])
    q = quotes.get(code, {})
    if not q.get("name"):
        return jsonify({"ok": False, "msg": f"查不到该股票: {code}"}), 404
    with ThreadPoolExecutor(max_workers=4) as pool:
        metrics, financials, news, kl = (pool.submit(ds.sina_metrics, code).result(),
                                         pool.submit(ds.financial_summary, code).result(),
                                         pool.submit(ds.stock_news, code).result(),
                                         pool.submit(ds.sina_kline, code, 60).result())
    vol_hist = _vol_hist(kl)
    market_ctx = _market_overview_payload().get("ai")
    prof = _company_profile(code, q.get("name", ""), news, financials)
    web_ctx = _tier_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", ""))
    scen = rules_store.get_scenario()
    rule_map = rules_store.active_rule_map(scen)
    try:
        advice = llm.entry_advice(code, q.get("name", ""), q, metrics, financials, news,
                                  vol_hist, capital, market_ctx, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ctx = {"quote": q, "metrics": metrics, "vol_hist": vol_hist,
           "financials": financials, "market_ctx": market_ctx}
    advice["basis"] = provenance.verify_basis(advice.get("basis"), ctx, rule_map)
    prov = provenance.build_provenance(q, metrics, vol_hist, financials, news,
                                       {"count": len(rule_map), "scenario": scen},
                                       market_ctx, config.bocha_enabled(), _now())
    ts = ai_cache.put("entry", inputs, {"advice": advice, "provenance": prov, "profile": prof},
                      config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, "advice": advice, "provenance": prov, "profile": prof,
                    "model": config.DEEPSEEK_MODEL,
                    "web_search": config.bocha_enabled(), "cached": False,
                    "analyzed_at": ts, "age_min": 0})


@app.route("/api/recommend/screen", methods=["POST"])
def recommend_screen():
    """全市场筛选：跨板块 + 按资金规模。资金默认取 active 投资画像的现金本金（body.capital 可覆盖）。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    _act = profile_store.get_active() or {}
    try:
        capital = float(body.get("capital") or _act.get("cash") or 10000)
    except (TypeError, ValueError):
        capital = float(_act.get("cash") or 10000)
    focus = body.get("focus_sector", "") or ""
    inputs = {"capital": capital, "focus": focus, "rules": rules_store.signature()}
    if not force:
        hit = ai_cache.get("screen", inputs)
        if hit:
            return jsonify({"ok": True, **hit["result"], "capital": capital, "focus": focus,
                            "model": hit["model"], "web_search": config.bocha_enabled(),
                            "cached": True, "analyzed_at": hit["ts"], "age_min": hit["age_min"]})
    rows = _screen_rows(capital, focus)
    if not rows:
        return jsonify({"ok": False, "msg": "候选池行情拉取失败，请重试"}), 502
    market_ctx = _market_overview_payload().get("ai")  # 复用缓存的大盘研判结论
    web_ctx = _tier_block() + _macro_block() + _ai_web_context("market")
    try:
        result = llm.market_screen(rows, capital, focus, market_ctx, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    blob = {"result": result, "candidates": len(rows),
            "market_regime": (market_ctx or {}).get("regime")}
    ts = ai_cache.put("screen", inputs, blob, config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, **blob, "capital": capital, "focus": focus,
                    "web_search": config.bocha_enabled(), "model": config.DEEPSEEK_MODEL,
                    "cached": False, "analyzed_at": ts, "age_min": 0})


def _vol_hist(kl: list[dict]) -> dict:
    """从日K算 60日高低/当前 + 近20日日振幅均值(%)。"""
    if not kl:
        return {}
    closes = [k["close"] for k in kl]
    recent = kl[-20:]
    atr = [((k["high"] - k["low"]) / k["close"] * 100) for k in recent if k["close"]]
    return {
        "hi": round(max(k["high"] for k in kl), 2),
        "lo": round(min(k["low"] for k in kl), 2),
        "cur": closes[-1],
        "atr_pct": round(sum(atr) / len(atr), 1) if atr else None,
    }


_SCREEN_CAP_TOTAL = 36  # 喂给 LLM 的候选总量上限（控 token 与时延）


def _balanced_pick(codes: list[str], cap_total: int, cap_per_sub: int) -> list[str]:
    """跨一级板块均衡采样：按一级轮询取，每个二级细分最多 cap_per_sub 只，总量 ≤ cap_total。"""
    by_primary: dict[str, list[str]] = {}
    for c in codes:
        primary, _ = universe.sector_of(c)
        by_primary.setdefault(primary, []).append(c)
    queues = list(by_primary.values())
    cursor = [0] * len(queues)
    per_sub: dict[str, int] = {}
    picked: list[str] = []
    progressed = True
    while len(picked) < cap_total and progressed:
        progressed = False
        for qi, q in enumerate(queues):
            while cursor[qi] < len(q):
                c = q[cursor[qi]]
                cursor[qi] += 1
                _, sub = universe.sector_of(c)
                if per_sub.get(sub, 0) < cap_per_sub:
                    per_sub[sub] = per_sub.get(sub, 0) + 1
                    picked.append(c)
                    progressed = True
                    break  # 取一只后轮到下一个一级板块
            if len(picked) >= cap_total:
                break
    return picked


def _screen_rows(capital: float, focus: str = "") -> list[dict]:
    """候选池行情 + 指标（按 focus 取数 + 负担得起优先 + 跨板块均衡采样）。"""
    codes = universe.codes_of(focus)
    quotes = ds.tencent_quote(codes)
    if not quotes:
        return []
    # 先按 1 手成本可负担过滤（资金太小买不起任何 1 手则退回全池给参考）
    affordable = [c for c in codes if quotes.get(c, {}).get("lot_cost", 9e9) <= capital]
    pool = affordable or codes
    # 侧重某个二级细分时放宽单细分配额（否则跨一级/全市场按 3 只/细分均衡）
    subs_all = {s for subs in universe.taxonomy().values() for s in subs}
    cap_per_sub = 6 if focus in subs_all else 3
    picked = _balanced_pick(pool, _SCREEN_CAP_TOTAL, cap_per_sub)
    with ThreadPoolExecutor(max_workers=8) as executor:
        metrics_list = list(executor.map(ds.sina_metrics, picked))
    rows = []
    for c, m in zip(picked, metrics_list):
        q = quotes.get(c, {})
        primary, sub = universe.sector_of(c)
        rows.append({"code": c, "name": q.get("name", c),
                     "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"),
                     "range_pos": m.get("range_pos"), "net20": m.get("net20"),
                     "lot_cost": q.get("lot_cost")})
    return rows


def _ai_web_context(scope: str, code: str = "", name: str = "") -> str:
    """构建喂给 AI 的「联网知识」上下文：交易规则 + L2 本地资讯库 + A 实时快讯 + B 博查(可选)。"""
    parts = []
    # 交易分析框架规则（启用中的规则库，蒸馏自 PA_Agent）——置顶，AI 按此推理
    rules_txt = rules_store.for_ai()
    if rules_txt:
        parts.append("【交易分析框架规则（价格行为体系，分析时严格按此推理）】\n" + rules_txt)
    # L4：本地新闻库（带日期，让 AI 按新鲜度加权；个股取该股近期，大盘取市场级+政策）
    if scope == "position" and code:
        local = news_store.query(code=code, days=120, limit=8)
        if len(local) < 5:  # L3：本地稀疏 → 按需深抓更久历史再取
            try:
                news_store.deepen(code)
            except Exception as e:  # noqa: BLE001 兜底，不阻断 AI
                logger.warning("news 深抓 %s 失败: %s", code, e)
            local = news_store.query(code=code, days=365, limit=10)
    else:
        local = (news_store.query(sector="市场", days=30, limit=8)
                 + news_store.query(kind="政策", days=60, limit=6))
    if local:
        seen: set[str] = set()
        lines = []
        for n in local:
            t = n.get("title", "")
            if not t or t in seen:
                continue
            seen.add(t)
            lines.append(f"- {n.get('date','')} [{n.get('kind','')}] {t}")
        if lines:
            parts.append("本地资讯库（近期新闻/政策，越近权重越高；仅供判断勿编造）：\n"
                         + "\n".join(lines[:12]))
    # L5：私域笔记（我本人的判断，带时间戳供按新鲜度加权；须与客观数据区分、勿当事实）
    if scope == "position" and code:
        p, s = universe.sector_of(code)
        my_notes = notes_store.for_ai(code=code, sectors=[p, s], limit=5)
    else:
        my_notes = notes_store.list_notes(limit=5)
    if my_notes:
        nlines = [f"- {n.get('created_at','')[:10]} [{n.get('kind','')}] "
                  f"{n.get('ai_summary') or (n.get('content','') or '')[:60]}" for n in my_notes]
        parts.append("【我的私域笔记（我本人的判断，仅供参考、需与客观数据区分，勿当事实）】\n"
                     + "\n".join(nlines))
    # A：实时快讯
    news = ds.market_news_digest(12)
    if news:
        parts.append("最新财经/政策快讯：\n"
                     + "\n".join(f"- {n['time']} {n['title']}" for n in news))
    if config.bocha_enabled():
        if scope == "position" and name:
            query = f"{name} {code} 最新消息 政策 业绩 利好 利空"
        else:
            query = "A股 科技板块 半导体 AI 芯片 最新政策 行业动态"
        dig = websearch.search_digest(query, count=6)
        if dig:
            parts.append("联网搜索（博查）：\n" + dig)
    return "\n\n".join(parts)


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    if news_store.stats()["total"] == 0:  # 首次运行：后台一次性回填新闻库(不阻塞启动)
        threading.Thread(target=news_store.backfill, daemon=True).start()
        logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
