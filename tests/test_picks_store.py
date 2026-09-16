"""tests/test_picks_store.py  观点账本：修改链、改口规则、到期、结果贴回、记忆块。"""
import concurrent.futures
import os, sys, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_store as ps

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

import news_store
# 节假日表在缓存缺失时会联网抓；测试一律按纯工作日算，零网络
news_store.is_trading_day = lambda d=None: (d or dt.date.today()).weekday() < 5

TMP = tempfile.mkdtemp()
ps.DB_PATHS["watchlist"] = os.path.join(TMP, "picks.db")
ps.DB_PATHS["public"] = os.path.join(TMP, "picks_public.db")
ps.init("watchlist"); ps.init("public")

def prop(**kw):
    base = {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy",
            "entry_lo": 9.5, "entry_hi": 9.8, "exit_lo": 10.8, "exit_hi": 11.2, "stop": 9.2,
            "thesis": "低吸", "trigger_note": "破 9.2 走", "basis_json": "{}",
            "decision": "new", "trigger": "none", "px_at_call": 9.9}
    base.update(kw)
    return base

def test_new_and_keep_chain():
    r1 = ps.apply("watchlist", prop(), "2026-09-16", "run1")
    ck(r1["decision"] == "new" and r1["status"] == "open", "首条为 new/open")
    r2 = ps.apply("watchlist", prop(decision="keep"), "2026-09-17", "run2")
    ck(r2["parent_id"] == r1["id"] and r2["decision"] == "keep", "keep 生成新行指向旧行")
    ck(len(ps.current("watchlist", "600519")) == 1, "只有一条 open")
    ck(ps.chain("watchlist", "600519")[0]["id"] == r2["id"], "chain 倒序最新在前")

def test_revise_without_trigger_downgraded():
    r = ps.apply("watchlist", prop(decision="revise", trigger="none", entry_lo=9.0, entry_hi=9.2), "2026-09-18", "run3")
    ck(r["decision"] == "keep", "无触发的 revise 降为 keep")
    ck(r["entry_lo"] == 9.5, "keep 沿用上一条的价位")

def test_revise_with_thesis_broken_allowed():
    r = ps.apply("watchlist", prop(decision="revise", trigger="thesis_broken", stance="watch", entry_lo=9.0, entry_hi=9.2), "2026-09-19", "run4")
    ck(r["decision"] == "revise" and r["entry_lo"] == 9.0 and r["stance"] == "watch", "有触发的 revise 生效")

def test_target_hit_requires_touched():
    r = ps.apply("watchlist", prop(decision="revise", trigger="target_hit", stance="sell"), "2026-09-20", "run5")
    ck(r["decision"] == "keep", "上一条 touched 不是 exit 时 target_hit 降为 keep")

def test_withdraw():
    # 上一条（2026-09-19 的 thesis_broken revise）把 valid_until 推到了 2026-09-25；
    # fix round 1 给 expired 触发加了「真过期才生效」的校验，这里改用 2026-09-26
    # （已过 valid_until）以保持 trigger="expired" 语义正确，而不是把日期定在有效期内。
    r = ps.apply("watchlist", prop(decision="withdraw", trigger="expired"), "2026-09-26", "run6")
    ck(r["status"] == "withdrawn", "撤销行状态 withdrawn")
    ck(ps.current("watchlist", "600519") == [], "撤销后无 open")

def test_new_with_prev_open_becomes_keep_unless_trigger():
    ps.apply("watchlist", prop(code="000001", name="平安银行"), "2026-09-16", "r")
    r = ps.apply("watchlist", prop(code="000001", name="平安银行", decision="new", entry_lo=1.0, entry_hi=1.1), "2026-09-17", "r")
    ck(r["decision"] == "keep" and r["entry_lo"] == 9.5, "已有 open 时 new 视为 keep")

def test_valid_until_counts_trading_days():
    ck(ps.valid_until("short", "2026-09-16") == "2026-09-23", "5 个交易日跨周末")
    ck(ps.valid_until("mid", "2026-09-16") > "2026-11-01", "40 个交易日约两个月")

def test_expire():
    ps.apply("public", prop(code="300001", name="特锐德"), "2026-01-05", "r")
    n = ps.expire("public", "2026-03-01")
    ck(n == 1 and ps.current("public", "300001") == [], "过期行标 expired")

def test_staple_outcome():
    ps.apply("watchlist", prop(code="002230", name="科大讯飞", entry_lo=9.5, entry_hi=9.8, exit_lo=10.8, exit_hi=11.2, stop=9.2, px_at_call=10.0), "2026-09-16", "r")
    bars = [{"date": "2026-09-16", "open": 10, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1},
            {"date": "2026-09-17", "open": 10, "high": 10.3, "low": 9.6, "close": 9.7, "volume": 1},
            {"date": "2026-09-18", "open": 9.7, "high": 11.0, "low": 9.6, "close": 10.9, "volume": 1}]
    n = ps.staple("watchlist", "002230", bars, "2026-09-18")
    row = ps.current("watchlist", "002230")[0]
    ck(n >= 1 and row["max_up"] == 10.0 and row["max_dn"] == -4.0, f"最高/最低相对给出价: {row['max_up']} {row['max_dn']}")
    ck(row["touched"] == "exit", "碰到卖点区间记 exit")

def test_memory_block_window_and_rollup():
    for i in range(15):
        ps.apply("watchlist", prop(code="600036", name="招商银行", decision="keep" if i else "new"), f"2026-09-{i+1:02d}", "r")
    blk = ps.memory_block("watchlist", "600036", 9.9, "2026-09-16")
    ck(blk.count("\n") <= 20, "近窗最多 12 条加标题")
    ck("招商银行" in blk and "keep" in blk or "维持" in blk, "含决定")
    old = ps.apply("watchlist", prop(code="601318", name="中国平安", entry_lo=9.5, entry_hi=9.8), "2025-01-10", "r")
    blk2 = ps.memory_block("watchlist", "601318", 9.6, "2026-09-16")
    ck("远期" in blk2 and "9.5" in blk2, "60 天外价位相近的记录被挑出")
    rl = ps.rollup("watchlist", "601318", "2026-07-18")
    ck(rl["n"] == 1, "远期汇总计数")

def test_staple_covers_withdrawn_rows():
    ps.apply("watchlist", prop(code="000858", name="五粮液", exit_lo=10.5), "2026-09-10", "r")
    ps.apply("watchlist", prop(code="000858", name="五粮液", decision="withdraw", trigger="thesis_broken"), "2026-09-11", "r")
    bars = [{"date": "2026-09-12", "open": 10.0, "high": 10.6, "low": 9.9, "close": 10.5, "volume": 1},
            {"date": "2026-09-13", "open": 10.5, "high": 11.0, "low": 10.4, "close": 10.9, "volume": 1}]
    n = ps.staple("watchlist", "000858", bars, "2026-09-13")
    rows = ps.chain("watchlist", "000858")
    ck(n == 2 and all(r["max_up"] is not None for r in rows), "撤销行也要被贴回结果，不能因 status 被跳过")
    withdrawn = [r for r in rows if r["status"] == "withdrawn"][0]
    ck(withdrawn["touched"] == "exit", "撤销行碰到卖点区间仍记 exit")

def test_expired_trigger_requires_past_valid_until():
    ps.apply("watchlist", prop(code="600030", name="中信证券"), "2026-09-16", "r")
    r1 = ps.apply("watchlist", prop(code="600030", name="中信证券", decision="revise", trigger="expired", stance="watch"), "2026-09-17", "r")
    ck(r1["decision"] == "keep", "有效期未到时 expired 触发被降为 keep")
    r2 = ps.apply("watchlist", prop(code="600030", name="中信证券", decision="revise", trigger="expired", stance="watch"), "2026-09-24", "r")
    ck(r2["decision"] == "revise", "有效期已过时 expired 触发生效")

def test_apply_single_open_row_under_threads():
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(ps.apply, "watchlist", prop(code="601988"), "2026-09-16", "r") for _ in range(8)]
        for f in futs:
            f.result()
    ck(len(ps.current("watchlist", "601988")) == 1, "并发 apply 只留一条 open 行")

def test_staple_ignores_zero_bars():
    ps.apply("watchlist", prop(code="600809", name="山西汾酒", entry_lo=9.5, entry_hi=9.8, exit_lo=10.8, exit_hi=11.2, stop=9.2, px_at_call=10.0), "2026-09-16", "r")
    bars = [
        {"date": "2026-09-16", "open": 10, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1},
        {"date": "2026-09-17", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},   # 停牌零价日
        {"date": "2026-09-18", "open": 9.8, "high": 9.9, "low": 9.6, "close": 9.7, "volume": 1},
    ]
    n = ps.staple("watchlist", "600809", bars, "2026-09-18")
    row = ps.current("watchlist", "600809")[0]
    ck(n >= 1 and row["touched"] != "stop", f"零价停牌日不该被当成碰到止损: {row}")
    ck(row["max_dn"] == -4.0, f"max_dn 应只从真实K线算，忽略零价日: {row['max_dn']}")

def test_withdraw_stop_hit_requires_touched():
    ps.apply("watchlist", prop(code="600887", name="伊利股份"), "2026-09-16", "r")
    r = ps.apply("watchlist", prop(code="600887", name="伊利股份", decision="withdraw", trigger="stop_hit"), "2026-09-17", "r")
    ck(r["decision"] == "keep", "withdraw+stop_hit 但上一条 touched 未记录到 stop 时应降为 keep")

def test_latest_run_per_horizon():
    ps.apply("public", prop(code="600000", name="浦发银行", horizon="short"), "2026-08-01", "d1")
    ps.apply("public", prop(code="600001", name="邯郸钢铁", horizon="short"), "2026-08-02", "d2")
    ps.apply("public", prop(code="600002", name="齐鲁石化", horizon="mid"), "2026-08-01", "d1")
    rows = ps.latest_run("public")
    hc = {(r["horizon"], r["code"]) for r in rows}
    ck(("short", "600001") in hc, "更新一天的短线行应在最新一轮里")
    ck(("short", "600000") not in hc, "旧一天的短线行不属于最新一轮")
    ck(("mid", "600002") in hc, "中线仍算它自己最新的一轮，即使日期比短线的更早")

def test_watchlist_horizon_switch_supersedes():
    r1 = ps.apply("watchlist", prop(code="601888", name="中国中免", horizon="short"), "2026-09-16", "r1")
    ck(r1["decision"] == "new" and r1["horizon"] == "short", "首条短线")
    r2 = ps.apply("watchlist", prop(code="601888", name="中国中免", horizon="mid", decision="revise",
                                    trigger="thesis_broken", entry_lo=9.0, entry_hi=9.2), "2026-09-17", "r2")
    ck(r2["decision"] == "revise" and r2["horizon"] == "mid" and r2["parent_id"] == r1["id"], "跨周期切换应替代旧行")
    open_rows = ps.current("watchlist", "601888")
    ck(len(open_rows) == 1 and open_rows[0]["horizon"] == "mid", "自选股同一只只留一条 open，且周期已切换")

def test_keep_preserves_prev_horizon():
    r1 = ps.apply("watchlist", prop(code="600350", name="山东威达", horizon="short"), "2026-09-16", "r1")
    ck(r1["decision"] == "new" and r1["horizon"] == "short", "首条短线")
    r2 = ps.apply("watchlist", prop(code="600350", name="山东威达", horizon="mid", decision="new",
                                    entry_lo=1.0, entry_hi=1.1), "2026-09-17", "r2")
    ck(r2["decision"] == "keep", "已有 open 时想换周期但没给触发，应降为 keep")
    ck(r2["horizon"] == "short", f"keep 行的 horizon 应沿用上一条，不该悄悄变成 proposed 想切的周期: {r2['horizon']}")
    ck(r2["entry_lo"] == r1["entry_lo"] and r2["entry_hi"] == r1["entry_hi"], "keep 行价位应沿用上一条")

if __name__ == "__main__":
    for fn in (test_new_and_keep_chain, test_revise_without_trigger_downgraded, test_revise_with_thesis_broken_allowed,
               test_target_hit_requires_touched, test_withdraw, test_new_with_prev_open_becomes_keep_unless_trigger,
               test_valid_until_counts_trading_days, test_expire, test_staple_outcome, test_memory_block_window_and_rollup,
               test_staple_covers_withdrawn_rows, test_expired_trigger_requires_past_valid_until,
               test_apply_single_open_row_under_threads, test_staple_ignores_zero_bars,
               test_withdraw_stop_hit_requires_touched, test_latest_run_per_horizon,
               test_watchlist_horizon_switch_supersedes, test_keep_preserves_prev_horizon):
        fn()
    print(f"OK — test_picks_store 全过（{N[0]} 断言）")
