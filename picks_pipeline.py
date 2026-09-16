"""三周期选股流水线：候选池、富化、运行、结算、定时循环。

候选池都压到 POOL_N 只再喂模型（每周期一次提示词）。取数全部来自现有模块，
测试里整体 monkeypatch。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import datasources as ds
import picks_levels
import picks_store
import screening
import universe_store
import userctx
from review import store as review_store

logger = logging.getLogger(__name__)

POOL_N = 30
KLINE_N = 90
LONG_PREFILTER = 120
_SNAP_TTL = 600
_snap_cache: dict[str, Any] = {"ts": 0.0, "rows": []}


def _snapshot() -> list[dict[str, Any]]:
    """全市场快照，带 TTL 缓存——short_pool/long_pool 各调一次，别各拉一遍全市场。

    只缓存取数成功且非空的结果；失败或空结果直接返回空表、不写缓存，
    好让下一次调用立刻重试，不会把一次瞬时失败锁死成 10 分钟无候选池。
    """
    now = time.time()
    if now - _snap_cache["ts"] < _SNAP_TTL:
        return _snap_cache["rows"]
    try:
        rows = ds.sina_all_stocks() or []
    except Exception as e:  # noqa: BLE001 取数失败按空处理，池子退化不崩，不写缓存以便下次重试
        logger.warning("picks: 全市场快照失败: %s", e)
        return []
    if not rows:
        return []
    _snap_cache["ts"] = now
    _snap_cache["rows"] = rows
    return rows


def short_pool(n: int = POOL_N) -> list[str]:
    """短线：当天复盘的题材/涨停池优先，再补全市场换手率最高的。"""
    out: list[str] = []
    env = review_store.latest() or {}
    for r in (env.get("raw_theme") or []):
        c = str(r.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            return out[:n]
    snap = [s for s in _snapshot() if s.get("turnover") and s.get("amount")]
    snap.sort(key=lambda s: (float(s["turnover"]), float(s["amount"])), reverse=True)
    for s in snap:
        c = str(s.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            break
    return out[:n]


def mid_pool(n: int = POOL_N) -> list[str]:
    """中线：排名前列板块的龙头与成员，按主力 20 日净流入为正筛。"""
    out: list[str] = []
    for row in universe_store.sector_ranking(limit=10):
        lead = str(row.get("leader_code") or "")
        if lead and lead not in out:
            out.append(lead)
        for c in universe_store.codes_of(row.get("sector", ""))[:8]:
            if c not in out:
                out.append(c)
    if not out:
        return []
    m = screening._metrics_of(out)
    good = [c for c in out if (m.get(c) or {}).get("net20") is not None and m[c]["net20"] > 0]
    rest = [c for c in out if c not in good]
    return (good + rest)[:n]


def _fin_ok(code: str) -> bool:
    """营收、利润同比是否双正；单只财报失败按不合格处理，不拖垮整批并发取数。"""
    try:
        fin = ds.financial_summary(code) or []
    except Exception as e:  # noqa: BLE001 单只财报失败按不合格处理，不影响其余并发请求
        logger.warning("picks: %s 财报失败: %s", code, e)
        return False
    if not fin:
        return False
    f0 = fin[0]
    return (f0.get("revenue_yoy") or 0) > 0 and (f0.get("profit_yoy") or 0) > 0


def long_pool(n: int = POOL_N) -> list[str]:
    """长线：估值分位低（PE、PB 在快照里排前 LONG_PREFILTER）且营收、利润同比双正。

    财报逐只请求是网络调用，串行 120 次太慢，用小并发池并行取。
    """
    snap = [s for s in _snapshot() if s.get("pe_ttm") and s["pe_ttm"] > 0 and s.get("pb") and s["pb"] > 0]
    snap.sort(key=lambda s: (float(s["pe_ttm"]) * float(s["pb"])))
    cands = snap[:LONG_PREFILTER]
    if not cands:
        return []
    with ThreadPoolExecutor(max_workers=6) as ex:
        pairs = list(ex.map(lambda s: (s["code"], _fin_ok(s["code"])), cands))
    out = [str(code) for code, ok in pairs if ok]
    return out[:n]


def enrich(codes: list[str], short: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """行情 + 指标 + 板块 + 候选价位。缺行情的代码剔除，不进 rows/levels。"""
    if not codes:
        return [], {}
    quotes = ds.tencent_quote(codes)
    valid: list[str] = []
    dropped = 0
    for c in codes:
        q = quotes.get(c) or {}
        if q.get("price") is None:
            dropped += 1
            continue
        valid.append(c)
    if dropped:
        logger.warning("picks: %d 只无行情已剔除", dropped)
    if not valid:
        return [], {}
    metrics = screening._metrics_of(valid)
    rows: list[dict[str, Any]] = []
    levels: dict[str, dict] = {}
    for c in valid:
        q = quotes.get(c) or {}
        m = metrics.get(c) or {}
        try:
            primary, sub = universe_store.sector_of(c)
        except Exception:  # noqa: BLE001 归属缺失不影响选股
            primary, sub = "", ""
        rows.append({"code": c, "name": q.get("name", c), "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"), "range_pos": m.get("range_pos"),
                     "net20": m.get("net20"), "turnover": q.get("turnover"), "lot_cost": q.get("lot_cost")})
        try:
            levels[c] = picks_levels.candidate_levels(ds.sina_kline(c, KLINE_N), short=short)
        except Exception as e:  # noqa: BLE001 单只 K 线失败不拖垮整批
            logger.warning("picks: %s K线失败: %s", c, e)
            levels[c] = {"price": q.get("price"), "atr_pct": None, "levels": []}
    return rows, levels
