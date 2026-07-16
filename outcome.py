"""结果结算 —— 把「买入时长得像错」换成「事后证明错」。

**为什么存在**：教训库 9 类里只有 `below_breakeven` 一类真的看了结果。
`chase_high` 的文案写着「>85 视为追高，**此后回落**」，但它在成交的**同一个循环**里
就记（`agent_loop.detect_failures`，传 `settled["filled"]`）——那时「此后」还没发生，
代码里从无一处检查过回落。这条教训在向 5 个 AI 陈述**从未被验证的结果**。

且系统自己横向矛盾：`factor_lab.HORIZONS=(5,10,20)`，选股的因子方向按未来 5–20 日
收益验证，教训却在 T+0 判罪。**选的时候看 20 天，判的时候看 0 天。**

本模块只做**结算数学**（纯函数、零网络、零 IO）；取数在 `datasources`，
留痕/落库在 `agent_store`，扫描在 `agent_loop`。

**红线：只算收益，不判罪。** 「超额多负算失败」是个阈值，按 PITFALLS#1 的战绩
（拍的方向被数据打脸 5 次）不能拍。判罪线要从 `factor_lab` 的历史分布里读——
那是 Phase 2，见 `plan/2026-07-16-outcome-driven-lessons-design.md`。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import factor_lab

logger = logging.getLogger(__name__)

# **必须与 factor_lab 对齐**：选股用什么地平线验证方向，教训就用什么地平线判罪。
# 不复制字面量而直接引用——两处各写一份，早晚会漂移成「选看20天、判看5天」。
HORIZONS: tuple[int, ...] = tuple(factor_lab.HORIZONS)


def _idx_of(bars: list[dict[str, Any]], date: str) -> int | None:
    """建仓日在 K 线里的位置。不存在返回 None —— **绝不取最近的一根凑数**。

    建仓日不在 K 线里意味着停牌或日期错，两种情况下猜出来的收益都是假的。
    """
    for i, b in enumerate(bars):
        if str(b.get("date", ""))[:10] == str(date)[:10]:
            return i
    return None


def _close(bars: list[dict[str, Any]], i: int) -> float | None:
    """第 i 根的收盘价；脏数据（0/None/非数值）返回 None，不抛。

    全市场池必踩：退市残留、停牌补 0、空字段（PITFALLS#11）。
    """
    if i < 0 or i >= len(bars):
        return None
    try:
        c = float(bars[i]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0 else None


def _rets_from(bars: list[dict[str, Any]], base_i: int, base_px: float,
               horizons: Iterable[int]) -> dict[int, float | None]:
    """自 base_i 起、按 **K 线根数**前推 h 根，相对 base_px 的收益(%)。

    **按根数不按日历天**：K 线只含交易日，索引 +5 天然就是 +5 个交易日。
    用日期加 5 天会在跨周末/长假时错位。
    """
    out: dict[int, float | None] = {}
    for h in horizons:
        c = _close(bars, base_i + h)
        out[h] = None if (c is None or base_px <= 0) else round((c / base_px - 1) * 100, 3)
    return out


def forward_returns(bars: list[dict[str, Any]], entry_date: str, entry_price: float,
                    horizons: Iterable[int] = HORIZONS) -> dict[int, float | None]:
    """个股绝对收益：自**真实成交价**起算，前推 5/10/20 个交易日。

    从 `entry_price` 而非建仓日收盘起算——判的是「这个决策」，不是「那天收盘买会怎样」。
    未到 h 个交易日 / 建仓日不在 K 线里 → 该项 None（**不外推**，编一个 r20 等于凭空造标签）。
    """
    hs = list(horizons)
    i = _idx_of(bars or [], entry_date)
    if i is None or entry_price <= 0:
        return {h: None for h in hs}
    return _rets_from(bars, i, float(entry_price), hs)


def horizon_end_dates(bars: list[dict[str, Any]], entry_date: str,
                      horizons: Iterable[int] = HORIZONS) -> dict[int, str | None]:
    """个股 +h 根 K 线**落在哪一天**。基准要按这个日期对齐，不能自己数 h 根。

    停牌会让个股的「+20 根」横跨比 20 个交易日更长的日历窗口。
    """
    hs = list(horizons)
    i = _idx_of(bars or [], entry_date)
    if i is None:
        return {h: None for h in hs}
    out: dict[int, str | None] = {}
    for h in hs:
        j = i + h
        out[h] = str(bars[j]["date"])[:10] if 0 <= j < len(bars) else None
    return out


def bench_returns(bars: list[dict[str, Any]], entry_date: str,
                  end_dates: dict[int, str | None]) -> dict[int, float | None]:
    """基准（大盘/板块）收益：建仓日收盘 → **个股 +h 根那一天**的收盘。

    **收的是终点日期而不是 horizons —— 这是刻意的**：让「两边各数 h 根」这种
    窗口错配在 API 上根本无法表达。踩过（2026-07-16 本模块首版）：个股停牌 5 天时
    拿 8 个交易日的个股收益减 3 个交易日的指数收益，**超额高估 10 个百分点且不报错**。

    基准没有「成交价」这回事，只能用收盘。与个股侧的成交价起点有 <1 个日内波动的
    口径差——二阶效应，**明确记下不假装没有**（见设计文档）。

    终点日在基准里缺失 → None，**不拿邻近日顶替**（顶替 = 悄悄换了窗口）。
    """
    i = _idx_of(bars or [], entry_date)
    base = _close(bars, i) if i is not None else None
    out: dict[int, float | None] = {}
    for h, d in end_dates.items():
        j = _idx_of(bars or [], d) if d else None
        c = _close(bars, j) if j is not None else None
        out[h] = None if (base is None or c is None) else round((c / base - 1) * 100, 3)
    return out


def excess(stock: dict[int, float | None],
           bench: dict[int, float | None]) -> dict[int, float | None]:
    """超额 = 个股 - 基准。**任一边缺失即 None。**

    缺基准时拿绝对收益顶替，等于把 beta 当成 AI 的能力——大盘跌 10% 的月份所有买入
    都亏，全记教训，AI 学到的是「别买」而非「别这样买」。那不是 AI 的错。
    （PITFALLS#5 的 beta 污染红线。）
    """
    out: dict[int, float | None] = {}
    for h in set(stock) | set(bench):
        s, b = stock.get(h), bench.get(h)
        out[h] = None if (s is None or b is None) else round(s - b, 3)
    return out
