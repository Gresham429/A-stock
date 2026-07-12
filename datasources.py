"""A股数据层：实时行情、波动/资金指标、研报、龙虎榜、解禁、资金流。

数据源优先级（不封IP优先）：
    行情/估值 -> 腾讯财经（GBK, HTTP）
    波动率/资金流 -> 新浪 MoneyFlow（含每日收盘价，一份数据两用）
    研报/龙虎榜/解禁 -> 东财 reportapi + datacenter（东财独有数据，走 em_get 限流）

所有东财请求通过 em_get() 串行限流，避免高频被封 IP。
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
import urllib.parse
import urllib.request
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
    """6 位代码 -> 交易所前缀。"""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith("8"):
        return "bj"
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
def tencent_quote(codes: list[str]) -> dict[str, dict[str, Any]]:
    """批量实时行情：价格/涨跌/PE/PB/市值/换手/振幅。返回 {code: {...}}。"""
    if not codes:
        return {}
    prefixed = [market_prefix(c) + c for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    try:
        data = _http_get(url, gbk=True, timeout=15)
    except OSError as e:
        logger.error("腾讯行情请求失败: %s", e)
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
    closes = [float(x["trade"]) for x in arr]
    r0net = [float(x.get("r0_net", 0)) for x in arr]  # 主力(超大单)净额，元
    dates = [x["opendate"] for x in arr]

    vol = _annualized_vol(closes, 20)
    cum20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 20 else None
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
    """近 n 日日对数收益率的年化波动率(%)。"""
    if len(closes) < n + 1:
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
