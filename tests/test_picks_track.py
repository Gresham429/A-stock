"""候选名单前向超额追踪（零依赖、离线、不打网络）。

**为什么有这个文件**：第二层验收（设计第七节）要回答「选出来的名单有没有跑赢同层池子」。
它靠每天自动记录，口径错一次就悄悄攒一整年的错数据：日期错位、基准与名单不同层、
超额算成与全池比，都不会报错。这里用可控的假日K把日期与算术钉死。

跑法：python3 tests/test_picks_track.py
"""
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import picks_pipeline as pp  # noqa: E402
import picks_track as pt     # noqa: E402

N = [0]


def ck(cond, msg):
    N[0] += 1
    assert cond, msg


DATES = ([f"2026-01-{i:02d}" for i in range(1, 32)]
         + [f"2026-02-{i:02d}" for i in range(1, 29)])
REC = "2026-01-05"          # DATES 的下标 4
IDX = DATES.index(REC)


def flat_bars(n=DATES.__len__(), base=10.0, step=0.01):
    """单调上涨的假日K：日期与收盘价一一对应，前向收益可手算。"""
    return [{"date": d, "close": round(base * (1 + step * i), 4), "high": 0, "low": 0}
            for i, d in enumerate(DATES[:n])]


CUR = [len(DATES)]          # 假日K「当前」到第几根：记录时截到记录日，结算时放到最新


def setup(closes_by_code: dict[str, float]):
    """记录用的假环境：底池三层各两只、层内候选就是底池、日K按 `closes_by_code` 的斜率生成。

    `CUR[0]` 模拟「今天」：记录时必须是记录日当天（最后一根就是当天），结算时才放到最新，
    否则 entry 会取到未来价（这正是真实数据里最容易错位的地方）。
    """
    pt.DB_PATH = os.path.join(tempfile.mkdtemp(), "track.db")
    pt.init()
    base = []
    for layer, codes in (("large", ["L1", "L2"]), ("mid", ["M1", "M2"]), ("small", ["S1", "S2"])):
        for c in codes:
            base.append({"code": c, "layer": layer, "mcap_yi": 1000.0})
    pp._pool_base = lambda: list(base)
    pp._layered_candidates = lambda b, depth=40: list(b)
    slopes = {r["code"]: closes_by_code.get(r["code"], 0.01) for r in base}
    CUR[0] = len(DATES)
    pt.screening._safe_kline = lambda code, num=260: flat_bars(
        n=CUR[0], base=10.0, step=slopes.get(code, 0.01))


def settle_at(day: str):
    """结算前把假日K推到最新（真实里日K每天在长，测试里得手动放长）。"""
    CUR[0] = len(DATES)
    return pt.settle(today=day)


def test_record_writes_bench_and_selected():
    """基准一次性记全（每只候选一行），名单按周期记且 selected=1。"""
    setup({})
    CUR[0] = IDX + 1
    r = pt.record({"short": ["L1", "M1"], "long": ["S1"]}, today=REC)
    ck(r["bench"] == 6 and r["selected"] == 3, f"行数不对: {r}")
    with pt._conn() as c:
        n_bench = c.execute("SELECT COUNT(*) n FROM track WHERE cycle=?", (pt.BENCH_CYCLE,)).fetchone()["n"]
        n_short = c.execute("SELECT COUNT(*) n FROM track WHERE cycle='short'").fetchone()["n"]
        sel = c.execute("SELECT selected FROM track WHERE cycle='short' AND code='L1'").fetchone()["selected"]
    ck(n_bench == 6, f"基准应 6 行: {n_bench}")
    ck(n_short == 2, f"短线名单应 2 行: {n_short}")
    ck(sel == 1, "名单行 selected 应为 1")


def test_settle_computes_forward_and_layer_bench():
    """前向收益按交易日下标取、基准取同层中位数、超额 = 前向 − 同层基准中位数。"""
    setup({"M1": 0.02, "M2": 0.01, "L1": 0.03})       # 中盘两只斜率不同，大盘一只快
    CUR[0] = IDX + 1
    pt.record({"short": ["L1", "M1"]}, today=REC)
    r = settle_at("2026-02-20")
    ck(r["settled"] == 8, f"8 行都该结算（6 基准 + 2 名单）: {r}")
    with pt._conn() as c:
        row = dict(c.execute("SELECT * FROM track WHERE date=? AND cycle='short' AND code='M1'",
                             (REC,)).fetchone())
        large = dict(c.execute("SELECT * FROM track WHERE date=? AND cycle='short' AND code='L1'",
                               (REC,)).fetchone())
        bench_m = [dict(x) for x in c.execute(
            "SELECT fwd5 FROM track WHERE date=? AND layer='mid' AND cycle=?",
            (REC, pt.BENCH_CYCLE)).fetchall()]
    closes = [b["close"] for b in flat_bars(base=10.0, step=0.02)]
    expect = round((closes[IDX + 5] / closes[IDX] - 1) * 100, 4)
    ck(abs(row["fwd5"] - expect) < 1e-6, f"5 日前向收益算错: {row['fwd5']} vs {expect}")
    med = sorted(x["fwd5"] for x in bench_m)
    med = (med[0] + med[1]) / 2 if len(med) % 2 == 0 else med[len(med) // 2]
    ck(abs(row["bench5"] - med) < 1e-6, f"同层基准中位数算错: {row['bench5']} vs {med}")
    ck(abs(row["excess5"] - (row["fwd5"] - med)) < 1e-6, "超额应为前向减同层基准")
    ck(large["bench5"] is not None, "大盘层的基准也该填上")


def test_settle_is_idempotent_and_skips_young_rows():
    """当天记的行不结算（没到期），已结算的行不重复结算。"""
    setup({})
    CUR[0] = IDX + 1
    pt.record({"short": ["L1"]}, today=REC)
    r1 = pt.settle(today=REC)          # 当天：一行都不该结算
    ck(r1["settled"] == 0, f"当天不该结算: {r1}")
    r2 = settle_at("2026-02-20")
    ck(r2["settled"] == 7, f"6 基准加 1 名单: {r2}")
    r3 = settle_at("2026-02-20")
    ck(r3["settled"] == 0, f"重跑不该重复结算: {r3}")


def test_summary_reports_excess_by_cycle_and_layer():
    """汇总按周期与层分组，只统计名单行（基准行不进汇总）。"""
    setup({"L1": 0.03, "L2": 0.001})
    CUR[0] = IDX + 1
    pt.record({"short": ["L1"], "mid": ["L2"]}, today=REC)
    settle_at("2026-02-20")
    rows = pt.summary()
    keyed = {(r["cycle"], r["layer"]): r for r in rows}
    ck(("short", "large") in keyed, f"缺大盘短线一组: {rows}")
    ck(keyed[("short", "large")]["n"] == 1, f"样本数不对: {keyed[('short', 'large')]}")
    ck(keyed[("short", "large")]["excess5_mean"] > 0,
       f"斜率最快的那只在基准之上，超额应为正: {keyed[('short', 'large')]}")
    ck(all(r["cycle"] != pt.BENCH_CYCLE for r in rows), "基准行不该进汇总")


def test_purge_drops_old_rows_and_status_counts():
    """按日累积的表要能清干净：超过保留期的行删掉，状态反映首末日期。"""
    setup({})
    CUR[0] = IDX + 1
    pt.record({"short": ["L1"]}, today=REC)
    old = (dt.date.today() - dt.timedelta(days=900)).isoformat()
    with pt._conn() as c:      # 直接塞一行三年前的（record 自己会顺手清理，测不到 purge）
        c.execute("INSERT INTO track(date,cycle,code,layer,selected,entry) VALUES(?,?,?,?,1,10.0)",
                  (old, "short", "L1", "large"))
    before = pt.status()
    ck(before["rows"] == 8 and before["first_date"] == old, f"塞进旧行后应有 8 行: {before}")
    pt.purge(days=730)
    after = pt.status()
    ck(after["rows"] == 7, f"三年前那行应被清理: {after}")
    ck(after["first_date"] == REC, f"首日应回到记录日: {after}")


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
    print(f"\n{len(fns) - failed}/{len(fns)} 通过（{N[0]} 断言）")
    sys.exit(1 if failed else 0)
