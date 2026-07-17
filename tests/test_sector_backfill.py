"""板块聚合 `_agg_sector_payload` 离线单测（零网络）。

live `snapshot_daily` 与历史回填 `backfill_sector_daily` 共用此函数，故它是「板块每日变化」
口径的唯一事实源。构造已知 snap+归属，逐字段校验：avg_chg / 涨跌家数 / 涨停家数（主板 9.7、
创业板科创 19.7 阈值）/ 领涨股 / price<=0 跳过 / 一股多板块。

跑：python3 tests/test_sector_backfill.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import universe_store as us  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _row(code, sector, kind):
    return {"code": code, "sector": sector, "kind": kind}


def _payload_by_sector(payload):
    # payload 元组列序：date,sector,kind,n,avg_chg,up_n,down_n,limit_up_n,amount,
    #                    leader_code,leader_name,leader_chg
    out = {}
    for t in payload:
        out[t[1]] = {"kind": t[2], "n": t[3], "avg_chg": t[4], "up_n": t[5],
                     "down_n": t[6], "limit_up_n": t[7], "amount": t[8],
                     "leader_code": t[9], "leader_name": t[10], "leader_chg": t[11]}
    return out


def test_basic_avg_and_breadth():
    snap = {
        "600000": {"chg_pct": 2.0, "amount": 1e8, "price": 10.0, "name": "甲"},
        "600001": {"chg_pct": -1.0, "amount": 2e8, "price": 20.0, "name": "乙"},
        "600002": {"chg_pct": 0.0, "amount": 3e8, "price": 30.0, "name": "丙"},
    }
    memb = [_row("600000", "银行", "sw1"), _row("600001", "银行", "sw1"),
            _row("600002", "银行", "sw1")]
    got = _payload_by_sector(us._agg_sector_payload("2026-01-05", snap, memb))
    b = got["银行"]
    check(b["n"] == 3, "n 应为 3")
    check(abs(b["avg_chg"] - round((2.0 - 1.0 + 0.0) / 3, 3)) < 1e-9, "avg_chg 均值错")
    check(b["up_n"] == 1 and b["down_n"] == 1, "涨跌家数错（0 不计涨也不计跌）")
    check(abs(b["amount"] - 6e8) < 1, "amount 应为成交额合计 6e8")
    check(b["leader_code"] == "600000" and b["leader_chg"] == 2.0, "领涨股应是涨幅最高者")
    check(b["kind"] == "sw1", "kind 透传错")


def test_limit_up_threshold_by_board():
    # 主板 9.7 阈值：+9.8 计涨停，+9.5 不计；创业板 19.7：+19.8 计，+19.5 不计
    snap = {
        "600100": {"chg_pct": 9.8, "amount": 1e8, "price": 10.0, "name": "主板涨停"},
        "600101": {"chg_pct": 9.5, "amount": 1e8, "price": 10.0, "name": "主板近涨停"},
        "300100": {"chg_pct": 19.8, "amount": 1e8, "price": 10.0, "name": "创板涨停"},
        "300101": {"chg_pct": 19.5, "amount": 1e8, "price": 10.0, "name": "创板近涨停"},
        "688100": {"chg_pct": 19.9, "amount": 1e8, "price": 10.0, "name": "科创涨停"},
    }
    memb = [_row(c, "测试板", "sub") for c in snap]
    b = _payload_by_sector(us._agg_sector_payload("2026-01-05", snap, memb))["测试板"]
    # 主板 600100(9.8✓) + 创业板 300100(19.8✓) + 科创 688100(19.9✓) = 3；
    # 600101(9.5✗)、300101(19.5✗，若误用主板9.7阈值会把它错判涨停) 不计
    check(b["limit_up_n"] == 3, f"涨停家数应为 3，实得 {b['limit_up_n']}（创业板须用 19.7 阈值）")


def test_skip_zero_price_and_missing():
    snap = {
        "600200": {"chg_pct": 5.0, "amount": 1e8, "price": 0.0, "name": "停牌"},  # price<=0 跳过
        "600201": {"chg_pct": 3.0, "amount": 1e8, "price": 10.0, "name": "正常"},
    }
    memb = [_row("600200", "钢铁", "sw1"), _row("600201", "钢铁", "sw1"),
            _row("600999", "钢铁", "sw1")]  # 600999 无快照 → 跳过
    b = _payload_by_sector(us._agg_sector_payload("2026-01-05", snap, memb))["钢铁"]
    check(b["n"] == 1, f"仅 1 只有效，实得 n={b['n']}")
    check(abs(b["avg_chg"] - 3.0) < 1e-9, "均值应只算有效股")


def test_stock_in_multiple_sectors():
    snap = {"600300": {"chg_pct": 4.0, "amount": 1e8, "price": 10.0, "name": "多板块股"}}
    memb = [_row("600300", "电子", "sw1"), _row("600300", "半导体", "sub")]
    got = _payload_by_sector(us._agg_sector_payload("2026-01-05", snap, memb))
    check("电子" in got and "半导体" in got, "一股应计入其所有板块")
    check(got["电子"]["avg_chg"] == 4.0 and got["半导体"]["avg_chg"] == 4.0, "两板块均含该股")


def test_empty_inputs():
    check(us._agg_sector_payload("2026-01-05", {}, []) == [], "空输入应返回空")
    check(us._agg_sector_payload("2026-01-05", {}, [_row("600000", "银行", "sw1")]) == [],
          "无快照命中应返回空（不产生 n=0 行）")


if __name__ == "__main__":
    test_basic_avg_and_breadth()
    test_limit_up_threshold_by_board()
    test_skip_zero_price_and_missing()
    test_stock_in_multiple_sectors()
    test_empty_inputs()
    print(f"OK — test_sector_backfill 全过（{_n} 断言）")
