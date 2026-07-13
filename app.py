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
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request

import ai_cache
import config
import datasources as ds
import llm
import portfolio
import store
import universe
import websearch

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)


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
    if not force:
        hit = ai_cache.get("market", {})
        if hit:
            return {**hit["result"], "cached": True,
                    "analyzed_at": hit["ts"], "age_min": hit["age_min"]}
    idx = ds.index_quotes()
    breadth = ds.market_breadth()
    ai = None
    if config.llm_enabled() and idx.get("indices"):
        try:
            ai = llm.market_overview(idx["indices"], breadth, _ai_web_context("market"))
        except llm.LLMError as e:
            logger.warning("大盘研判 AI 失败: %s", e)
    model = config.DEEPSEEK_MODEL if config.llm_enabled() else None
    core = {"ok": True, "indices": idx.get("indices", []),
            "amount_liang_yi": idx.get("amount_liang_yi"),
            "breadth": breadth, "ai": ai, "model": model, "updated": _now()}
    ts = ai_cache.put("market", {}, core, model or "")
    return {**core, "cached": False, "analyzed_at": ts, "age_min": 0}


@app.route("/api/market/overview")
def market_overview_api():
    """大盘研判：五大指数 + 两市成交额 + 市场情绪 + AI 局势分析。

    ?ai=0 只返回指数(快)；?refresh=1 强制重算 AI 研判。
    """
    return jsonify(_market_overview_payload(
        force=request.args.get("refresh") == "1",
        with_ai=request.args.get("ai") != "0"))


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
        "intraday": intraday,
        "min5": [{"t": k["date"], "close": k["close"]} for k in min5],
        "daily": [{"date": k["date"], "close": k["close"]} for k in daily],
        "updated": _now(),
    })


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
    """记录持仓：{code, shares, cost_price, buy_date?, note?}。"""
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
    inputs = {"wl": sorted(watchlist),
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
    web_ctx = _ai_web_context("market")
    try:
        result = llm.daily_recommendation(rows, holdings, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ts = ai_cache.put("daily", inputs, {"result": result}, config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, "result": result, "model": config.DEEPSEEK_MODEL,
                    "web_search": config.bocha_enabled(), "cached": False,
                    "analyzed_at": ts, "age_min": 0})


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
    inputs = {"code": code, "shares": holding.get("shares"),
              "cost": holding.get("cost_price"), "buy_date": holding.get("buy_date", "")}
    if not force:
        hit = ai_cache.get("position", inputs)
        if hit:
            return jsonify({"ok": True, "advice": hit["result"]["advice"],
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
    web_ctx = _ai_web_context("position", code, q.get("name", ""))
    try:
        advice = llm.position_advice(holding, q, metrics, financials, news, vol_hist, web_ctx)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    ts = ai_cache.put("position", inputs, {"advice": advice}, config.DEEPSEEK_MODEL)
    return jsonify({"ok": True, "advice": advice, "financials": financials,
                    "news": news[:5], "web_search": config.bocha_enabled(),
                    "model": config.DEEPSEEK_MODEL, "cached": False,
                    "analyzed_at": ts, "age_min": 0})


@app.route("/api/recommend/screen", methods=["POST"])
def recommend_screen():
    """全市场科技股筛选：跨板块 + 按资金规模。body: {capital?, focus_sector?}。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    try:
        capital = float(body.get("capital", 10000))
    except (TypeError, ValueError):
        capital = 10000.0
    focus = body.get("focus_sector", "") or ""
    inputs = {"capital": capital, "focus": focus}
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
    web_ctx = _ai_web_context("market")
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
    """构建喂给 AI 的「联网知识」上下文：A=免费财经/政策快讯，B=博查联网搜索(可选)。"""
    parts = []
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
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
