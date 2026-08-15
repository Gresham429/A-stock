"""复盘数据适配层：打板四池 + 题材串 + 龙虎榜。

数据源（实测 2026-08-15 可用，字段见各函数）：
- 东财 push2ex：涨停/炸板/跌停/昨日涨停 四池（date=YYYYMMDD）
- 同花顺：涨停原因题材串（date=YYYYMMDD）
- 东财 datacenter：全市场龙虎榜（date=YYYY-MM-DD，注意带横杠）

纯 urllib（不引 requests，与 A-stock 一致）。东财系走内置节流 `_em_get` 防封。
只取「已收盘定稿」口径：`resolve_trade_date` 逐日回探涨停池、命中即最近已收盘交易日。
"""
from __future__ import annotations

import datetime
import json
import logging
import random
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"
_EM_MIN_INTERVAL = 1.1          # 东财两次请求最小间隔(秒) + 抖动，防 IP 封
_last_em_call = [0.0]
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _get_json(url: str, params: dict, referer: str, throttle: bool = False,
              timeout: int = 15) -> Optional[dict]:
    """GET → JSON。throttle=True 时走东财节流。失败返回 None（调用方按需降级）。"""
    if throttle:
        wait = _EM_MIN_INTERVAL - (time.time() - _last_em_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.4))
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": _UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001 — 数据源不稳定，统一降级
        logger.warning("取数失败 %s: %s", url.split("/")[-1], e)
        return None
    finally:
        if throttle:
            _last_em_call[0] = time.time()


def _fmt_hms(t: Any) -> str:
    """封板时间整数 → 'HH:MM:SS'（92500 → '09:25:00'）。"""
    s = str(t or 0).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _seal_int(t: Any) -> int:
    """封板时间归一化为 6 位整数（092500），供 seal_quality 数值比较。"""
    try:
        return int(str(t or 0).zfill(6))
    except (ValueError, TypeError):
        return 0


def _zt_stat(p: dict) -> tuple[int, int]:
    """zttj = {'days':N,'ct':M} → (N天, M板)。用于还原「N天M板」有效高度。"""
    tj = p.get("zttj") or {}
    try:
        return int(tj.get("days") or 0), int(tj.get("ct") or 0)
    except (ValueError, TypeError):
        return 0, 0


# ── 东财 push2ex 打板四池 ────────────────────────────────────────────
def _em_pool(endpoint: str, sort: str, date: str) -> Optional[list[dict]]:
    """四池通用请求。返回 None=请求失败（区别于 []=非交易日/空池）。"""
    d = _get_json(f"https://push2ex.eastmoney.com/{endpoint}",
                  {"ut": _ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000,
                   "sort": sort, "date": date},
                  referer="https://quote.eastmoney.com/", throttle=True)
    if d is None:
        return None
    return (d.get("data") or {}).get("pool") or []


def zt_pool(date: str) -> Optional[list[dict]]:
    """涨停池。price 已 ÷1000；seal_fund 单位元；first/last_seal 为 HH:MM:SS 显示、
    *_int 为数值比较用；limit_days=连板数；stat_days/stat_ct=N天M板（还原有效高度）。"""
    raw = _em_pool("getTopicZTPool", "fbt:asc", date)
    if raw is None:
        return None
    out = []
    for p in raw:
        sd, sc = _zt_stat(p)
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000, "pct": round(p.get("zdp") or 0, 2),
            "amount": p.get("amount") or 0, "float_cap": p.get("ltsz") or 0,
            "turnover": round(p.get("hs") or 0, 2), "limit_days": p.get("lbc") or 0,
            "first_seal": _fmt_hms(p.get("fbt")), "first_seal_int": _seal_int(p.get("fbt")),
            "last_seal": _fmt_hms(p.get("lbt")), "last_seal_int": _seal_int(p.get("lbt")),
            "seal_fund": p.get("fund") or 0, "break_times": p.get("zbc") or 0,
            "industry": p.get("hybk", ""), "stat_days": sd, "stat_ct": sc,
        })
    return out


def zb_pool(date: str) -> Optional[list[dict]]:
    """炸板池（涨停后开板）。"""
    raw = _em_pool("getTopicZBPool", "fbt:asc", date)
    if raw is None:
        return None
    out = []
    for p in raw:
        sd, sc = _zt_stat(p)
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000, "limit_price": (p.get("ztp") or 0) / 1000,
            "pct": round(p.get("zdp") or 0, 2), "turnover": round(p.get("hs") or 0, 2),
            "first_seal": _fmt_hms(p.get("fbt")), "break_times": p.get("zbc") or 0,
            "amplitude": round(p.get("zf") or 0, 2), "speed": round(p.get("zs") or 0, 2),
            "industry": p.get("hybk", ""), "stat_days": sd, "stat_ct": sc,
        })
    return out


def dt_pool(date: str) -> Optional[list[dict]]:
    """跌停池。dt_days=连续跌停；open_times=开板次数。"""
    raw = _em_pool("getTopicDTPool", "fund:asc", date)
    if raw is None:
        return None
    return [{
        "code": p.get("c", ""), "name": p.get("n", ""),
        "price": (p.get("p") or 0) / 1000, "pct": round(p.get("zdp") or 0, 2),
        "turnover": round(p.get("hs") or 0, 2), "seal_fund": p.get("fund") or 0,
        "last_seal": _fmt_hms(p.get("lbt")), "dt_days": p.get("days") or 0,
        "open_times": p.get("oc") or 0, "industry": p.get("hybk", ""),
    } for p in raw]


def yzt_pool(date: str) -> Optional[list[dict]]:
    """昨日涨停池（定稿口径）：pct=今日涨幅，y_limit_days=昨连板。
    算晋级率/赚钱效应/连板溢价/亏钱效应/反馈矩阵的主来源。"""
    raw = _em_pool("getYesterdayZTPool", "zs:desc", date)
    if raw is None:
        return None
    out = []
    for p in raw:
        sd, sc = _zt_stat(p)
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000, "pct": round(p.get("zdp") or 0, 2),
            "turnover": round(p.get("hs") or 0, 2), "amplitude": round(p.get("zf") or 0, 2),
            "y_first_seal": _fmt_hms(p.get("yfbt")), "y_limit_days": p.get("ylbc") or 0,
            "industry": p.get("hybk", ""), "stat_days": sd, "stat_ct": sc,
        })
    return out


# ── 同花顺涨停原因题材串 ──────────────────────────────────────────────
def theme_reasons(date: str) -> Optional[list[dict]]:
    """同花顺涨停揭秘：reason=题材串（'+' 分隔），high_days=几天几板，
    seal_rate=封板率，first_time=首封 HH:MM:SS（源为 Unix 秒）。"""
    d = _get_json("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
                  {"page": 1, "limit": 200,
                   "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004",
                   "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0", "date": date},
                  referer="https://data.10jqka.com.cn/")
    if d is None:
        return None
    info = (d.get("data") or {}).get("info") or []
    out = []
    for it in info:
        ft = it.get("first_limit_up_time")
        try:
            first = datetime.datetime.fromtimestamp(int(ft)).strftime("%H:%M:%S") if ft else ""
        except (ValueError, TypeError, OSError):
            first = ""
        out.append({
            "code": it.get("code", ""), "name": it.get("name", ""),
            "price": it.get("latest"), "pct": it.get("change_rate"),
            "reason": it.get("reason_type", "") or "", "board_type": it.get("limit_up_type", ""),
            "seal_rate": it.get("limit_up_suc_rate"), "break_times": it.get("open_num") or 0,
            "high_days": it.get("high_days", ""), "first_time": first,
            "is_again": it.get("is_again_limit"),
        })
    return out


# ── 东财全市场龙虎榜（date 要 YYYY-MM-DD）─────────────────────────────
def dragon_tiger(date_dash: str) -> Optional[list[dict]]:
    """全市场龙虎榜。net_buy_wan=净买额(万元，源为元)。date_dash 必须带横杠。"""
    d = _get_json("https://datacenter-web.eastmoney.com/api/data/v1/get",
                  {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
                   "filter": f"(TRADE_DATE>='{date_dash}')(TRADE_DATE<='{date_dash}')",
                   "pageNumber": 1, "pageSize": 500, "sortColumns": "BILLBOARD_NET_AMT",
                   "sortTypes": -1, "source": "WEB", "client": "WEB"},
                  referer="https://data.eastmoney.com/", throttle=True)
    if d is None:
        return None
    rows = (d.get("result") or {}).get("data") or []
    return [{
        "code": r.get("SECURITY_CODE", ""), "name": r.get("SECURITY_NAME_ABBR", ""),
        "reason": r.get("EXPLANATION", ""),
        "net_buy_wan": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e4, 1),
        "change_pct": round(float(r.get("CHANGE_RATE") or 0), 2),
        "close": r.get("CLOSE_PRICE") or 0,
    } for r in rows]


# ── 交易日解析（只复盘已收盘定稿场次）────────────────────────────────
def to_dash(date_compact: str) -> str:
    """YYYYMMDD → YYYY-MM-DD。"""
    return f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"


def resolve_trade_date(date: Optional[str] = None, max_back: int = 12) -> Optional[str]:
    """解析目标复盘日（YYYYMMDD）。
    - date 指定：直接返回（YYYYMMDD 或 YYYY-MM-DD 都接受）。
    - date 为空：从今天起逐个工作日回探涨停池，命中非空即「最近已收盘交易日」。
    返回 None = 最近 max_back 个工作日都探不到（被封/网络/端点变更）。
    """
    if date:
        return date.replace("-", "")
    day = datetime.date.today()
    tried = 0
    while tried < max_back:
        if day.weekday() < 5:  # 跳过周末
            compact = day.strftime("%Y%m%d")
            raw = _em_pool("getTopicZTPool", "fbt:asc", compact)
            if raw:  # 非空 → 已收盘且有涨停数据
                return compact
            tried += 1
        day -= datetime.timedelta(days=1)
    logger.error("resolve_trade_date: 最近 %d 个工作日均探不到涨停池", max_back)
    return None
