"""tests/test_picks_levels.py  候选价位纯函数。python3 tests/test_picks_levels.py 直接跑。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_levels as pl

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

def mkbars(closes, spread=0.02):
    bars = []
    for i, c in enumerate(closes):
        bars.append({"date": f"2026-01-{i+1:02d}", "open": c, "high": round(c * (1 + spread), 2),
                     "low": round(c * (1 - spread), 2), "close": c, "volume": 1000})
    return bars

def test_too_short():
    r = pl.candidate_levels(mkbars([10.0] * 10))
    ck(r["levels"] == [] and r["price"] is None, "不足 20 根返回空")

def test_levels_contain_window_extremes_and_mas():
    closes = [10 + (i % 7) * 0.3 for i in range(70)]
    r = pl.candidate_levels(mkbars(closes))
    kinds = {l["kind"] for l in r["levels"]}
    for k in ("lo5", "hi5", "lo10", "hi10", "lo20", "hi20", "lo60", "hi60", "ma5", "ma20", "ma60", "atr_lo1", "atr_hi1"):
        ck(k in kinds, f"缺 {k}")
    ck(r["price"] == closes[-1], "price 是最后收盘")
    ck(all(l["px"] > 0 for l in r["levels"]), "价位都为正")
    ck(r["atr_pct"] > 0, "atr_pct 为正")

def test_swing_points():
    closes = [10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 8, 9, 10]
    lows, highs = pl.swing_points(mkbars(closes, spread=0.0), k=2)
    ck(9 in lows or 8 in lows, "找到摆动低点")
    ck(14 in highs or 12 in highs, "找到摆动高点")

def test_clusters_merge_within_tol():
    ck(pl.clusters([10.0, 10.05, 10.09, 11.0, 11.02]) == [10.05, 11.01] or len(pl.clusters([10.0, 10.05, 10.09, 11.0, 11.02])) == 2, "1% 内合并成两簇")
    ck(pl.clusters([]) == [], "空输入")

def test_short_adds_clusters():
    closes = [10, 9.5, 9.6, 10.4, 10.5, 9.55, 9.6, 10.45, 10.5, 9.5] * 3
    r = pl.candidate_levels(mkbars(closes, spread=0.0), short=True)
    kinds = {l["kind"] for l in r["levels"]}
    ck("cluster_lo" in kinds and "cluster_hi" in kinds, "短线加低点簇与高点簇")

def test_snap():
    levels = [{"px": 10.0, "kind": "lo20"}, {"px": 12.0, "kind": "hi20"}]
    px, adj = pl.snap(10.05, levels)
    ck(px == 10.05 and adj is False, "1% 内不调整")
    px, adj = pl.snap(10.5, levels)
    ck(px == 10.0 and adj is True, "1% 外吸附到最近候选")
    px, adj = pl.snap(11.9, levels)
    ck(px == 11.9 and adj is False, "接近 hi20 不调整")

def test_fmt():
    s = pl.fmt_levels({"price": 10.0, "atr_pct": 2.5, "levels": [{"px": 9.5, "kind": "lo20"}]})
    ck("lo20=9.5" in s and "现价 10.0" in s, "格式化含价位与现价")

def test_zero_padded_bars_ignored():
    closes = [10 + (i % 7) * 0.3 for i in range(25)]
    bars = mkbars(closes)
    last_close = closes[-1]
    bars.append({"date": "2026-02-01", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0})
    r = pl.candidate_levels(bars)
    ck(r["price"] == last_close, "停牌补0的尾部K线不改变现价")
    ck(all(l["px"] > 0 for l in r["levels"]), "候选价位全部为正，零价K线被过滤")
    ck(pl.candidate_levels([])["levels"] == [], "空输入返回空候选")
    ck(pl.snap(None, [{"px": 10.0, "kind": "lo20"}]) == (None, False), "px 为 None 时原样返回不调整")

if __name__ == "__main__":
    for fn in (test_too_short, test_levels_contain_window_extremes_and_mas, test_swing_points,
               test_clusters_merge_within_tol, test_short_adds_clusters, test_snap, test_fmt,
               test_zero_padded_bars_ignored):
        fn()
    print(f"OK — test_picks_levels 全过（{N[0]} 断言）")
