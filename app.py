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

import config
import datasources as ds
import llm
import portfolio
import store

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
                    "model": config.DEEPSEEK_MODEL if config.llm_enabled() else None})


@app.route("/api/overview")
def overview():
    """自选股全量对比：一次腾讯批量行情 + 并发拉各自波动/资金指标。"""
    return jsonify({"rows": _overview_rows(store.load_watchlist()), "updated": _now()})


@app.route("/api/detail/<code>")
def detail(code: str):
    """单只深挖：研报 + 龙虎榜 + 解禁 + 资金流序列。"""
    code = ds.normalize(code)
    quotes = ds.tencent_quote([code])
    q = quotes.get(code, {})
    metrics = ds.sina_metrics(code)
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_reports = pool.submit(ds.eastmoney_reports, code)
        fut_lhb = pool.submit(ds.dragon_tiger, code)
        fut_lock = pool.submit(ds.lockup_expiry, code)
        reports = fut_reports.result()
        lhb = fut_lhb.result()
        lockup = fut_lock.result()
    return jsonify({
        "code": code,
        "quote": q,
        "metrics": metrics,
        "band": ds.scenario_band(q.get("price", 0), metrics.get("vol")),
        "reports": reports,
        "dragon_tiger": lhb,
        "lockup": lockup,
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
    """每日推荐：把自选股指标 + 持仓喂给 DeepSeek。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    rows = _overview_rows(store.load_watchlist())
    quotes = ds.tencent_quote(portfolio.codes()) if portfolio.codes() else {}
    holdings = portfolio.with_pnl(quotes)
    try:
        result = llm.daily_recommendation(rows, holdings)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    return jsonify({"ok": True, "result": result, "model": config.DEEPSEEK_MODEL,
                    "updated": _now()})


@app.route("/api/recommend/position/<code>", methods=["POST"])
def recommend_position(code: str):
    """单只持仓的卖出/加仓/止损建议。"""
    if not config.llm_enabled():
        return jsonify({"ok": False, "msg": "未配置 DeepSeek key"}), 400
    code = ds.normalize(code)
    holding = next((h for h in portfolio.load() if h.get("code") == code), None)
    if not holding:
        return jsonify({"ok": False, "msg": "该股票不在持仓中"}), 404
    quotes = ds.tencent_quote([code])
    q = quotes.get(code, {})
    enriched = [h for h in portfolio.with_pnl(quotes) if h.get("code") == code]
    if enriched:
        holding = enriched[0]
    metrics = ds.sina_metrics(code)
    try:
        advice = llm.position_advice(holding, q, metrics)
    except llm.LLMError as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    return jsonify({"ok": True, "advice": advice, "model": config.DEEPSEEK_MODEL})


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
