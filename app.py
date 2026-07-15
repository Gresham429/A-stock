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
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import agent_loop
import agent_store
import ai_cache
import config
import datasources as ds
import factor_lab
import fees
import llm
import news_store
import notes_store
import paper_store
import portfolio
import profile_store
import provenance
import rules_store
import store
import template_store
import universe
import universe_store
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
agent_store.init()  # agent 配置/日志/教训/条件单（表是增量加的，靠 IF NOT EXISTS 自愈）
template_store.init()  # 提示词模板版本化
factor_lab.init()  # 因子 IC 回测
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
                    "taxonomy": universe_store.taxonomy()})


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
            ai = llm.market_overview(idx["indices"], breadth, _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _ai_web_context("market"))
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
    """清仓。body 带 sell_price 则按「卖出净收入」加回画像现金；不带则只删记录、不动现金
    （用于「记错了想删掉」）。sell_price='market' 用当前市价。
    """
    b = request.json or {}
    code = ds.normalize(b.get("code", ""))
    raw = b.get("sell_price")
    price: float | None = None
    if raw == "market":
        price = (ds.tencent_quote([code]).get(code, {}) or {}).get("price") or None
    elif raw not in (None, ""):
        try:
            price = float(raw)
        except (TypeError, ValueError):
            price = None
    return jsonify({"ok": True, "holdings": portfolio.remove(code, price),
                    "sold_at": price})


@app.route("/api/portfolio/reconcile")
def portfolio_reconcile_check():
    """对账预览：历史持仓是在「买入不扣现金」的旧逻辑下记的，现金偏高多少。只算不改。"""
    return jsonify(portfolio.cash_reconcile())


@app.route("/api/portfolio/reconcile", methods=["POST"])
def portfolio_reconcile_apply():
    """执行对账：把画像现金减去未扣减的持仓成本。涉及资金，需显式调用。"""
    r = portfolio.cash_reconcile()
    if not r.get("ok") or not r.get("delta"):
        return jsonify({**r, "applied": False})
    profile_store.update(r["pid"], cash=r["cash_should_be"])
    logger.info("画像 %s 对账: 现金 %.2f → %.2f", r["profile"], r["cash_now"], r["cash_should_be"])
    return jsonify({**r, "applied": True})


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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _ai_web_context("market")
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


def _record_basis_stats(basis: list[dict] | None) -> None:
    """把引用校验结果（✓/⚠）计入当前 system prompt 版本的统计。

    这是**唯一可信的提示词优化信号**：每次调用一个样本、后端权威校验(AI 编不了)、
    即时反馈、不受市场影响。收益率则相反——样本太少且被大盘 beta 污染，故不统计。
    """
    if not basis:
        return
    ok = sum(1 for b in basis for r in (b.get("refs") or []) if r.get("status") == "ok")
    bad = sum(1 for b in basis for r in (b.get("refs") or []) if r.get("status") == "bad")
    if ok or bad:
        template_store.record("system_disclaimer", llm._system_prompt()[1],
                              basis_ok=ok, basis_bad=bad)


def _lesson_block() -> str:
    """【历史教训】注入块：把模拟盘上 agent 犯过的**真实错误**喂给 5 个已有 AI（四期闭环）。

    喂的是**事实统计**（「追高 4 次」），不是让 AI 改写提示词——与「用 5 个样本优化提示词」
    有本质区别：这里数事实，那里拟合参数。故不受 784 笔样本量死局限制。
    """
    try:
        txt = agent_store.for_ai()
        return txt + "\n\n" if txt else ""
    except (sqlite3.Error, OSError) as e:
        logger.warning("教训块生成失败: %s", e)
        return ""


def _fee_block() -> str:
    """【交易成本】注入块：给 AI 具体费率与保本涨幅，而非「注意手续费」这种空话。

    此前 5 个 AI 提示词 0 处提及手续费——AI 在给买卖建议时并不知道交易要花钱，
    会建议博取小于保本涨幅的价差（对万9费率，往返 0.232%，即涨不到 0.232% 就是亏）。
    费率取 active 画像（因券商/账户而异，见 fees.py）。
    """
    try:
        sched = profile_store.fee_schedule()
        prof = profile_store.get_active()
        capital = float((prof or {}).get("cash") or 0) or 10000.0
        return fees.for_ai(sched, capital) + "\n\n"
    except (sqlite3.Error, OSError, ValueError) as e:
        logger.warning("交易成本块生成失败: %s", e)
        return ""


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
    # 费率（因券商/账户而异，必须可改；佣金率填小数，如 万9 = 0.0009）
    for k in ("commission_rate", "min_commission", "stamp_rate", "transfer_rate"):
        if k in b:
            try:
                v = float(b[k])
            except (TypeError, ValueError):
                continue
            if 0 <= v <= (100.0 if k == "min_commission" else 0.01):  # 挡住把 9 当成万9 填进来
                fields[k] = v
            else:
                logger.warning("画像 %s 费率 %s=%s 超出合理范围，忽略", pid, k, b[k])
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", ""))
    scen = rules_store.get_scenario()
    rule_map = rules_store.active_rule_map(scen)
    try:
        advice = llm.position_advice(holding, q, metrics, financials, news, vol_hist, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ctx = {"quote": q, "metrics": metrics, "vol_hist": vol_hist, "financials": financials}
    advice["basis"] = provenance.verify_basis(advice.get("basis"), ctx, rule_map)
    _record_basis_stats(advice["basis"])
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", ""))
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
    _record_basis_stats(advice["basis"])
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _ai_web_context("market")
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
_PRESCREEN = 600        # 全市场未指定板块时，均衡采样前先按流通市值预筛到这么多只
VOL_FLOOR = 15.0        # 波动率下限：低于此没有波段空间（用户偏好，非收益预测）
VOL_CEIL = 130.0        # 波动率上限：高于此风险失控


_PA_RANK_MAX = 200   # 板块内 ≤ 该只数时，对全部成分股算形态再排序（超过则退回市值预筛）
_METRIC_TTL = 900    # 形态指标进程内缓存秒数（同板块反复选股不重复取数）
_metric_cache: dict[str, tuple[float, dict]] = {}
_EMPTY_METRICS = {"vol": None, "range_pos": None, "cum20": None,
                  "net5": None, "net20": None, "series": []}


def _safe_metrics(code: str) -> dict:
    """sina_metrics 的兜底包装 + 进程内 TTL 缓存：单只异常不拖垮整批。

    全市场池数据质量参差（0 收盘价、空字段、退市残留），executor.map 里任一只抛异常
    都会让整个选股 500。指标缺失退化为 None，AI 侧本就按缺失处理。
    """
    hit = _metric_cache.get(code)
    if hit and time.time() - hit[0] < _METRIC_TTL:
        return hit[1]
    try:
        m = ds.sina_metrics(code)
    except Exception as e:  # noqa: BLE001 兜底：宁可该股无指标，不可整批失败
        logger.warning("指标计算失败 %s（跳过该股指标）: %s", code, e)
        return dict(_EMPTY_METRICS)
    _metric_cache[code] = (time.time(), m)
    return m


def _pa_score(m: dict) -> float | None:
    """形态初筛打分（0–100）。None = 形态不可分析，该股出局。

    这是**粗筛**，只决定谁值得占用送进 AI 的 36 个名额；真正的 PA 判断由 AI 依
    rules_store 的规则库做。

    **方向由 162,014 个样本的 IC 回测驱动，不再是我拍的先验**（见 factor_lab）：
      · 每个因子的方向取 `factor_lab.direction()` —— 近 60 日 |t|>2 用近期方向
        （regime 已切换），否则用全样本方向，两者都不显著则该因子**不参与打分**。
      · 权重保持等权（每个生效因子等分）——量化实证里过度优化的权重样本外
        常打不过等权，且频繁重拟合会让噪音驱动参数、在 regime 间来回甩。
      · 实测：全样本三因子皆反向(cum20 t=-8.16 反转效应)，但近 60 日全部符号反转
        (vol t=+6.96)，A股 2026 年从反转切向动量。静态权重会持续押错方向。

    **波动率的双重身份**：它既是收益预测因子（低波动异象），又是用户的风险偏好
    （要波动型科技股才有波段空间）。二者混淆会打架，故拆开——
      打分：按 IC 方向（预测）；过滤：VOL_FLOOR/CEIL 硬门（偏好与风控）。

    资金分量(net20)因 `sina_metrics` 只给 30 天历史，**无法回测方向**，故不打分、
    仅作展示。
    """
    vol = m.get("vol")
    if vol is None:  # 形态算不出来 -> 不进候选（次新/停牌/数据缺口）
        return None
    if not (VOL_FLOOR <= vol <= VOL_CEIL):  # 偏好+风控硬门，与预测无关
        return None
    dirs = factor_lab.directions()
    live = [f for f in ("vol", "cum20", "range_pos")
            if dirs.get(f, {}).get("sign", 0) != 0 and m.get(f) is not None]
    if not live:  # 没有任何因子方向可信 -> 全体中性，交给 AI 判断
        return 50.0
    per = 100.0 / len(live)
    score = 0.0
    for f in live:
        sign = dirs[f]["sign"]
        pct = _factor_pct(f, m[f])          # 该值在历史分布中的位置 0..1
        score += per * (pct if sign > 0 else (1.0 - pct))
    return round(score, 1)


# 因子取值的经验分位锚点（把原始值映射到 0..1，避免量纲差异主导打分）。
# 取自 299 只 × 600 日样本的实际分布，非拍脑袋。
_FACTOR_RANGE = {"vol": (15.0, 110.0), "cum20": (-25.0, 35.0), "range_pos": (0.0, 100.0)}


def _factor_pct(f: str, v: float) -> float:
    lo, hi = _FACTOR_RANGE[f]
    return min(max((v - lo) / (hi - lo), 0.0), 1.0) if hi > lo else 0.5


def _screen_rows(capital: float, focus: str = "") -> list[dict]:
    """候选池行情 + 指标（按 focus 取数 + 负担得起优先 + 跨板块均衡采样）。

    focus 为板块名（一级/细分/概念）时只在该板块内选；为空则全市场。
    候选池来自 universe_store（全A ~4989 只 eligible），未回填时自动降级手工池。
    """
    codes = universe_store.codes_of(focus)
    quotes = ds.tencent_quote(codes)  # 自动分批：全池 4989 只 ≈1.7s
    if not quotes:
        return []
    # 先按 1 手成本可负担过滤（资金太小买不起任何 1 手则退回全池给参考）
    affordable = [c for c in codes if quotes.get(c, {}).get("lot_cost", 9e9) <= capital]
    pool = affordable or codes
    smap = universe_store.sectors_map(pool)  # 批量查板块，避免逐只 DB 往返
    subs_all = {s for subs in universe_store.taxonomy().values() for s in subs}
    # 池子够小（关注某板块，主路径）-> 对全部成分股算形态再按分排序，形态真正参与筛选。
    # 池子过大（全市场）-> 退回流通市值预筛 + 均衡采样：均衡采样按池内顺序取，
    # 5000 只不预筛会取到各板块代码号最小的股而非龙头，扩池反成选垃圾。
    if focus and len(pool) <= _PA_RANK_MAX:
        metrics = _metrics_of(pool)
        scored = [(c, metrics[c], _pa_score(metrics[c])) for c in pool]
        keep = [(c, m, s) for c, m, s in scored if s is not None]
        keep.sort(key=lambda x: x[2], reverse=True)
        chosen = keep[:_SCREEN_CAP_TOTAL]
        logger.info("形态初筛 focus=%s: 池 %d -> 可分析 %d -> 取前 %d",
                    focus, len(pool), len(keep), len(chosen))
        picked = [c for c, _, _ in chosen]
        mmap = {c: m for c, m, _ in chosen}
        score_map = {c: s for c, _, s in chosen}
    else:
        if not focus and len(pool) > _PRESCREEN:
            pool = sorted(pool, key=lambda c: quotes.get(c, {}).get("float_mcap_yi", 0),
                          reverse=True)[:_PRESCREEN]
            smap = universe_store.sectors_map(pool)
        cap_per_sub = 6 if focus and focus in subs_all else 3
        picked = _balanced_pick(pool, _SCREEN_CAP_TOTAL, cap_per_sub, smap)
        mmap = _metrics_of(picked)
        score_map = {c: _pa_score(mmap[c]) for c in picked}
    rows = []
    for c in picked:
        q, m = quotes.get(c, {}), mmap.get(c, _EMPTY_METRICS)
        primary, sub = smap.get(c) or universe_store.sector_of(c)
        rows.append({"code": c, "name": q.get("name", c),
                     "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"),
                     "range_pos": m.get("range_pos"), "net20": m.get("net20"),
                     "pa_score": score_map.get(c),
                     "turnover": q.get("turnover"),
                     "lot_cost": q.get("lot_cost")})
    return rows


def _metrics_of(codes: list[str]) -> dict[str, dict]:
    """并发拉一批股票的形态指标（带进程内 TTL 缓存）。"""
    if not codes:
        return {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        return dict(zip(codes, executor.map(_safe_metrics, codes)))


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
        p, s = universe_store.sector_of(code)
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


# ── 全市场股票池 + 板块日变化（universe_store） ────────────────────────────
@app.route("/api/universe/status")
def api_universe_status():
    """池子健康度：总数/eligible/板块回填进度/板块数/最近刷新。"""
    return jsonify(universe_store.status())


@app.route("/api/universe/refresh", methods=["POST"])
def api_universe_refresh():
    """刷新全A名单（新浪 hs_a，~12s）+ 后台续跑板块回填（断点续传）。"""
    n = universe_store.refresh_roster()
    threading.Thread(target=universe_store.backfill_sectors, daemon=True).start()
    return jsonify({"refreshed": n, **universe_store.status()})


@app.route("/api/sectors")
def api_sectors():
    """板块日排行。?kind=sw1|sub|concept（默认 sw1）&date=&limit=

    数据来自 universe_store.sector_daily；无当日数据时惰性补算一次（~6s）。
    """
    kind = request.args.get("kind", "sw1")
    date = request.args.get("date", "")
    limit = max(1, min(int(request.args.get("limit", 30)), 100))
    rows = universe_store.sector_ranking(date=date, kind=kind, limit=limit)
    if not rows and not date:  # 当日尚未统计过 -> 惰性补算
        universe_store.snapshot_daily()
        rows = universe_store.sector_ranking(kind=kind, limit=limit)
    return jsonify({"date": rows[0]["date"] if rows else "", "kind": kind,
                    "rows": rows, "status": universe_store.status()})


@app.route("/api/sectors/snapshot", methods=["POST"])
def api_sectors_snapshot():
    """手动重算当日板块统计（~6s）。"""
    return jsonify({"sectors": universe_store.snapshot_daily()})


@app.route("/api/sectors/<name>")
def api_sector_detail(name: str):
    """单板块：近 N 日变化历史 + 成分股行情（按流通市值降序，上限 40）。"""
    days = max(2, min(int(request.args.get("days", 30)), 250))
    codes = universe_store.codes_of(name)
    quotes = ds.tencent_quote(codes[:40]) if codes else {}
    members = [{"code": c, "name": quotes.get(c, {}).get("name", c),
                "price": quotes.get(c, {}).get("price"),
                "chg_pct": quotes.get(c, {}).get("chg_pct"),
                "turnover": quotes.get(c, {}).get("turnover"),
                "lot_cost": quotes.get(c, {}).get("lot_cost")}
               for c in codes[:40] if c in quotes]
    members.sort(key=lambda m: m.get("chg_pct") or -99, reverse=True)
    return jsonify({"sector": name, "total": len(codes),
                    "history": universe_store.sector_history(name, days),
                    "members": members})


@app.route("/api/factors")
def api_factors():
    """因子回测结果：IC 均值/t值/胜率 + 当前方向 + 失效报警 + 容量。"""
    return jsonify({"summary": factor_lab.summary(),
                    "directions": factor_lab.directions(),
                    "alerts": factor_lab.decay_alert(),
                    "status": factor_lab.status()})


@app.route("/api/factors/rolling")
def api_factors_rolling():
    """滚动 IC 曲线（因子衰减看得见）。?factor=cum20&horizon=10&window=60"""
    return jsonify({"points": factor_lab.rolling_ic(
        request.args.get("factor", "cum20"), _arg_int("horizon", 10),
        _arg_int("window", 60))})


@app.route("/api/factors/backtest", methods=["POST"])
def api_factors_backtest():
    """重跑因子回测（299 只 × 600 日 ≈ 14s）。后台线程。"""
    n = int((request.get_json(silent=True) or {}).get("stocks") or 300)
    threading.Thread(target=factor_lab.backtest, args=(n,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "stocks": n})


@app.route("/api/templates")
def api_templates():
    """提示词模板：版本列表 + 近 N 日客观指标（引用有效率/schema 失败率）+ 容量。"""
    return jsonify({"versions": template_store.list_versions(request.args.get("name", "")),
                    "stats": template_store.stats(request.args.get("name", ""),
                                                  _arg_int("days", 30)),
                    "status": template_store.status()})


@app.route("/api/templates/activate", methods=["POST"])
def api_templates_activate():
    """切换 active 版本（回滚 = 切回旧版本号）。body: {name, version}"""
    b = request.get_json(silent=True) or {}
    ok = template_store.activate(b.get("name", ""), int(b.get("version") or 0))
    return jsonify({"ok": ok})


@app.route("/api/templates", methods=["POST"])
def api_templates_add():
    """新增版本。body: {name, body, note, activate}"""
    b = request.get_json(silent=True) or {}
    if not (b.get("name") and b.get("body")):
        return jsonify({"ok": False, "msg": "name/body 必填"}), 400
    ver = template_store.add_version(b["name"], b["body"], b.get("note", ""),
                                     bool(b.get("activate")))
    return jsonify({"ok": True, "version": ver})


# ── Agent 模拟交易（agent_store + agent_loop） ─────────────────────────────
@app.route("/api/agents")
def api_agents():
    """agent 列表 + 教训汇总 + 容量。"""
    return jsonify({"agents": agent_store.list_agents(),
                    "lessons": agent_store.lesson_rollup(12),
                    "status": agent_store.status()})


@app.route("/api/agents", methods=["POST"])
def api_agents_create():
    """建 agent：自动建配套模拟盘账户。body: {name, capital, profile_id, decider}"""
    b = request.get_json(silent=True) or {}
    name = (b.get("name") or "agent").strip()
    try:
        capital = float(b.get("capital") or 10000)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "capital 无效"}), 400
    prof = b.get("profile_id") or (profile_store.get_active() or {}).get("id")
    if not prof:
        return jsonify({"ok": False, "msg": "无可用投资画像"}), 400
    aid = paper_store.create_account(f"[agent]{name}", capital)
    gid = agent_store.create_agent(name, aid, int(prof),
                                   b.get("decider", "single"), b.get("note", ""))
    return jsonify({"ok": True, "agent_id": gid, "account_id": aid})


@app.route("/api/agents/<int:gid>", methods=["DELETE"])
def api_agents_delete(gid: int):
    ag = agent_store.get_agent(gid)
    if ag:
        paper_store.delete_account(ag["account_id"])
        agent_store.delete_agent(gid)
    return jsonify({"ok": True})


@app.route("/api/agents/<int:gid>/run", methods=["POST"])
def api_agents_run(gid: int):
    """跑一个 agent 的日循环。?dry=1 只决策不下单。"""
    b = request.get_json(silent=True) or {}
    blocks = _tier_block() + _fee_block() + _lesson_block() + rules_store.for_ai()
    return jsonify(agent_loop.run_day(gid, focus=b.get("focus", ""),
                                      dry_run=bool(b.get("dry")), blocks=blocks,
                                      force=bool(b.get("force"))))


@app.route("/api/agents/run_all", methods=["POST"])
def api_agents_run_all():
    """跑所有启用的 agent（多档位/同档多账户并行实验）。后台线程，不阻塞。"""
    b = request.get_json(silent=True) or {}
    blocks = _tier_block() + _fee_block() + _lesson_block() + rules_store.for_ai()
    dry = bool(b.get("dry"))
    threading.Thread(target=agent_loop.run_all, args=(dry, blocks), daemon=True).start()
    return jsonify({"ok": True, "running": True,
                    "agents": len(agent_store.list_agents(active_only=True))})


@app.route("/api/agents/<int:gid>/runs")
def api_agents_runs(gid: int):
    """某 agent 的日循环日志 + 净值曲线 + 教训。"""
    ag = agent_store.get_agent(gid) or {}
    return jsonify({"runs": agent_store.runs_of(gid, request.args.get("date", ""),
                                                _arg_int("limit", 40)),
                    "orders": paper_store.orders_of(ag.get("account_id", 0), 60),
                    "positions": paper_store.positions_of(ag.get("account_id", 0)),
                    "conditions": agent_store.conditions_of(gid),
                    "equity": agent_store.equity_of(gid, _arg_int("days", 90)),
                    "lessons": agent_store.lessons(gid, 20),
                    "account": paper_store.get_account(
                        (agent_store.get_agent(gid) or {}).get("account_id", 0))})


def _agent_boot() -> None:
    """启动时跑 agent 日循环（用户要求「只在我每天打开 app.py 后进行持盘操作」）。

    双重门控：`run_all` 内部有非交易日门 + 每 agent 幂等门（今日跑过就跳过）——
    一天开三次 app 只会跑一次。
    """
    if not agent_store.list_agents(active_only=True):
        return
    blocks = _tier_block() + _fee_block() + _lesson_block() + rules_store.for_ai()
    for r in agent_loop.run_all(blocks=blocks):
        if r.get("skipped"):
            logger.info("agent %s: %s", r.get("agent", "-"), r["skipped"])
        elif r.get("ok"):
            logger.info("agent %s 日循环: 成交 %d / 教训 %d", r.get("agent"),
                        len(r.get("filled") or []), len(r.get("lessons") or []))


def _universe_boot() -> None:
    """启动时后台预热：名单刷新 + 板块回填续跑 + 当日统计。不阻塞启动。"""
    try:
        universe_store.init()
        st = universe_store.status()
        if not st.get("ready") or st.get("roster_at", "")[:10] != _now()[:10]:
            universe_store.refresh_roster()
        if universe_store.status().get("sectors_pending", 0) > 0:
            logger.info("板块归属回填续跑…（断点续传，期间 sector_of 降级手工池）")
            universe_store.backfill_sectors()
        universe_store.snapshot_daily()
        template_store.init()
        template_store.purge()   # 按日累积的表一律配清理
        agent_store.init()
        agent_store.purge()
        factor_lab.init()
        factor_lab.purge()
        _agent_boot()
    except (OSError, ValueError, sqlite3.Error) as e:
        logger.warning("全市场池预热失败（不影响其余功能）: %s", e)


if __name__ == "__main__":
    if news_store.stats()["total"] == 0:  # 首次运行：后台一次性回填新闻库(不阻塞启动)
        threading.Thread(target=news_store.backfill, daemon=True).start()
        logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
    threading.Thread(target=_universe_boot, daemon=True).start()
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
