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
from zoneinfo import ZoneInfo

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
# 选股与形态初筛（2026-07-16 抽出 screening.py）。显式带回名字，路由调用点不用改；
# `app._pa_score` / `app._FACTOR_RANGE` 等仍可达（测试与 agent 依赖）。
from screening import (  # noqa: E402,F401
    _safe_metrics, _safe_kline, _pa_score, _factor_pct, _balanced_pick,
    _screen_rows, _metrics_of, _FACTOR_RANGE, _EMPTY_METRICS,
    _SCREEN_CAP_TOTAL, _PRESCREEN, VOL_FLOOR, VOL_CEIL, _PA_RANK_MAX,
)
# AI 提示词注入块（2026-07-16 抽出 ai_blocks.py）。同样显式带回，路由调用点不改。
from ai_blocks import (  # noqa: E402,F401
    _total_assets, _tier_block, _record_basis_stats, _lesson_block, _agent_blocks,
    _fee_block, _macro_block, _profile_block, _ai_web_context, _stock_house_view,
    _regime_view,
)
# 交易时段判定移到 agent_loop（与 current_slot 同源）；带回以保持路由调用点不变。
from agent_loop import _market_open  # noqa: E402,F401

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


@app.route("/review")
def review() -> str:
    """复盘自动化模块（独立页面，不与选股看板共用主页）。"""
    return render_template("review.html")


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
            ai = llm.market_overview(idx["indices"], breadth, _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _regime_view(agent_loop.current_regime()) + _ai_web_context("market"))
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


def _min5_with_avg(min5: list[dict]) -> list[dict]:
    """5日 5 分钟线 + **按天重置的均价线(VWAP)**。

    多日分时图的均价线每天开盘重新累计（跨天连续累计无意义）。用 close×volume 近似
    成交额（sina 5min 不单给成交额）。volume 缺失则该点 avg=None，不外推。
    """
    out, cum_amt, cum_vol, cur_day = [], 0.0, 0.0, ""
    for k in min5:
        day = str(k.get("date", ""))[:10]
        if day != cur_day:      # 跨天 → 重置累计
            cum_amt, cum_vol, cur_day = 0.0, 0.0, day
        vol = float(k.get("volume") or 0)
        close = float(k.get("close") or 0)
        cum_amt += close * vol
        cum_vol += vol
        out.append({"t": k["date"], "close": k["close"],
                    "avg": round(cum_amt / cum_vol, 3) if cum_vol > 0 else None})
    return out


@app.route("/api/wave/<code>")
def wave(code: str):
    """波动多周期：当日分时 + 5日(5分钟) + 日K(~260, 供30/60/当季/当年切片) + 昨收基准。

    一次并发返回全部序列，前端切换周期纯前端切片、不再请求。
    """
    code = ds.normalize(code)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_intra = pool.submit(ds.tencent_minute, code)
        f_min5 = pool.submit(ds.sina_kline, code, 240, 5)     # num=240 ≈ 近5交易日
        # num=600：近1年窗口(~245交易日) + MA240 打底(240) ≈ 485，600 留足余量。
        f_daily = pool.submit(ds.sina_kline, code, 600, 240)
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
        "min5": _min5_with_avg(min5),
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
    """持仓 + 实时盈亏 + 组合汇总（含可用现金 / 累计已实现盈亏 / 总资产）+ 已实现流水。"""
    codes = portfolio.codes()
    quotes = ds.tencent_quote(codes) if codes else {}
    rows = portfolio.with_pnl(quotes)
    s = portfolio.summary(rows)
    cash = float((profile_store.get_active() or {}).get("cash") or 0)
    s["cash"] = round(cash, 2)
    s["realized_total"] = portfolio.realized_total()
    s["total_assets"] = round(cash + s.get("market_value", 0), 2)   # 现金 + 持仓市值
    return jsonify({"holdings": rows, "summary": s,
                    "realized": portfolio.realized(), "updated": _now()})


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


@app.route("/api/portfolio/reduce", methods=["POST"])
def portfolio_reduce():
    """减仓/卖出：{code, shares, sell_price}。FIFO 扣 lot、现金加回净收入、记一条已实现盈亏。

    sell_price='market' 用当前市价。卖满该只=清仓变现。超卖/无持仓 → 400。
    """
    b = request.json or {}
    code = ds.normalize(b.get("code", ""))
    q = ds.tencent_quote([code]) if code else {}
    raw = b.get("sell_price")
    if raw == "market":
        raw = (q.get(code, {}) or {}).get("price")
    try:
        shares = float(b.get("shares", 0))
        price = float(raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "股数/卖价必须是数字"}), 400
    try:
        holdings = portfolio.reduce(code, shares, price,
                                    name=(q.get(code, {}) or {}).get("name", ""))
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    return jsonify({"ok": True, "holdings": holdings, "sold_at": round(price, 3),
                    "realized_total": portfolio.realized_total()})


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
    # 画像选择器只列**用户自己的**档；agent 系列（agent-稳健/均衡/激进）是 20 个 agent 的
    # 风险偏好+费率来源，按 ID 直接取用（不走此列表），故从用户选择器隐藏但**绝不删**。
    # ?all=1 显示全部（调试/管理 agent 画像用）。
    show_all = request.args.get("all") == "1"
    profiles = [p for p in profile_store.list_profiles()
                if show_all or not str(p.get("name", "")).startswith("agent-")]
    return jsonify({"profiles": profiles, "active_id": prof.get("id"),
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", "")) + _stock_house_view(code)
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _profile_block(prof) + _ai_web_context("position", code, q.get("name", "")) + _stock_house_view(code)
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
    web_ctx = _tier_block() + _fee_block() + _lesson_block() + _macro_block() + _regime_view(agent_loop.current_regime()) + _ai_web_context("market")
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


@app.route("/api/sectors/backfill", methods=["POST"])
def api_sectors_backfill():
    """后台回填板块日变化历史(~一个季度)，让板块走势有足够数据。逐股日 K，只填缺失日期。"""
    try:
        days = int((request.get_json(silent=True) or {}).get("days", 95))
    except (TypeError, ValueError):
        days = 95
    days = max(10, min(days, 250))  # 夹在 [10, 250] 交易日
    threading.Thread(target=universe_store.backfill_sector_daily,
                     kwargs={"days": days}, daemon=True).start()
    return jsonify({"ok": True, "running": True, "days": days})


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
    """因子回测结果：IC 均值/t值/胜率 + 当前方向 + 失效报警 + 超额分布/判罪线 + 容量。"""
    stale, lag = factor_lab.is_stale()
    return jsonify({"summary": factor_lab.summary(),
                    "directions": factor_lab.directions(),
                    "alerts": factor_lab.decay_alert(),
                    "history": factor_lab.direction_history(limit=20),
                    "flip_rate": factor_lab.flip_rate(),
                    # 超额分布 = 判罪线的唯一合法来源；判罪线本身是**纪律参数**，
                    # 暴露出来让用户能认可/否决（PITFALLS#1：拍的数不许假装有数据支持）。
                    "excess_dist": factor_lab.excess_dist(),
                    "lesson_gate": {
                        "horizon": agent_loop.JUDGE_H, "pct": agent_loop.LESSON_PCT,
                        "note": f"超额收益落在历史分布底部 {agent_loop.LESSON_PCT}% 才记教训。"
                                f"分布是事实(16万样本)，取底部 {agent_loop.LESSON_PCT}% 是"
                                f"**纪律参数**（选择性取舍，非数据结论）。"
                                f"不用「超额<0」是因为 p50 为负——个股跑输上证是常态，"
                                f"那条线会把过半建仓点判成失败。"},
                    "freshness": {"last_ic": factor_lab.last_ic_date(), "lag_days": lag,
                                  "stale": stale,
                                  "note": f"IC 天然滞后 {max(factor_lab.HORIZONS)} 个交易日"
                                          "（今日因子值要等未来收益才能算 IC）"},
                    "status": factor_lab.status()})


@app.route("/api/factors/rolling")
def api_factors_rolling():
    """滚动 IC 曲线（因子衰减看得见）。?factor=cum20&horizon=10&window=60"""
    return jsonify({"points": factor_lab.rolling_ic(
        request.args.get("factor", "cum20"), _arg_int("horizon", 10),
        _arg_int("window", 60))})


@app.route("/api/factors/stops")
def api_factor_stops():
    """止损线网格回测：各档止损的平均收益/胜率/5%最差/触发率。?stocks=200&hold=20

    判据不是「哪个赚最多」（那答案永远是不止损），而是「花多少收益把尾部压住」。
    """
    return jsonify(factor_lab.backtest_stops(_arg_int("stocks", 200), _arg_int("hold", 20)))


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
    """agent 列表 + 教训汇总 + 建仓留痕(含结算) + 容量。"""
    return jsonify({"agents": agent_store.list_agents(),
                    "lessons": agent_store.lesson_rollup(12),
                    "entries": agent_store.entries(limit=50),
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
    return jsonify(agent_loop.run_day(gid, focus=b.get("focus", ""),
                                      dry_run=bool(b.get("dry")),
                                      force=bool(b.get("force"))))


@app.route("/api/agents/run_all", methods=["POST"])
def api_agents_run_all():
    """跑所有启用的 agent（多档位/同档多账户并行实验）。后台线程，不阻塞。"""
    b = request.get_json(silent=True) or {}
    dry = bool(b.get("dry"))
    threading.Thread(target=agent_loop.run_all, args=(dry,), daemon=True).start()
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
                    "pending": agent_store.pending_of(gid),
                    "pending_stats": agent_store.pending_stats(),
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
    # require_open：非交易时段只补判条件单、不跑决策（否则 16 次 v4-pro 全部被拒单白烧）
    for r in agent_loop.run_all(require_open=True):
        if r.get("skipped"):
            logger.info("agent %s: %s", r.get("agent", "-"), r["skipped"])
        elif r.get("ok"):
            logger.info("agent %s [%s]: 挂出 %d / 结算成交 %d / 教训 %d",
                        r.get("agent"), r.get("slot", "-"), len(r.get("placed") or []),
                        len(r.get("filled") or []), len(r.get("lessons") or []))


# 盘中每 5 分钟探一次日循环。claim_slot 保证每桶只真跑一次，多余 tick 近乎零成本；
# 解决「app 在非交易时段启动、此后当日决策永不自动触发」——见
# plan/2026-07-17-intraday-agent-scheduler-design.md。
_AGENT_TICK_SEC = 300
_agent_tick_lock = threading.Lock()  # 单飞：一轮没跑完就跳过本次 tick，不叠并发 run_all


def _agent_tick() -> None:
    """一次调度心跳：非阻塞抢单飞锁 → 跑一轮 `_agent_boot`（run_all(require_open=True)）。

    抢不到锁（上一轮还在跑）就跳过——20 个 agent 一轮 workers=3 可能 7~20 分钟，
    不能让 5 分钟的 tick 叠出第二个并发 run_all（会翻倍行情/LLM 并发、易被限流）。
    """
    if not _agent_tick_lock.acquire(blocking=False):
        logger.info("agent 调度：上一轮未结束，跳过本次 tick")
        return
    try:
        _agent_boot()
    finally:
        _agent_tick_lock.release()


def _agent_scheduler() -> None:
    """盘中日循环调度器：先立刻跑一次（等价旧的启动即 `_agent_boot`），之后每 5 分钟探一次。

    `require_open` + `claim_slot` 双门保证：非交易时段只补条件单；每桶只真决策一次。
    守护线程，绝不因单次异常整体退出。
    """
    _agent_tick()
    while True:
        time.sleep(_AGENT_TICK_SEC)
        try:
            _agent_tick()
        except Exception as e:  # noqa: BLE001 调度器绝不能因单次心跳异常而停摆
            logger.warning("agent 调度心跳异常（下次继续）：%s", e)


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
        # 因子 IC 惰性自动刷新：过期才重跑(14s)。判过期已扣除 IC 的 20 交易日结构性滞后
        r = factor_lab.refresh_if_stale()
        if r.get("skipped"):
            logger.info("因子 IC: %s", r["skipped"])
        # 启动即跑一次 + 之后每 5 分钟探一次（长期挂机也能每桶自动跑，见
        # plan/2026-07-17-intraday-agent-scheduler-design.md）。守护线程，不阻塞预热。
        threading.Thread(target=_agent_scheduler, daemon=True).start()
    except (OSError, ValueError, sqlite3.Error) as e:
        logger.warning("全市场池预热失败（不影响其余功能）: %s", e)


if __name__ == "__main__":
    if news_store.stats()["total"] == 0:  # 首次运行：后台一次性回填新闻库(不阻塞启动)
        threading.Thread(target=news_store.backfill, daemon=True).start()
        logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
    threading.Thread(target=_universe_boot, daemon=True).start()
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
