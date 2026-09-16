"""候选价位：从日 K 算出买点/卖点/止损可以落在哪些价上（纯函数，零网络）。

模型不报无来源的价格：它只能从这里算出的候选里挑，返回后再用 snap() 校验。
候选来源：近 5/10/20/60 日高低、MA5/20/60、摆动高低点、ATR 上下带；
短线另加近 10 日低点簇/高点簇（相距 1% 内合并）。
"""
from __future__ import annotations

import logging
from typing import Any

import structure

logger = logging.getLogger(__name__)

MIN_BARS = 20
ATR_N = 20
SNAP_TOL = 0.01
CLUSTER_TOL = 0.01


def _clean(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for b in bars or []:
        try:
            high, low, close = float(b["high"]), float(b["low"]), float(b["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(high, low, close) <= 0:
            continue
        out.append({"date": b.get("date"), "high": high, "low": low, "close": close})
    return out


def swing_points(bars: list[dict[str, Any]], k: int = 2) -> tuple[list[float], list[float]]:
    """局部极值：左右各 k 根都不更低/更高。返回 (lows, highs)。"""
    rows = _clean(bars)
    lows: list[float] = []
    highs: list[float] = []
    for i in range(k, len(rows) - k):
        lo = rows[i]["low"]
        hi = rows[i]["high"]
        if all(lo <= rows[j]["low"] for j in range(i - k, i + k + 1)):
            lows.append(round(lo, 2))
        if all(hi >= rows[j]["high"] for j in range(i - k, i + k + 1)):
            highs.append(round(hi, 2))
    return lows, highs


def clusters(prices: list[float], tol: float = CLUSTER_TOL) -> list[float]:
    """相距 tol 以内的价合并成一簇，返回各簇均值（升序）。"""
    ps = sorted(float(p) for p in prices if p)
    if not ps:
        return []
    groups: list[list[float]] = [[ps[0]]]
    for p in ps[1:]:
        if p - groups[-1][-1] <= groups[-1][-1] * tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [round(sum(g) / len(g), 2) for g in groups]


def _atr_pct(rows: list[dict[str, Any]], n: int = ATR_N) -> float:
    win = rows[-n:]
    if not win:
        return 0.0
    return round(sum((r["high"] - r["low"]) / r["close"] for r in win if r["close"]) / len(win) * 100, 2)


def candidate_levels(bars: list[dict[str, Any]], short: bool = False) -> dict[str, Any]:
    """候选价位表。bars 不足 20 根返回空表。"""
    rows = _clean(bars)
    if len(rows) < MIN_BARS:
        return {"price": None, "atr_pct": None, "levels": []}
    price = rows[-1]["close"]
    levels: list[dict[str, Any]] = []
    for n in (5, 10, 20, 60):
        win = rows[-n:]
        levels.append({"px": round(min(r["low"] for r in win), 2), "kind": f"lo{n}"})
        levels.append({"px": round(max(r["high"] for r in win), 2), "kind": f"hi{n}"})
    d = structure.digest(bars) or {}
    for k in ("ma5", "ma20", "ma60"):
        if d.get(k):
            levels.append({"px": round(float(d[k]), 2), "kind": k})
    lows, highs = swing_points(rows)
    for px in lows[-3:]:
        levels.append({"px": px, "kind": "swing_lo"})
    for px in highs[-3:]:
        levels.append({"px": px, "kind": "swing_hi"})
    atr = _atr_pct(rows)
    for mult in (1, 2):
        levels.append({"px": round(price * (1 - atr / 100 * mult), 2), "kind": f"atr_lo{mult}"})
        levels.append({"px": round(price * (1 + atr / 100 * mult), 2), "kind": f"atr_hi{mult}"})
    if short:
        win = rows[-10:]
        for px in clusters([r["low"] for r in win]):
            levels.append({"px": px, "kind": "cluster_lo"})
        for px in clusters([r["high"] for r in win]):
            levels.append({"px": px, "kind": "cluster_hi"})
    return {"price": price, "atr_pct": atr, "levels": levels}


def snap(px: float, levels: list[dict[str, Any]], tol: float = SNAP_TOL) -> tuple[float, bool]:
    """px 落在某候选 ±tol 内则原样返回；否则吸附到最近候选并标记调整。"""
    if not levels or px is None:
        return px, False
    nearest = min(levels, key=lambda l: abs(l["px"] - px))
    if abs(nearest["px"] - px) <= nearest["px"] * tol:
        return px, False
    return nearest["px"], True


def fmt_levels(cl: dict[str, Any]) -> str:
    """一行文本给提示词：现价 + 各候选。"""
    if not cl.get("levels"):
        return "无K线，候选价位不可用"
    parts = [f"{l['kind']}={l['px']}" for l in cl["levels"]]
    return f"现价 {cl['price']}  近20日振幅均值 {cl['atr_pct']}%  候选价位: " + " ".join(parts)
