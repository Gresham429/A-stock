"""outcome.py 的结果结算数学（零依赖，离线，不打网络）。

**为什么有这个文件**：这里算的是**给 AI 贴的失败标签**。算错了不会报错，只会让系统
自信地把「对的做法」记成教训，再经 `_lesson_block()` 注入 5 个 AI（含用户自己用的
daily/screen/position/entry/market）。这正是 PITFALLS 第一类「不报错、只让你相信错结论」。

三条必须守住的线：
  1. **交易日 ≠ 日历日**：+5 交易日要按 K 线根数走，不能按日期加 5 天（跨周末/长假必错）。
  2. **必须扣 beta**：大盘跌的月份所有买入都亏，绝对收益会把 beta 记成 AI 的错
     （PITFALLS#5 的红线）。
  3. **数据不足返回 None，不外推**：未到 20 日就编一个 r20 出来，等于凭空造标签。

跑法：python3 tests/test_outcome.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome  # noqa: E402


def _bars(closes: list[float], start: date = date(2026, 1, 5)) -> list[dict]:
    """按收盘价造**只含交易日**的日K（跳过周末，时间正序）。

    刻意跳周末：用来证明结算走的是 K 线根数而不是日历天数。
    """
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:      # 跳过六日
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "open": c, "high": c * 1.01,
                    "low": c * 0.99, "close": c, "volume": 1e6})
        d += timedelta(days=1)
    return out


def test_forward_returns_counts_trading_days_not_calendar():
    """+5 必须是 5 **根K线**（交易日），不是日历 5 天 —— 跨周末必错。"""
    bars = _bars([100.0] + [101.0, 102.0, 103.0, 104.0, 105.0] + [110.0] * 20)
    r = outcome.forward_returns(bars, bars[0]["date"], 100.0, (5,))
    # 第 0 根是建仓日，+5 根 = 索引 5 = 收盘 105 → +5%
    assert r[5] == 5.0, f"+5 交易日收益错: {r[5]}（应 5.0，说明按日历天算了）"


def test_forward_returns_measured_from_entry_price_not_close():
    """个股收益从**真实成交价**起算 —— 判的是「这个决策」，不是「那天收盘买会怎样」。"""
    bars = _bars([100.0] + [110.0] * 25)
    r = outcome.forward_returns(bars, bars[0]["date"], 50.0, (5,))   # 成交价 50，收盘 100
    assert r[5] == 120.0, f"未从成交价起算: {r[5]}（应 (110/50-1)*100=120）"


def test_forward_returns_none_when_horizon_not_reached():
    """未到 N 交易日返回 None，不外推 —— 编一个 r20 等于凭空造标签。"""
    bars = _bars([100.0] * 8)         # 只有 8 根，够 5 不够 10/20
    r = outcome.forward_returns(bars, bars[0]["date"], 100.0, (5, 10, 20))
    assert r[5] is not None, "够 5 根却没算 r5"
    assert r[10] is None and r[20] is None, "数据不够却外推出了 r10/r20"


def test_forward_returns_none_when_entry_date_absent():
    """建仓日不在K线里（停牌/日期错）→ 全 None，绝不猜最近的一根。"""
    bars = _bars([100.0] * 30)
    r = outcome.forward_returns(bars, "2020-01-01", 100.0, (5,))
    assert r[5] is None, "建仓日不存在却算出了收益"


def test_bench_returns_measured_from_close():
    """基准（大盘/板块）从建仓日**收盘**起算 —— 基准没有「成交价」这回事。"""
    bars = _bars([3000.0] + [3060.0] * 25)
    r = outcome.bench_returns(bars, bars[0]["date"], (5,))
    assert r[5] == 2.0, f"基准收益错: {r[5]}（应 (3060/3000-1)*100=2.0）"


def test_excess_strips_beta():
    """超额 = 个股 - 基准。大盘跌 10% 时个股只跌 2% → 超额 +8%，**不该记成失败**。

    这是本模块存在的理由：绝对收益会把 beta 记到 AI 头上（PITFALLS#5 红线）。
    """
    x = outcome.excess({5: -2.0, 10: None, 20: 3.0}, {5: -10.0, 10: -8.0, 20: 1.0})
    assert x[5] == 8.0, f"超额算错: {x[5]}（个股-2% - 大盘-10% = +8%）"
    assert x[20] == 2.0, f"超额算错: {x[20]}"


def test_excess_none_when_either_side_missing():
    """任一边缺失 → 超额 None。缺基准时拿绝对收益顶替 = 把 beta 当成能力。"""
    x = outcome.excess({5: -2.0}, {5: None})
    assert x[5] is None, "基准缺失却给出了超额（等于悄悄退化成绝对收益）"
    x2 = outcome.excess({5: None}, {5: -10.0})
    assert x2[5] is None, "个股收益缺失却给出了超额"


def test_decay_visible_short_negative_long_positive():
    """必须能表达「x5<0 但 x20>0」= **入场早了，不是选错股**。

    这正是「只看短期」会误判的那类 —— 用户提的问题的靶心。
    """
    bars = _bars([100.0, 95.0, 94.0, 96.0, 97.0, 98.0] + [99.0] * 4 + [120.0] * 15)
    r = outcome.forward_returns(bars, bars[0]["date"], 100.0, (5, 20))
    assert r[5] < 0, f"r5 应为负: {r[5]}"
    assert r[20] > 0, f"r20 应为正: {r[20]}"


def test_survives_dirty_bars():
    """含 0 收盘的脏K线不得抛异常（全市场池必踩，PITFALLS#11）。"""
    bars = _bars([100.0] * 30)
    bars[3]["close"] = 0.0
    bars[7]["close"] = None
    try:
        outcome.forward_returns(bars, bars[0]["date"], 100.0, (5, 20))
        outcome.bench_returns(bars, bars[0]["date"], (5,))
    except Exception as e:  # noqa: BLE001 —— 就是要证明它不抛
        raise AssertionError(f"脏数据穿透: {type(e).__name__}: {e}") from e


def test_horizons_match_factor_lab():
    """地平线必须与 factor_lab 对齐 —— 选股用什么地平线验证，教训就用什么判罪。

    否则回到老毛病：选的时候看 20 天，判的时候看 0 天。
    """
    import factor_lab
    assert tuple(outcome.HORIZONS) == tuple(factor_lab.HORIZONS), (
        f"地平线不一致: outcome={outcome.HORIZONS} vs factor_lab={factor_lab.HORIZONS}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
