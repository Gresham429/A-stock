"""交易成本 fees.py 单测（零依赖离线）。

**为什么重要**：fees 是撮合(paper_store)、盈亏(portfolio)、AI 保本涨幅三处共用的单一事实源。
这里错一分钱，会顺着结算 → entries 超额 → 冻结分位 → journal/教训 一路污染 agent 学到的东西。
重点钉：`max(成交额×费率, 最低)` 的最低佣金分界、买/卖印花差异、往返保本涨幅。

跑：python3 tests/test_fees.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fees  # noqa: E402

_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def test_buy_vs_sell_stamp():
    """印花税**只在卖出**收（千0.5）；过户费双向（万0.1）。"""
    d = fees.FeeSchedule()
    b = fees.compute("buy", 10000, d)
    s = fees.compute("sell", 10000, d)
    ck(b["stamp"] == 0.0, "买入不应有印花税")
    ck(abs(s["stamp"] - 5.0) < 1e-9, f"卖出印花应为 10000×0.0005=5，实得 {s['stamp']}")
    ck(abs(b["transfer"] - 0.1) < 1e-9 and abs(s["transfer"] - 0.1) < 1e-9, "过户费双向万0.1")


def test_min_commission_gate():
    """佣金=max(成交额×费率, 最低)。低于分界抬到最低并标 hit_min。"""
    d = fees.FeeSchedule()   # 万2.5 + 最低5
    small = fees.compute("buy", 10000, d)   # raw=2.5 < 5 → 抬到5
    ck(small["commission"] == 5.0 and small["hit_min"] is True, f"小单未抬到最低5: {small}")
    big = fees.compute("buy", 30000, d)     # raw=7.5 > 5 → 线性
    ck(abs(big["commission"] - 7.5) < 1e-9 and big["hit_min"] is False, f"大单佣金应线性7.5: {big}")


def test_no_min_is_linear():
    """免最低（min=0）：佣金线性、hit_min 恒 False、分界点=0（拆单零惩罚）。"""
    d = fees.FeeSchedule(commission_rate=0.0009, min_commission=0.0)   # 万9免最低
    ck(d.has_min is False, "min=0 应 has_min=False")
    ck(d.min_amount_for_rate == 0.0, "免最低分界点应为0")
    c = fees.compute("buy", 2500, d)   # 2500×0.0009=2.25
    ck(abs(c["commission"] - 2.25) < 1e-9 and c["hit_min"] is False, f"免最低小单应线性2.25: {c}")


def test_breakpoint_matches_doc():
    """分界点=最低/费率。文档示例：万9+最低5 → 5556 元；默认 万2.5+5 → 20000 元。"""
    ck(fees.FeeSchedule(commission_rate=0.0009, min_commission=5).min_amount_for_rate == 5556,
       "万9+最低5 分界点应为 5556")
    ck(fees.FeeSchedule().min_amount_for_rate == 20000, "万2.5+5 分界点应为 20000")


def test_round_trip_and_breakeven():
    """往返=买费+卖费；保本涨幅=往返/成交额×100。"""
    d = fees.FeeSchedule()
    rt = fees.round_trip(10000, d)      # 买5.1 + 卖10.1 = 15.2 → 0.152%
    ck(abs(rt["buy"] - 5.1) < 1e-9 and abs(rt["sell"] - 10.1) < 1e-9, f"往返分项错: {rt}")
    ck(abs(rt["total"] - 15.2) < 1e-9 and abs(rt["pct"] - 0.152) < 1e-9, f"往返合计/占比错: {rt}")
    ck(abs(fees.breakeven_pct(10000, d) - 0.152) < 1e-9, "breakeven_pct 应等于往返 pct")


def test_zero_and_negative_amount():
    """成交额<=0 → 全零、不抛（停牌/脏数据兜底）。"""
    for amt in (0, -100):
        c = fees.compute("buy", amt)
        ck(c["total"] == 0.0 and c["hit_min"] is False, f"amount={amt} 应全零: {c}")


def test_from_row_defaults_and_parsing():
    """from_row：给定字段取用；缺字段/None 行/坏值 → 回落默认，不抛。"""
    s = fees.from_row({"commission_rate": 0.0009, "min_commission": 0})
    ck(abs(s.commission_rate - 0.0009) < 1e-12 and s.min_commission == 0, "from_row 未取到给定费率")
    d = fees.from_row(None)
    ck(d.commission_rate == fees.DEFAULT_COMMISSION_RATE, "None 行应回落默认")
    d2 = fees.from_row({})    # 缺全部键
    ck(d2.min_commission == fees.DEFAULT_MIN_COMMISSION, "缺键应回落默认")
    d3 = fees.from_row({"commission_rate": "坏值"})
    ck(d3.commission_rate == fees.DEFAULT_COMMISSION_RATE, "坏值应回落默认、不抛")


def test_lower_rate_not_always_cheaper():
    """文档核心结论：低费率不一定更省——取决于单笔金额（分界点两侧翻转）。"""
    cheap_min = fees.FeeSchedule(commission_rate=0.00025, min_commission=5)   # 万2.5+5
    low_free = fees.FeeSchedule(commission_rate=0.0009, min_commission=0)     # 万9免最低
    # 单笔 2500：免最低更省（2.25 < 5）
    ck(fees.compute("buy", 2500, low_free)["commission"] < fees.compute("buy", 2500, cheap_min)["commission"],
       "2500 元时免最低应更省")
    # 单笔 1 万：低费率更省（2.5→抬5 vs 9）… 实为 5 < 9
    ck(fees.compute("buy", 10000, cheap_min)["commission"] < fees.compute("buy", 10000, low_free)["commission"],
       "1 万元时低费率(触最低5)应比万9(9元)省")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
    print(f"OK — test_fees 全过（{_n} 断言）")
