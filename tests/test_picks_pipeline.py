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

def test_snapshot_failure_not_cached():
    setup_fakes()
    pp._snap_cache["ts"] = 0.0
    pp._snap_cache["rows"] = []
    calls = [0]
    def fake_snap():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")
        return [{"code": "603010", "name": "万盛股份", "price": 12, "turnover": 20, "amount": 5e8, "pe_ttm": 30, "pb": 3}]
    pp.ds.sina_all_stocks = fake_snap
    r1 = pp._snapshot()
    ck(r1 == [], f"取数失败应返回空表: {r1}")
    r2 = pp._snapshot()
    ck(len(r2) == 1 and calls[0] == 2, f"失败不应写入缓存、下一次要真正重试: {r2} calls={calls[0]}")

import datetime as dt
import json
import tempfile
import picks_store as ps
import llm_picks

def setup_store():
    tmp = tempfile.mkdtemp()
    ps.DB_PATHS["public"] = os.path.join(tmp, "pub.db"); ps.DB_PATHS["watchlist"] = os.path.join(tmp, "wl.db")
    ps.init("public"); ps.init("watchlist")
    pp.LOCK_DIR = tmp
    pp.news_store.is_trading_day = lambda d=None: True   # 零网络；valid_until 与 due_slot 都走这里

def test_run_public_persists_calls():
    setup_fakes(); setup_store()
    llm_picks.horizon_picks = lambda horizon, cands, levels, memory, mctx, cap: [
        {"code": cands[0]["code"], "name": cands[0]["name"], "horizon": horizon, "stance": "buy", "entry_lo": 9.8, "entry_hi": 10.0,
         "exit_lo": 10.5, "exit_hi": 10.8, "stop": 9.5, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n",
         "basis_json": "{}", "px_at_call": 10.0}]
    r = pp.run_public(horizons=("short", "mid"))
    ck(r["calls"] == 2 and r["error"] is None, f"两周期各落 1 条: {r}")
    ck(len(ps.current("public")) == 2, "账本 open 两条")

def test_run_watchlist_uses_user_watchlist():
    setup_fakes(); setup_store()
    pp.store.load_watchlist = lambda: ["603010", "300750"]
    pp.profile_store.get_active = lambda: {"cash": 5000}
    llm_picks.watchlist_points = lambda rows, levels, memory, mctx, cap: [
        {"code": r["code"], "name": r["name"], "horizon": "short", "stance": "watch", "entry_lo": 9.8, "entry_hi": 10.0,
         "exit_lo": 10.5, "exit_hi": 10.8, "stop": 9.5, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n",
         "basis_json": "{}", "px_at_call": 10.0} for r in rows]
    with pp.userctx.as_user("tester"):
        r = pp.run_watchlist()
    ck(r["calls"] == 2, f"自选股两条: {r}")

def test_run_watchlist_lock():
    setup_fakes(); setup_store()
    pp.store.load_watchlist = lambda: ["603010"]
    pp.profile_store.get_active = lambda: {"cash": 5000}
    called = [False]
    def wp(rows, levels, memory, mctx, cap):
        called[0] = True
        return []
    llm_picks.watchlist_points = wp
    with pp.userctx.as_user("lockuser"):
        lock_path = os.path.join(pp.userctx.user_dir(), ".picks-running")
        open(lock_path, "w").close()
        try:
            r = pp.run_watchlist()
            ck(r["error"] == "已在生成" and not called[0], f"个人锁存在时应直接返回、不调用 llm: {r}")
        finally:
            os.remove(lock_path)

def test_run_public_llm_failure_keeps_ledger():
    setup_fakes(); setup_store()
    llm_picks.horizon_picks = lambda *a, **k: []
    r = pp.run_public(horizons=("short",))
    ck(r["calls"] == 0 and ps.current("public") == [], "模型无返回时账本不动")

def test_lock_and_due():
    setup_store()
    ck(pp.acquire_lock("public") is True and pp.acquire_lock("public") is False, "锁互斥")
    pp.release_lock("public")
    ck(pp.acquire_lock("public") is True, "释放后可再拿"); pp.release_lock("public")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 16, 5), {}) == "full", "16:05 到点全量")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 16, 5), {"full": "2026-09-16"}) == "", "同日不重复")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 9, 10), {}) == "morning", "09:10 到点早盘")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 12, 0), {"morning": "2026-09-16"}) == "", "午间无事")

def test_run_public_isolates_horizon_failure():
    setup_fakes(); setup_store()
    def hp(horizon, cands, levels, memory, mctx, cap):
        if horizon == "mid":
            raise RuntimeError("boom")
        return [{"code": cands[0]["code"], "name": cands[0]["name"], "horizon": horizon, "stance": "buy", "entry_lo": 9.8, "entry_hi": 10.0,
                 "exit_lo": 10.5, "exit_hi": 10.8, "stop": 9.5, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n",
                 "basis_json": "{}", "px_at_call": 10.0}]
    llm_picks.horizon_picks = hp
    r = pp.run_public(horizons=("short", "mid"))
    ck(r["calls"] == 1, f"short 成功的一条应保留计数: {r}")
    ck(r["error"] is not None and "mid" in r["error"], f"error 应点名失败的周期: {r}")
    ck(len(ps.current("public")) == 1, "账本只留成功落库的那一条，不因 mid 失败被清空")

def test_loop_isolates_user_failure():
    setup_store()
    pp.due_slot = lambda now, last: "full"
    pp.run_public = lambda *a, **k: {"calls": 0, "error": None}
    done = []
    def rw(market_ctx=None, capital=None):
        uid = pp.userctx.get_uid()
        if uid == "a":
            raise RuntimeError("boom")
        done.append(uid)
        return {"calls": 0, "error": None}
    pp.run_watchlist = rw
    slot = pp.tick(lambda: ["a", "b"], None)
    ck(slot == "full", f"到点应跑 full: {slot}")
    ck(done == ["b"], f"a 失败不拖累 b，b 应正常跑到: {done}")
    ck(pp._last["full"] == pp._today(), "桶一旦开始就标记今天已处理（按上海时区，不用进程本地时区）")

def test_tick_survives_public_failure():
    setup_store()
    pp.due_slot = lambda now, last: "full"
    def rp(*a, **k):
        raise RuntimeError("boom")
    pp.run_public = rp
    done = []
    def rw(market_ctx=None, capital=None):
        done.append(pp.userctx.get_uid())
        return {"calls": 0, "error": None}
    pp.run_watchlist = rw
    slot = pp.tick(lambda: ["a", "b"], None)
    ck(slot == "full", f"公共失败仍应返回到点的 slot: {slot}")
    ck(done == ["a", "b"], f"公共失败不应跳过逐用户自选股循环: {done}")

def test_tick_market_ctx_under_fleet():
    setup_store()
    pp.due_slot = lambda now, last: "full"
    pp.userctx.set_fleet_uid("owner")
    seen_uid = []
    def mctx_fn():
        seen_uid.append(pp.userctx.get_uid())
        return {"regime": "x"}
    received = []
    def rp(mctx=None, horizons=("short", "mid", "long")):
        received.append(mctx)
        return {"calls": 0, "error": None}
    pp.run_public = rp
    pp.run_watchlist = lambda *a, **k: {"calls": 0, "error": None}
    pp.tick(lambda: [], mctx_fn)
    ck(seen_uid == ["owner"], f"market_ctx_fn 应在舰队站长上下文下调用，好让 _tier_block 读到画像: {seen_uid}")
    ck(received == [{"regime": "x"}], f"run_public 应收到 market_ctx_fn 返回的 dict: {received}")

def test_settle_staples_recent_expired():
    setup_fakes(); setup_store()
    today = dt.date.today()
    created = (today - dt.timedelta(days=10)).isoformat()
    ps.apply("public", {"code": "600809", "name": "山西汾酒", "horizon": "short", "stance": "buy",
                        "entry_lo": 9.5, "entry_hi": 9.8, "exit_lo": 10.8, "exit_hi": 11.2, "stop": 9.2,
                        "thesis": "t", "trigger_note": "n", "basis_json": "{}",
                        "decision": "new", "trigger": "none", "px_at_call": 10.0}, created, "r")
    d0 = dt.date.fromisoformat(created)
    future = [{"date": (d0 + dt.timedelta(days=i + 1)).isoformat(), "open": 10.0, "high": 10.2, "low": 9.8,
              "close": 10.0 + i * 0.01, "volume": 1} for i in range(15)]
    pp.ds.sina_kline = lambda code, num=40, scale=240: future
    r = pp.settle("public", today.isoformat())
    ck(r["expired"] == 1, f"10 天前建的短线行到今天该已过期: {r}")
    rows = ps.chain("public", "600809")
    ck(rows[0]["status"] == "expired" and rows[0]["max_up"] is not None,
       f"过期行也要被结果贴回，不能因为 expire() 先跑、current() 只看 open 而漏掉: {rows[0]}")

if __name__ == "__main__":
    for fn in (test_short_pool_prefers_theme_and_turnover, test_mid_pool_uses_sector_leaders_and_flow,
               test_long_pool_filters_by_valuation_and_growth, test_enrich_rows_and_levels,
               test_long_pool_survives_financial_errors, test_mid_pool_visits_all_ranked_sectors,
               test_enrich_drops_codes_without_quote, test_snapshot_cached,
               test_snapshot_failure_not_cached, test_run_public_persists_calls,
               test_run_watchlist_uses_user_watchlist, test_run_watchlist_lock,
               test_run_public_llm_failure_keeps_ledger,
               test_lock_and_due, test_run_public_isolates_horizon_failure,
               test_loop_isolates_user_failure, test_tick_survives_public_failure,
               test_tick_market_ctx_under_fleet, test_settle_staples_recent_expired):
        fn()
    print(f"OK — test_picks_pipeline 全过（{N[0]} 断言）")
