"""模拟撮合 paper_store.py 单测（零依赖离线）：A股规则 + 现金/持仓一致性。

**为什么重要**：agent 全靠它成交。撮合错(涨跌停放行/T+1 漏判/现金不扣) → 成交价与持仓失真
→ entries 结算超额失真 → 教训学错。这里钉：整手、涨跌停封板、T+1、限价即时性、现金扣加。
显式传 sched 隔离 profile_store。

跑：python3 tests/test_paper_store.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fees          # noqa: E402
import paper_store   # noqa: E402

SCHED = fees.FeeSchedule()                         # 万2.5 + 最低5
Q = {"price": 10.0, "limit_up": 11.0, "limit_down": 9.0}
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _setup(cap=100000.0):
    paper_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "paper.db")
    paper_store.init()
    return paper_store.create_account("t", cap)


def _buy(aid, shares=100, q=Q, otype="market", price=0.0):
    return paper_store.order(aid, "600000", "甲", "buy", otype, price, shares, q, True, SCHED)


def test_buy_fills_deducts_cash_and_locks_t1():
    aid = _setup()
    r = _buy(aid)
    ck(r["ok"] and abs(r["fill"] - 10) < 1e-9, f"买入应成交@10: {r}")
    ck(abs(r["fee"] - 5.01) < 0.01, f"买入费应 5.01(触最低5+过户0.01): {r}")
    ck(abs(paper_store.get_account(aid)["cash"] - (100000 - 1005.01)) < 0.01, "现金未按成交额+费扣减")
    pos = paper_store.positions_of(aid)[0]
    ck(pos["shares"] == 100 and pos["sellable"] == 0, "当日买入应 T+1 锁定（sellable=0）")
    ck(abs(pos["avg_cost"] - 10.0501) < 1e-4, f"成本应含买入费=(1000+5.01)/100: {pos['avg_cost']}")


def test_reject_lot_size_and_nonpositive():
    aid = _setup()
    ck(not _buy(aid, 150)["ok"], "非整手应拒")
    ck(not _buy(aid, 0)["ok"], "股数<=0 应拒")


def test_reject_market_closed_and_bad_price():
    aid = _setup()
    ck(not paper_store.order(aid, "600000", "甲", "buy", "market", 0, 100, Q, False, SCHED)["ok"],
       "非交易时段应拒")
    ck(not paper_store.order(aid, "600000", "甲", "buy", "market", 0, 100,
                             {"price": 0}, True, SCHED)["ok"], "行情异常(价<=0)应拒")


def test_limit_up_blocks_buy():
    aid = _setup()
    zt = {"price": 11.0, "limit_up": 11.0, "limit_down": 9.0}
    r = _buy(aid, q=zt)
    ck(not r["ok"] and "涨停" in r["msg"], f"涨停封板应买不到: {r}")


def test_limit_price_not_touched_rejects():
    aid = _setup()
    r = _buy(aid, otype="limit", price=9.5)   # 限价买 9.5 < 现价 10 → 不即时触及
    ck(not r["ok"] and "限价" in r["msg"], f"限价未触及应拒(不挂单排队): {r}")
    r2 = _buy(aid, otype="limit", price=10.5)  # 限价买 10.5 >= 现价 → 按现价成交
    ck(r2["ok"] and abs(r2["fill"] - 10) < 1e-9, f"限价可成交应按现价10: {r2}")


def test_cash_insufficient_rejects():
    aid = _setup(cap=500.0)
    r = _buy(aid)     # 需 ~1005 > 500
    ck(not r["ok"] and "资金不足" in r["msg"], f"资金不足应拒: {r}")


def test_t1_blocks_same_day_sell():
    aid = _setup()
    _buy(aid)
    r = paper_store.order(aid, "600000", "甲", "sell", "market", 0, 100, Q, True, SCHED)
    ck(not r["ok"] and "可卖" in r["msg"], f"当日买入当日不可卖(T+1): {r}")


def test_settle_then_sell_adds_proceeds():
    aid = _setup()
    _buy(aid)
    with paper_store._conn() as c:     # 模拟次日：把锁定日改到过去 → _settle 会放开可卖
        c.execute("UPDATE positions SET lock_date=? WHERE account_id=?", ("2000-01-01", aid))
    cash0 = paper_store.get_account(aid)["cash"]
    sq = {"price": 12.0, "limit_up": 13.0, "limit_down": 11.0}
    r = paper_store.order(aid, "600000", "甲", "sell", "market", 0, 100, sq, True, SCHED)
    ck(r["ok"] and abs(r["fill"] - 12) < 1e-9, f"结算后应可卖@12: {r}")
    # 净收入 = 1200 − 卖出费(5+印花0.6+过户0.01=5.61) = 1194.39
    ck(abs((paper_store.get_account(aid)["cash"] - cash0) - 1194.39) < 0.01, "卖出净收入回补错")
    ck(paper_store.positions_of(aid) == [], "全卖后应清仓")


def test_sell_at_limit_down_blocked():
    aid = _setup()
    _buy(aid)
    with paper_store._conn() as c:
        c.execute("UPDATE positions SET lock_date=? WHERE account_id=?", ("2000-01-01", aid))
    dq = {"price": 9.0, "limit_up": 11.0, "limit_down": 9.0}   # 现价=跌停价
    r = paper_store.order(aid, "600000", "甲", "sell", "market", 0, 100, dq, True, SCHED)
    ck(not r["ok"] and "跌停" in r["msg"], f"跌停封板应卖不出: {r}")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
    print(f"OK — test_paper_store 全过（{_n} 断言）")
