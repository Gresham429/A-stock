"""持仓 portfolio.py 单测（零依赖离线）：lot 模型 + 现金扣减 + 盈亏三口径 + 逐笔当日盈亏。

**为什么重要**：现金扣减错了会让总资产把买股的钱算两次（分级错档、AI 拿到假的可用资金）；
盈亏三口径（毛 / 券商口径 pnl_broker / 净 pnl_net）是持仓面板与决策的依据。
用假 profile_store 隔离，只钉 portfolio 自己的数学。

跑：python3 tests/test_portfolio.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fees        # noqa: E402
import portfolio   # noqa: E402

_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


class _FakeProfileStore:
    """只实现 portfolio 用到的几个方法；捕获现金变动便于断言。"""
    def __init__(self, cash=100000.0, sched=None):
        self._cash = float(cash)
        self._sched = sched or fees.FeeSchedule()

    def get_active(self):
        return {"id": 1, "name": "test", "cash": self._cash}

    def get(self, pid):
        return {"id": int(pid), "name": "test", "cash": self._cash}

    def fee_schedule(self, pid=None):
        return self._sched

    def update(self, pid, **kw):
        if "cash" in kw:
            self._cash = float(kw["cash"])


def _setup(cash=100000.0, sched=None):
    d = tempfile.mkdtemp()
    portfolio.PORTFOLIO_PATH = Path(d) / "portfolio.json"
    fake = _FakeProfileStore(cash, sched)
    portfolio.profile_store = fake      # monkeypatch 隔离
    return fake


def test_add_appends_lots_weighted_avg():
    """同代码再 add = 追加一笔（不覆盖）；总股数=Σ、成本=加权平均。"""
    _setup()
    portfolio.add("600000", 100, 10.0, "2026-06-01")
    portfolio.add("600000", 300, 14.0, "2026-06-02")   # 加权 (100*10+300*14)/400 = 13.0
    h = portfolio.load()[0]
    ck(len(h["lots"]) == 2, "同代码应追加为 2 笔 lot，未覆盖")
    ck(h["shares"] == 400, f"总股数应 400，实得 {h['shares']}")
    ck(abs(h["cost_price"] - 13.0) < 1e-9, f"加权均价应 13.0，实得 {h['cost_price']}")


def test_add_deducts_cash_with_buy_fee():
    """买入按「成交额 + 买入费」扣可用现金（不扣会把钱算两次）。"""
    fake = _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0)     # 成交额1000，买入费=max(0.25,5)+0.01=5.01
    ck(abs(fake._cash - (100000 - 1005.01)) < 0.01, f"现金应扣成交额+买入费，实得 {fake._cash}")


def test_remove_with_price_adds_sell_net():
    """清仓给 sell_price → 现金加回「卖出额 − 卖出费」；不给则只删不动现金。"""
    fake = _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0)                  # 扣 1005.01 → 98994.99
    cash_after_buy = fake._cash
    portfolio.remove("600000", sell_price=12.0)         # 卖额1200，卖出费=5+0.6+0.01=5.61 → +1194.39
    ck(abs(fake._cash - (cash_after_buy + 1194.39)) < 0.01, f"卖出净收入回补错，实得 {fake._cash}")
    ck(portfolio.load() == [], "清仓后持仓应为空")
    # 不给价只删、不动现金
    fake2 = _setup(cash=50000.0)
    portfolio.add("600000", 100, 10.0)
    c = fake2._cash
    portfolio.remove("600000")     # 无 sell_price
    ck(fake2._cash == c, "无 sell_price 不应改现金")


def test_with_pnl_three_measures():
    """盈亏三口径：毛(pnl_amount) / 券商(pnl_broker=毛−已付买入费) / 净(pnl_net=毛−买入费−现卖费)。"""
    _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0, portfolio._today())    # 今日买
    rows = portfolio.with_pnl({"600000": {"name": "甲", "price": 12.0, "last_close": 11.0}})
    r = rows[0]
    ck(abs(r["cost_value"] - 1000) < 1e-9, "成本 1000")
    ck(abs(r["market_value"] - 1200) < 1e-9, "市值 1200")
    ck(abs(r["pnl_amount"] - 200) < 1e-9, "毛盈亏 200")
    ck(abs(r["pnl_broker"] - 194.99) < 0.01, f"券商口径应 200−5.01=194.99，实得 {r['pnl_broker']}")
    ck(abs(r["pnl_net"] - 189.38) < 0.01, f"净盈亏应 200−5.01−5.61=189.38，实得 {r['pnl_net']}")


def test_today_pnl_per_lot_basis():
    """当日盈亏逐笔：今日买的用买入价基准，之前持有的用昨收基准。"""
    _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0, portfolio._today())    # 今日：基准=10
    r = portfolio.with_pnl({"600000": {"name": "甲", "price": 12.0, "last_close": 11.0}})[0]
    ck(abs(r["today_pnl"] - 200) < 1e-9, f"今日买入基准=买入价10 → (12−10)*100=200，实得 {r['today_pnl']}")
    # 换成之前持有（date 非今日）→ 基准=昨收11 → (12−11)*100=100
    _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0, "2026-06-01")
    r2 = portfolio.with_pnl({"600000": {"name": "甲", "price": 12.0, "last_close": 11.0}})[0]
    ck(abs(r2["today_pnl"] - 100) < 1e-9, f"之前持有基准=昨收11 → 100，实得 {r2['today_pnl']}")


def test_cash_floor_at_zero():
    """现金下限 0：扣超也不为负。"""
    fake = _setup(cash=500.0)
    portfolio.add("600000", 100, 10.0)      # 要扣 1005 > 500
    ck(fake._cash == 0.0, f"现金应触底 0，实得 {fake._cash}")


def test_migrate_old_flat_holding():
    """旧扁平单笔 {code,shares,cost_price} → lots 模型。"""
    old = {"code": "600000", "shares": 100, "cost_price": 9.5, "buy_date": "2026-05-01"}
    m = portfolio._migrate_holding(old)
    ck("lots" in m and len(m["lots"]) == 1, "旧格式应迁成单 lot")
    ck(m["lots"][0]["shares"] == 100 and m["lots"][0]["cost"] == 9.5, "迁移字段错")


def test_summary_aggregates():
    """组合汇总：总市值/成本/毛/净。"""
    _setup(cash=100000.0)
    portfolio.add("600000", 100, 10.0, portfolio._today())
    rows = portfolio.with_pnl({"600000": {"name": "甲", "price": 12.0, "last_close": 11.0}})
    s = portfolio.summary(rows)
    ck(abs(s["market_value"] - 1200) < 1e-9 and abs(s["cost_value"] - 1000) < 1e-9, "汇总市值/成本错")
    ck(abs(s["pnl_amount"] - 200) < 1e-9 and s["count"] == 1, "汇总毛盈亏/数量错")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
    print(f"OK — test_portfolio 全过（{_n} 断言）")
