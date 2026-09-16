"""tests/test_picks_pipeline.py  候选池与富化：全部取数 monkeypatch。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_pipeline as pp

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

def bars(n=70, base=10.0):
    return [{"date": f"2026-01-{i+1:02d}", "open": base, "high": base * 1.02, "low": base * 0.98, "close": base + i * 0.01, "volume": 1} for i in range(n)]

def setup_fakes():
    pp._snap_cache["ts"] = 0.0
    pp.review_store.latest = lambda: {"raw_theme": [{"code": "603010", "name": "万盛股份", "pct": 10.0}, {"code": "300001", "name": "特锐德", "pct": 9.9}]}
    pp.ds.sina_all_stocks = lambda: [
        {"code": "603010", "name": "万盛股份", "price": 12, "turnover": 20, "amount": 5e8, "pe_ttm": 30, "pb": 3},
        {"code": "600000", "name": "浦发银行", "price": 8, "turnover": 0.5, "amount": 1e8, "pe_ttm": 5, "pb": 0.5},
        {"code": "000002", "name": "万科A", "price": 7, "turnover": 3, "amount": 3e8, "pe_ttm": 8, "pb": 0.7},
        {"code": "300750", "name": "宁德时代", "price": 200, "turnover": 6, "amount": 9e8, "pe_ttm": 25, "pb": 4}]
    pp.universe_store.sector_ranking = lambda date="", kind="", limit=30: [{"sector": "电池", "avg_chg": 3.1, "leader_code": "300750", "leader_name": "宁德时代"}]
    pp.universe_store.codes_of = lambda focus="", eligible_only=True: ["300750", "603010"] if focus == "电池" else ["603010", "600000", "000002", "300750"]
    pp.universe_store.sector_of = lambda code: ("电池", "锂电") if code == "300750" else ("化工", "阻燃")
    pp.screening._metrics_of = lambda codes: {c: {"vol": 30, "cum20": 2, "range_pos": 40, "net20": 1.0 if c == "300750" else -0.5} for c in codes}
    pp.ds.tencent_quote = lambda codes, workers=8: {c: {"name": "N" + c, "price": 10.0, "pe_ttm": 10, "pb": 1, "turnover": 2, "lot_cost": 1000} for c in codes}
    pp.ds.financial_summary = lambda code, periods=4: [{"period": "2026-06-30", "revenue_yoy": 12.0, "profit_yoy": 8.0}] if code != "600000" else [{"period": "2026-06-30", "revenue_yoy": -3.0, "profit_yoy": 1.0}]
    pp.ds.sina_kline = lambda code, num=120, scale=240: bars()

def test_short_pool_prefers_theme_and_turnover():
    setup_fakes()
    p = pp.short_pool(3)
    ck(p[0] in ("603010", "300001") and len(p) <= 3, f"题材池优先: {p}")
    ck("600000" not in p, "换手最低的不进短线池")

def test_mid_pool_uses_sector_leaders_and_flow():
    setup_fakes()
    p = pp.mid_pool(3)
    ck("300750" in p, f"板块龙头在中线池: {p}")

def test_long_pool_filters_by_valuation_and_growth():
    setup_fakes()
    p = pp.long_pool(3)
    ck("600000" not in p and "000002" in p, f"营收负增长被剔除、低估值双正保留: {p}")

def test_enrich_rows_and_levels():
    setup_fakes()
    rows, levels = pp.enrich(["603010", "300750"], short=True)
    ck(len(rows) == 2 and rows[0]["code"] == "603010" and rows[0]["lot_cost"] == 1000, "rows 字段齐")
    ck("603010" in levels and levels["603010"]["levels"], "每只有候选价位")
    ck(rows[0]["primary"] == "化工", "带板块归属")

def test_long_pool_survives_financial_errors():
    setup_fakes()
    def fin(code, periods=4):
        if code == "603010":
            raise RuntimeError("boom")
        if code == "600000":
            return [{"period": "2026-06-30", "revenue_yoy": -3.0, "profit_yoy": 1.0}]
        return [{"period": "2026-06-30", "revenue_yoy": 12.0, "profit_yoy": 8.0}]
    pp.ds.financial_summary = fin
    p = pp.long_pool(3)
    ck("603010" not in p, f"财报异常应被剔除: {p}")
    ck("000002" in p, f"财报正常双正应保留: {p}")

def test_mid_pool_visits_all_ranked_sectors():
    setup_fakes()
    sectors = [{"sector": f"S{i}", "avg_chg": 1.0, "leader_code": f"L{i:02d}", "leader_name": f"leader{i}"} for i in range(1, 11)]
    pp.universe_store.sector_ranking = lambda date="", kind="", limit=30: sectors
    pp.universe_store.codes_of = lambda focus="", eligible_only=True: [f"{focus}-{j}" for j in range(1, 9)]
    pp.screening._metrics_of = lambda codes: {c: {"net20": 1.0 if c == "L10" else -0.5} for c in codes}
    p = pp.mid_pool(30)
    ck("L10" in p, f"应遍历全部十个前列板块: {p}")

def test_enrich_drops_codes_without_quote():
    setup_fakes()
    pp.ds.tencent_quote = lambda codes, workers=8: {"603010": {"name": "N603010", "price": 10.0, "pe_ttm": 10, "pb": 1, "turnover": 2, "lot_cost": 1000}}
    rows, levels = pp.enrich(["603010", "300750"])
    ck(len(rows) == 1 and rows[0]["code"] == "603010", f"缺行情的应剔除: {rows}")
    ck(len(levels) == 1 and "603010" in levels, f"缺行情的不进 levels: {levels}")
    pp.ds.tencent_quote = lambda codes, workers=8: {}
    rows2, levels2 = pp.enrich(["603010", "300750"])
    ck(rows2 == [] and levels2 == {}, f"全部无行情应返回空: {rows2} {levels2}")

def test_snapshot_cached():
    setup_fakes()
    calls = [0]
    def fake_snap():
        calls[0] += 1
        return [{"code": "603010", "name": "万盛股份", "price": 12, "turnover": 20, "amount": 5e8, "pe_ttm": 30, "pb": 3}]
    pp.ds.sina_all_stocks = fake_snap
    pp._snapshot()
    pp._snapshot()
    ck(calls[0] == 1, f"TTL 内两次调用应只取一次快照: {calls[0]}")

if __name__ == "__main__":
    for fn in (test_short_pool_prefers_theme_and_turnover, test_mid_pool_uses_sector_leaders_and_flow,
               test_long_pool_filters_by_valuation_and_growth, test_enrich_rows_and_levels,
               test_long_pool_survives_financial_errors, test_mid_pool_visits_all_ranked_sectors,
               test_enrich_drops_codes_without_quote, test_snapshot_cached):
        fn()
    print(f"OK — test_picks_pipeline 全过（{N[0]} 断言）")
