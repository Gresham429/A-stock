"""A股数据层：实时行情、波动/资金指标、研报、龙虎榜、解禁、资金流。

数据源优先级（不封IP优先）：
    行情/估值 -> 腾讯财经（GBK, HTTP）
    波动率/资金流 -> 新浪 MoneyFlow（含每日收盘价，一份数据两用）
    研报/龙虎榜/解禁 -> 东财 reportapi + datacenter（东财独有数据，走 em_get 限流）

所有东财请求通过 em_get() 串行限流，避免高频被封 IP。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EM_MIN_INTERVAL = 1.0  # 东财两次请求最小间隔(秒)
_em_last_call = [0.0]


# ── 基础 helper ──────────────────────────────────────────────────────────
def normalize(code: str) -> str:
    """归一化为纯 6 位代码：SH688017 / 688017.SH / sz000001 -> 688017 / 000001。"""
    c = code.strip().upper()
    for sep in (".",):
        if sep in c:
            parts = c.split(sep)
            c = parts[0] if parts[0].isdigit() else parts[1]
    c = c.replace("SH", "").replace("SZ", "").replace("BJ", "")
    return c.zfill(6)


def market_prefix(code: str) -> str:
    """6 位代码 -> 交易所前缀（sh / sz / bj）。

    北交所号段：8xxxxx、4xxxxx、**92xxxx**（920 为 2024 年起启用的新号段）。
    `92` 必须先于 `9` 判断——9 开头的另一支是沪B（900xxx），仍属上海。
    实测（腾讯 qt.gtimg.cn）：`bj920000`/`bj430047` 有数据，`sh920000`/`sz430047` 返回空。
    """
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("6", "9")):  # 9 开头剩沪B(900xxx)
        return "sh"
    return "sz"


def _http_get(url: str, ref: str | None = None, gbk: bool = False,
              timeout: int = 15) -> str:
    """普通 HTTP GET（腾讯/新浪，不封 IP）。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    if ref:
        req.add_header("Referer", ref)
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return raw.decode("gbk" if gbk else "utf-8", "ignore")


def em_get(url: str, ref: str = "https://data.eastmoney.com/",
           timeout: int = 20) -> str:
    """东财统一请求入口：串行限流 + 随机抖动，避免被风控封 IP。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.4))
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", ref)
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    finally:
        _em_last_call[0] = time.time()


# ── 行情/估值（腾讯） ─────────────────────────────────────────────────────
QUOTE_CHUNK = 400  # 单次请求代码数上限：实测 800 可过、2000 报 HTTP 414，取 400 留足余量


def tencent_quote(codes: list[str], workers: int = 8) -> dict[str, dict[str, Any]]:
    """批量实时行情：价格/涨跌/PE/PB/市值/换手/振幅。返回 {code: {...}}。

    自动分批：全部代码拼一个 URL 在全市场池（~5000 只 = 44KB URL）会报 HTTP 414，
    故按 QUOTE_CHUNK 切片并发请求；单批失败只丢该批、不拖垮整体。
    """
    if not codes:
        return {}
    if len(codes) > QUOTE_CHUNK:
        chunks = [codes[i:i + QUOTE_CHUNK] for i in range(0, len(codes), QUOTE_CHUNK)]
        merged: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(lambda cs: tencent_quote(cs, workers), chunks):
                merged.update(part)
        return merged
    prefixed = [market_prefix(c) + c for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    try:
        data = _http_get(url, gbk=True, timeout=15)
    except OSError as e:
        logger.error("腾讯行情请求失败(%d 只): %s", len(codes), e)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue

        def f(i: int) -> float:
            try:
                return float(vals[i])
            except (ValueError, IndexError):
                return 0.0

        code = key[2:]
        result[code] = {
            "code": code,
            "name": vals[1],
            "price": f(3),
            "last_close": f(4),      # 昨收（当日盈亏用）
            "change_amt": f(31),     # 涨跌额
            "chg_pct": f(32),
            "turnover": f(38),
            "pe_ttm": f(39),
            "amplitude": f(43),
            "mcap_yi": f(44),
            "float_mcap_yi": f(45),
            "pb": f(46),
            "limit_up": f(47),
            "limit_down": f(48),
            "vol_ratio": f(49),
            "lot_cost": round(f(3) * 100, 0),  # 1手(100股)成本
        }
    return result


# ── 全市场名单 + 快照（新浪 hs_a，一份数据两用：名单 + 板块日统计） ────────
_SINA_HQ = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
            "/Market_Center.getHQNodeData?page={page}&num={num}&sort=symbol&asc=1&node=hs_a")
_SINA_CNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
             "/Market_Center.getHQNodeStockCount?node=hs_a")
_SINA_REF = "https://vip.stock.finance.sina.com.cn/"


def sina_stock_count() -> int:
    """全 A 股票总数（新浪 hs_a 节点）。失败返回 0。"""
    try:
        return int(_http_get(_SINA_CNT, ref=_SINA_REF, gbk=True, timeout=10).strip().strip('"'))
    except (OSError, ValueError) as e:
        logger.warning("新浪 A 股总数请求失败: %s", e)
        return 0


def _sina_hq_page(page: int, num: int = 80) -> list[dict[str, Any]]:
    """新浪 hs_a 单页。失败返回 []（单页失败不拖垮全量）。"""
    try:
        raw = _http_get(_SINA_HQ.format(page=page, num=num), ref=_SINA_REF, gbk=True, timeout=20)
        data = json.loads(raw) if raw.strip().startswith("[") else []
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as e:
        logger.warning("新浪 hs_a 第 %d 页失败: %s", page, e)
        return []


def sina_all_stocks(num: int = 80, workers: int = 8) -> list[dict[str, Any]]:
    """全 A 名单 + 实时快照（一次拉全，约 5527 只、70 页并发 ~10s）。

    字段自带涨跌幅/成交额/换手/市值/PE/PB，故名单刷新与板块日统计共用这一份数据，
    无需再走腾讯分批（单 URL 装不下 5000+ 代码）。失败返回 []。
    """
    total = sina_stock_count()
    if not total:
        return []
    pages = (total + num - 1) // num
    with ThreadPoolExecutor(max_workers=workers) as ex:
        chunks = list(ex.map(lambda p: _sina_hq_page(p, num), range(1, pages + 1)))
    out: dict[str, dict[str, Any]] = {}
    for items in chunks:
        for it in items:
            code = str(it.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            out[code] = {
                "code": code,
                "symbol": str(it.get("symbol") or ""),
                "name": str(it.get("name") or "").strip(),
                "price": _fnum(it.get("trade")),
                "chg_pct": _fnum(it.get("changepercent")),
                "amount": _fnum(it.get("amount")),          # 成交额（元）
                "turnover": _fnum(it.get("turnoverratio")),  # 换手率（%）
                "mktcap": _fnum(it.get("mktcap")),           # 总市值（万元）
                "float_mcap": _fnum(it.get("nmc")),          # 流通市值（万元）
                "pe_ttm": _fnum(it.get("per")),
                "pb": _fnum(it.get("pb")),
            }
    logger.info("新浪全市场名单: %d/%d 只（%d 页）", len(out), total, pages)
    return list(out.values())


def _fnum(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── 波动率 + 资金流（新浪，一份数据两用） ─────────────────────────────────
def sina_metrics(code: str, num: int = 45) -> dict[str, Any]:
    """新浪 MoneyFlow：含每日收盘价 -> 波动率/区间位置/20日涨幅/主力资金流。

    返回: vol(年化波动%), range_pos(20日区间位置%), cum20(20日累计涨幅%),
          net5/net20(主力5/20日净流入,亿元), series(近30日 {date,close,main} 用于图表)。
    """
    pre = market_prefix(code) + code
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={num}&sort=opendate&asc=0&daima={pre}")
    empty = {"vol": None, "range_pos": None, "cum20": None,
             "net5": None, "net20": None, "series": []}
    try:
        t = _http_get(url, ref="https://finance.sina.com.cn/")
        arr = json.loads(t[t.index("["):t.rindex("]") + 1])
    except (OSError, ValueError) as e:
        logger.warning("新浪资金流请求失败 %s: %s", code, e)
        return empty
    if not arr:
        return empty

    arr = list(reversed(arr))  # 时间正序
    try:  # 全市场池里字段可能为空串/异常值（手工龙头池不会），解析失败按无数据处理
        closes = [float(x["trade"]) for x in arr]
        r0net = [float(x.get("r0_net", 0)) for x in arr]  # 主力(超大单)净额，元
        dates = [x["opendate"] for x in arr]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("新浪资金流字段解析失败 %s: %s", code, e)
        return empty

    vol = _annualized_vol(closes, 20)
    # 分母可能为 0（停牌日收盘价缺失），不设防会 ZeroDivisionError 打崩整个选股
    cum20 = ((closes[-1] / closes[-21] - 1) * 100
             if len(closes) > 20 and closes[-21] > 0 else None)
    range_pos = None
    if len(closes) >= 20:
        w = closes[-20:]
        lo, hi = min(w), max(w)
        range_pos = (closes[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0

    series = [{"date": dates[i], "close": closes[i], "main": r0net[i]}
              for i in range(max(0, len(dates) - 30), len(dates))]
    return {
        "vol": round(vol, 0) if vol else None,
        "range_pos": round(range_pos, 0) if range_pos is not None else None,
        "cum20": round(cum20, 1) if cum20 is not None else None,
        "net5": round(sum(r0net[-5:]) / 1e8, 2),
        "net20": round(sum(r0net[-20:]) / 1e8, 2),
        "series": series,
    }


def _annualized_vol(closes: list[float], n: int = 20) -> float | None:
    """近 n 日日对数收益率的年化波动率(%)。收盘价含 0/负值（停牌日、源缺口）则返回 None。

    全市场池里确实存在收盘价为 0 的记录（手工龙头池不会），不设防会 math domain error
    并穿透 executor.map 把整个选股打崩。
    """
    if len(closes) < n + 1:
        return None
    window = closes[len(closes) - n - 1:]
    if any(c <= 0 for c in window):
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(252) * 100


def scenario_band(price: float, vol_annual: float | None,
                  months: float = 1.0) -> dict[str, Any] | None:
    """由年化波动率反推价格情景区间（±1σ 约 68% 概率，±2σ 约 95%）。"""
    if not vol_annual or price <= 0:
        return None
    sigma = vol_annual / 100 * math.sqrt(months / 12)
    return {
        "sigma_pct": round(sigma * 100, 1),
        "low1": round(price * (1 - sigma), 2),
        "high1": round(price * (1 + sigma), 2),
        "low2": round(price * (1 - 2 * sigma), 2),
        "high2": round(price * (1 + 2 * sigma), 2),
    }


# ── 研报（东财 reportapi） ────────────────────────────────────────────────
def eastmoney_reports(code: str, page_size: int = 20) -> list[dict[str, Any]]:
    """个股研报列表 + 评级 + EPS 预测 + PDF 链接。"""
    url = ("https://reportapi.eastmoney.com/report/list?industryCode=*"
           f"&pageSize={page_size}&industry=*&rating=*&ratingChange=*"
           "&beginTime=2023-01-01&endTime=2030-01-01&pageNo=1&qType=0&code=" + code)
    try:
        d = json.loads(em_get(url))
        rows = d.get("data") or []
    except (OSError, ValueError) as e:
        logger.warning("东财研报请求失败 %s: %s", code, e)
        return []
    out = []
    for r in rows:
        info = r.get("infoCode", "")
        out.append({
            "date": (r.get("publishDate") or "")[:10],
            "org": r.get("orgSName", ""),
            "author": r.get("researcher", ""),
            "title": r.get("title", ""),
            "rating": r.get("emRatingName", ""),
            "eps_this": r.get("predictThisYearEps"),
            "eps_next": r.get("predictNextYearEps"),
            "pdf": f"https://pdf.dfcfw.com/pdf/H3_{info}_1.pdf" if info else "",
        })
    return out


# ── 龙虎榜（东财 datacenter） ─────────────────────────────────────────────
def _datacenter(report_name: str, filter_str: str, page_size: int = 30,
                sort_col: str = "", sort_type: str = "-1") -> list[dict[str, Any]]:
    """东财数据中心统一查询（龙虎榜/解禁共用）。"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           f"?reportName={report_name}&columns=ALL&filter={urllib.parse.quote(filter_str)}"
           f"&pageNumber=1&pageSize={page_size}"
           f"&sortColumns={sort_col}&sortTypes={sort_type}&source=WEB&client=WEB")
    try:
        d = json.loads(em_get(url))
        return (d.get("result") or {}).get("data") or []
    except (OSError, ValueError) as e:
        logger.warning("东财 datacenter 请求失败 [%s]: %s", report_name, e)
        return []


def dragon_tiger(code: str, look_back_days: int = 180) -> dict[str, Any]:
    """龙虎榜：近 look_back 天上榜记录 + 最近一次买卖席位 TOP5。"""
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    data = _datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        f'(TRADE_DATE>=\'{start}\')(TRADE_DATE<=\'{end}\')(SECURITY_CODE="{code}")',
        page_size=50, sort_col="TRADE_DATE", sort_type="-1",
    )
    records = [{
        "date": str(r.get("TRADE_DATE", ""))[:10],
        "reason": r.get("EXPLANATION", ""),
        "change_pct": round(float(r.get("CHANGE_RATE") or 0), 2),
        "net_buy_wan": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e4, 1),
        "turnover": round(float(r.get("TURNOVERRATE") or 0), 2),
    } for r in data]

    seats: dict[str, list] = {"buy": [], "sell": []}
    if records:
        latest = records[0]["date"]
        for side, rpt, sort_c in (("buy", "RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
                                  ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")):
            rows = _datacenter(
                rpt, f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")',
                page_size=10, sort_col=sort_c, sort_type="-1")
            for x in rows[:5]:
                seats[side].append({
                    "name": x.get("OPERATEDEPT_NAME", ""),
                    "buy_wan": round((x.get("BUY") or 0) / 1e4, 1),
                    "sell_wan": round((x.get("SELL") or 0) / 1e4, 1),
                    "net_wan": round((x.get("NET") or 0) / 1e4, 1),
                    "is_inst": str(x.get("OPERATEDEPT_CODE", "")) == "0",
                })
    return {"records": records, "seats": seats}


# ── 解禁（东财 datacenter） ───────────────────────────────────────────────
def lockup_expiry(code: str, forward_days: int = 365) -> dict[str, Any]:
    """限售解禁：历史 + 未来 forward_days 天待解禁（含风险标注）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=forward_days)).strftime("%Y-%m-%d")

    def _parse(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            ratio = r.get("FREE_RATIO") or 0
            out.append({
                "date": str(r.get("FREE_DATE", ""))[:10],
                "type": r.get("FREE_SHARES_TYPE", "") or r.get("FREE_SHARES_TYPE_NAME", ""),
                "shares_wan": round((r.get("FREE_SHARES") or 0) / 1e4, 1),
                "ratio_pct": round(float(ratio) * 100, 2) if ratio else 0,
            })
        return out

    history = _parse(_datacenter(
        "RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")',
        page_size=10, sort_col="FREE_DATE", sort_type="-1"))
    upcoming = _parse(_datacenter(
        "RPT_LIFT_STAGE",
        f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{today}\')(FREE_DATE<=\'{end}\')',
        page_size=20, sort_col="FREE_DATE", sort_type="1"))

    # 风险：未来 90 天内解禁比例 > 5% 视为高压
    risk = "none"
    soon = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    near = [u for u in upcoming if u["date"] <= soon]
    if near:
        max_ratio = max(u["ratio_pct"] for u in near)
        risk = "high" if max_ratio >= 5 else "mid"
    return {"history": history, "upcoming": upcoming, "risk": risk}


# ── 日K线 OHLC（新浪，用于蜡烛图 + 箱形图） ────────────────────────────────
def sina_kline(code: str, num: int = 120, scale: int = 240) -> list[dict[str, Any]]:
    """新浪K线（前复权口径）。scale=240 日K / 5 五分钟 / 15 十五分钟 …。

    返回时间正序 [{date, open, high, low, close, volume}]；分钟级时 date 含 'HH:MM:SS'。
    **指数请用 `index_kline()`** —— market_prefix 对 000xxx 会误判为 sz。
    """
    return _sina_kline_sym(market_prefix(code) + code, num, scale)


def index_kline(sym: str, num: int = 70) -> list[dict[str, Any]]:
    """指数日K。sym 须**带前缀且硬编码**（如 sh000001 / sz399006）。

    指数前缀不能过 `market_prefix()`：上证/沪深300/科创50 均在 sh，但它对
    000xxx 一律判 sz（同 `index_quotes()` 的注释）。故这里只收调用方给的完整符号。
    """
    return _sina_kline_sym(sym, num, 240)


def _sina_kline_sym(sym: str, num: int, scale: int) -> list[dict[str, Any]]:
    """新浪K线取数内核（收完整符号，个股/指数共用）。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={num}")
    try:
        arr = json.loads(_http_get(url, ref="https://finance.sina.com.cn/"))
    except (OSError, ValueError) as e:
        logger.warning("新浪K线请求失败 %s: %s", sym, e)
        return []
    if not isinstance(arr, list):
        # 未知/退市代码新浪回 `null` -> json.loads 得 None -> 迭代抛 TypeError。
        # TypeError 不在上面的捕获里，会穿透调用方（全市场池必踩，见 PITFALLS#11）。
        logger.warning("新浪K线返回非列表 %s: %r", sym, arr)
        return []
    out = []
    for x in arr:
        try:
            out.append({
                "date": x["day"],
                "open": float(x["open"]), "high": float(x["high"]),
                "low": float(x["low"]), "close": float(x["close"]),
                "volume": float(x.get("volume", 0)),
            })
        except (KeyError, ValueError):
            continue
    return out


def tencent_minute(code: str) -> list[dict[str, Any]]:
    """腾讯当日分时（逐分钟价格，不封 IP）。返回 [{t:'09:30', price}]。

    源 ifzq.gtimg.cn minute/query，data[sym].data.data 每项 'HHMM 价 累计量 累计额'。
    非交易时段返回上一交易日分时；数据缺失返回空列表。
    """
    sym = market_prefix(code) + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    try:
        d = json.loads(_http_get(url, ref="https://gu.qq.com/"))
    except (OSError, ValueError) as e:
        logger.warning("腾讯分时请求失败 %s: %s", code, e)
        return []
    rows = (((d.get("data") or {}).get(sym) or {}).get("data") or {}).get("data") or []
    out = []
    for item in rows:
        parts = item.split(" ")
        if len(parts) < 2:
            continue
        hhmm = parts[0]
        try:
            price = float(parts[1])
        except ValueError:
            continue
        out.append({"t": f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm,
                    "price": price})
    return out


# ── 财报摘要（新浪利润表：营收/归母净利 + 同比） ──────────────────────────
def financial_summary(code: str, periods: int = 4) -> list[dict[str, Any]]:
    """最近 periods 期利润表关键项。返回 [{period, revenue_yi, revenue_yoy, profit_yi, profit_yoy}]。"""
    sym = market_prefix(code) + code
    url = ("https://quotes.sina.cn/cn/api/openapi.php/"
           "CompanyFinanceService.getFinanceReport2022"
           f"?paperCode={sym}&source=lrb&type=0&page=1&num={periods}")
    try:
        d = json.loads(_http_get(url, ref="https://finance.sina.com.cn/"))
        report_list = d.get("result", {}).get("data", {}).get("report_list", {}) or {}
    except (OSError, ValueError) as e:
        logger.warning("新浪财报请求失败 %s: %s", code, e)
        return []

    def _pick(items: list[dict], titles: tuple[str, ...]) -> tuple[Any, Any]:
        for t in titles:
            for it in items:
                if it.get("item_title") == t and it.get("item_value") not in (None, ""):
                    return it.get("item_value"), it.get("item_tongbi")
        return None, None

    out = []
    for period in sorted(report_list.keys(), reverse=True)[:periods]:
        items = report_list[period].get("data", []) or []
        rev, rev_yoy = _pick(items, ("营业总收入", "营业收入"))
        prof, prof_yoy = _pick(items, ("归属于母公司所有者的净利润", "净利润"))
        out.append({
            "period": f"{period[:4]}-{period[4:6]}-{period[6:8]}",
            "revenue_yi": round(float(rev) / 1e8, 2) if rev else None,
            "revenue_yoy": round(float(rev_yoy) * 100, 1) if rev_yoy not in (None, "") else None,
            "profit_yi": round(float(prof) / 1e8, 2) if prof else None,
            "profit_yoy": round(float(prof_yoy) * 100, 1) if prof_yoy not in (None, "") else None,
        })
    return out


# ── 个股新闻（东财 search-api，公司/题材新闻，政策面参考） ──────────────────
def stock_news(code: str, page_size: int = 8) -> list[dict[str, Any]]:
    """东财个股新闻。返回 [{date, title, source}]。（部分住宅 IP 间歇返回空，安全降级为 []）"""
    inner = json.dumps({
        "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                  "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}},
    }, separators=(",", ":"))
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=jQ&param="
           + urllib.parse.quote(inner))
    try:
        t = em_get(url, ref="https://so.eastmoney.com/")
        d = json.loads(t[t.index("(") + 1:t.rindex(")")])
        arts = d.get("result", {}).get("cmsArticleWebOld", []) or []
    except (OSError, ValueError) as e:
        logger.warning("东财个股新闻请求失败 %s: %s", code, e)
        return []
    return [{
        "date": a.get("date", ""),
        "title": re.sub(r"<[^>]+>", "", a.get("title", "")),
        "source": a.get("mediaName", ""),
    } for a in arts]


def announcements(code: str, n: int = 8) -> list[dict[str, Any]]:
    """东财个股公告（近 n 条，标题+日期，沪深自动）。供公司叙事『在做/要做』。降级 []。"""
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           f"?sr=-1&page_size={n}&page_index=1&ann_type=A&client_source=web"
           f"&stock_list={code}&f_node=0&s_node=0")
    try:
        rows = (json.loads(em_get(url)).get("data") or {}).get("list") or []
    except (OSError, ValueError) as e:
        logger.warning("东财公告请求失败 %s: %s", code, e)
        return []
    return [{"date": (a.get("notice_date", "") or "")[:10], "title": a.get("title", "")}
            for a in rows if a.get("title")]


def concept_tags(code: str, limit: int = 12) -> list[str]:
    """东财个股所属板块/概念（行业+概念+地域混合，板块名自解释）。供叙事『所属方向/题材』。降级 []。"""
    market = 1 if code.startswith(("6", "9")) else 0
    url = ("https://push2.eastmoney.com/api/qt/slist/get"
           f"?fltt=2&invt=2&secid={market}.{code}&spt=3&pi=0&pz=200&po=1&fields=f12,f14,f3")
    try:
        diff = (json.loads(em_get(url, ref="https://quote.eastmoney.com/")).get("data") or {}).get("diff") or {}
        items = list(diff.values()) if isinstance(diff, dict) else diff
    except (OSError, ValueError) as e:
        logger.warning("东财概念板块请求失败 %s: %s", code, e)
        return []
    return [it.get("f14", "") for it in items if it.get("f14")][:limit]


def global_markets() -> list[dict[str, Any]]:
    """外围市场**数值**指标（新浪外盘，纯 HTTP、不封 IP、海外可用）。

    覆盖：WTI原油/布伦特原油/黄金/铜（`hf_*` 外盘期货）+ 道指/纳指/标普500（`gb_$*`）。
    注：该源无美元指数(`hf_DX`)与 VIX，实测返回空，故不含。
    返回 [{name, price, chg_pct}]；失败降级 []。
    """
    url = ("https://hq.sinajs.cn/list="
           "hf_CL,hf_OIL,hf_GC,hf_HG,gb_$dji,gb_$ixic,gb_$inx")
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", "https://finance.sina.com.cn/")
        text = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
    except OSError as e:
        logger.warning("新浪外盘行情请求失败: %s", e)
        return []
    alias = {"纽约原油": "WTI原油", "伦敦原油": "布伦特原油",
             "纽约黄金": "黄金", "纽约铜": "铜"}
    out: list[dict[str, Any]] = []
    for line in text.strip().split("\n"):
        if '="' not in line:
            continue
        key = line.split("=")[0].strip().replace("var hq_str_", "")
        v = line.split('"')[1].split(",")
        if len(v) < 4 or not v[0]:
            continue
        try:
            if key.startswith("hf_"):      # 外盘期货：[0]现价 [7]昨结算 [13]名称
                price = float(v[0])
                prev = float(v[7]) if v[7] else 0.0
                name = v[13] if len(v) > 13 else key
                chg = round((price - prev) / prev * 100, 2) if prev else None
            else:                          # 美股指数 gb_$：[0]名称 [1]现价 [2]涨跌幅%
                name = v[0]
                price = float(v[1])
                chg = round(float(v[2]), 2)
        except (ValueError, IndexError):
            continue
        out.append({"name": alias.get(name, name), "price": price, "chg_pct": chg})
    return out


# ── 全市场财经/政策快讯（财联社 + 东财 7×24，用于 AI「自我更新知识」A 方案）──
def cls_telegraph(page_size: int = 30) -> list[dict[str, Any]]:
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）。"""
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
              "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    try:
        t = _http_get(url, ref="https://www.cls.cn/")
        rd = json.loads(t).get("data", {}).get("roll_data", []) or []
    except (OSError, ValueError) as e:
        logger.warning("财联社快讯请求失败: %s", e)
        return []
    out = []
    for it in rd:
        ts = it.get("ctime")
        tm = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""
        title = it.get("title") or it.get("brief") or ""
        if title:
            out.append({"time": tm, "title": title, "source": "财联社"})
    return out


def eastmoney_global_news(page_size: int = 30) -> list[dict[str, Any]]:
    """东财 7×24 全球财经资讯。"""
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    params = {"client": "web", "biz": "web_724", "fastColumn": "102",
              "sortEnd": "", "pageSize": str(page_size), "req_trace": str(uuid.uuid4())}
    try:
        t = em_get(url + "?" + urllib.parse.urlencode(params),
                   ref="https://kuaixun.eastmoney.com/")
        lst = json.loads(t).get("data", {}).get("fastNewsList", []) or []
    except (OSError, ValueError) as e:
        logger.warning("东财全球资讯请求失败: %s", e)
        return []
    return [{"time": (it.get("showTime", "") or "")[5:16], "title": it.get("title", ""),
             "source": "东财"} for it in lst if it.get("title")]


def market_news_digest(limit: int = 12) -> list[dict[str, Any]]:
    """聚合最新全市场财经/政策快讯（财联社优先，东财兜底），去重取前 limit 条。"""
    news = cls_telegraph(limit + 8) + eastmoney_global_news(limit)
    seen: set[str] = set()
    out = []
    for n in news:
        key = n["title"][:18]
        if not n["title"] or key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out[:limit]


# ── 大盘指数 + 市场情绪（大盘研判用） ─────────────────────────────────────
_INDEX_DISPLAY = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000300", "沪深300"), ("sh000688", "科创50"),
]
_INDEX_AMOUNT_EXTRA = ("sz399106", "深证综指")  # 仅用于两市成交额合计（深市全量）


def index_quotes() -> dict[str, Any]:
    """五大指数行情 + 两市成交额（腾讯，不封 IP）。

    返回 {"indices": [{code,name,point,chg_pct,amount_yi}...], "amount_liang_yi": float|None}。
    amount_liang_yi = 上证(沪市全量) + 深证综指(深市全量) 成交额，单位亿元。
    指数前缀须硬编码（上证/沪深300/科创50 均在 sh，market_prefix 对 000xxx 会误判为 sz）。
    """
    prefixed = [p for p, _ in _INDEX_DISPLAY] + [_INDEX_AMOUNT_EXTRA[0]]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    try:
        data = _http_get(url, gbk=True, timeout=15)
    except OSError as e:
        logger.error("腾讯指数请求失败: %s", e)
        return {"indices": [], "amount_liang_yi": None}

    parsed: dict[str, dict[str, Any]] = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]  # 如 sh000001
        vals = line.split('"')[1].split("~")
        if len(vals) < 38:  # 指数字段较个股少，成交额在索引 37
            continue

        def f(i: int) -> float:
            try:
                return float(vals[i])
            except (ValueError, IndexError):
                return 0.0

        parsed[key] = {
            "name": vals[1],
            "point": round(f(3), 2),
            "chg_pct": f(32),
            "amount_yi": round(f(37) / 10000, 1),  # 成交额: 万 -> 亿
        }

    indices = []
    for pcode, disp_name in _INDEX_DISPLAY:
        d = parsed.get(pcode)
        if not d:
            continue
        indices.append({"code": pcode[2:], "name": disp_name,
                        "point": d["point"], "chg_pct": d["chg_pct"],
                        "amount_yi": d["amount_yi"]})

    sh = parsed.get("sh000001", {}).get("amount_yi")
    sz = parsed.get(_INDEX_AMOUNT_EXTRA[0], {}).get("amount_yi")
    liang = round(sh + sz, 1) if (sh and sz) else None
    return {"indices": indices, "amount_liang_yi": liang}


def _em_json(url: str, ref: str = "https://data.eastmoney.com/") -> dict[str, Any]:
    """东财 JSON 请求（走 em_get 限流），失败返回 {}。"""
    try:
        return json.loads(em_get(url, ref=ref))
    except (OSError, ValueError) as e:
        logger.warning("东财 JSON 请求失败: %s", e)
        return {}


_ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _limit_pool_count(endpoint: str, sort: str, date: str) -> int | None:
    """涨停/跌停池家数（东财 push2ex）。data 为 null（非交易日/盘前）时返回 None。"""
    params = urllib.parse.urlencode({
        "ut": _ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000,
        "sort": sort, "date": date,
    })
    d = _em_json(f"https://push2ex.eastmoney.com/{endpoint}?{params}",
                 ref="https://quote.eastmoney.com/")
    data = d.get("data")
    if not data:
        return None
    return len(data.get("pool") or [])


def market_breadth() -> dict[str, Any]:
    """市场情绪：涨跌家数 + 涨停/跌停家数 + 行业冷热榜。

    涨跌家数由东财行业板块每行业上涨/下跌家数汇总（单次请求，顺带行业冷热）。
    任一子项失败返回 None / 空，绝不抛异常（大盘研判可在缺情绪数据时降级）。
    """
    out: dict[str, Any] = {
        "advancers": None, "decliners": None,
        "limit_up": None, "limit_down": None,
        "top_industries": [], "bottom_industries": [],
    }
    # 1) 行业板块：汇总涨跌家数 + 冷热榜（单次东财请求）
    ind_params = urllib.parse.urlencode({
        "pn": 1, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:90+t:2", "fields": "f3,f14,f104,f105",
    })
    d = _em_json(f"https://push2.eastmoney.com/api/qt/clist/get?{ind_params}")
    diff = (d.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    if diff:
        out["advancers"] = sum(int(x.get("f104") or 0) for x in diff)
        out["decliners"] = sum(int(x.get("f105") or 0) for x in diff)
        ranked = [{"name": x.get("f14", ""), "chg_pct": x.get("f3")} for x in diff]
        out["top_industries"] = ranked[:5]
        out["bottom_industries"] = ranked[-5:][::-1]
    # 2) 涨停/跌停家数（东财 push2ex，按今日交易日）
    today = datetime.now().strftime("%Y%m%d")
    out["limit_up"] = _limit_pool_count("getTopicZTPool", "fbt:asc", today)
    out["limit_down"] = _limit_pool_count("getTopicDTPool", "fund:asc", today)
    return out
