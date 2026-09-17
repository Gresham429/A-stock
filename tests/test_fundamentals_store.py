"""财务面板单测（零依赖离线，临时库，不打网络）。

钉住四件事：
1. `save_many` 按 (code, period) upsert，重复抓同一期不产生重复行；
2. `sync` 分批断点续传：limit 跑一部分、cursor 前进、跑完一整轮才写 synced_at；
3. 心跳挡并发：上一轮还在跑时本次直接跳过；
4. 单只抓取失败不拖垮整批。

跑法：python3 tests/test_fundamentals_store.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fundamentals_store as fs  # noqa: E402

_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        raise AssertionError(msg)


def setup() -> None:
    fs.DB_PATH = os.path.join(tempfile.mkdtemp(), "fundamentals.db")
    fs._inited = False
    fs.init()


def fake_fin(code, periods=4):
    return [{"period": "2026-06-30", "revenue_yi": 100.0, "revenue_yoy": 12.5,
             "profit_yi": 20.0, "profit_yoy": 8.0},
            {"period": "2026-03-31", "revenue_yi": 90.0, "revenue_yoy": 10.0,
             "profit_yi": 18.0, "profit_yoy": 6.0}]


def test_save_is_idempotent():
    setup()
    fs.ds.financial_summary = fake_fin
    ck(fs.save_many("600519", fake_fin("600519")) == 2, "两期应写两行")
    fs.save_many("600519", fake_fin("600519"))
    rows = fs.of("600519")
    ck(len(rows) == 2, f"同两期重复写不应产生新行: {len(rows)}")
    ck(rows[0]["period"] == "2026-06-30" and rows[1]["period"] == "2026-03-31", "应按报告期新到旧")
    ck(abs(rows[0]["revenue_yoy"] - 12.5) < 1e-9, "字段应原样落库")
    st = fs.status()
    ck(st["rows"] == 2 and st["codes"] == 1, f"status 计数不对: {st}")
    ck(st["latest_period"] == "2026-06-30", st)


def test_sync_is_resumable():
    setup()
    fs.ds.financial_summary = fake_fin
    codes = ["600001", "600002", "600003"]
    r1 = fs.sync(limit=2, codes=codes)
    ck(r1["ok"] and r1["done"] == 2, f"第一批应抓 2 只: {r1}")
    ck(fs.status()["synced_at"] == "", "没跑完一整轮不该写 synced_at")
    ck(fs.status()["cursor"] == "2", f"游标应停在 2: {fs.status()}")
    r2 = fs.sync(codes=codes)                    # 接着跑剩下的一只
    ck(r2["ok"] and r2["done"] == 1, f"第二批应只抓剩下的 1 只: {r2}")
    ck(fs.status()["synced_at"] != "", "跑完一整轮应写 synced_at")
    ck(fs.status()["cursor"] == "0", f"跑完一轮游标回到 0: {fs.status()}")
    ck(fs.status()["codes"] == 3 and fs.status()["rows"] == 6, f"三只各两期: {fs.status()}")


def test_heartbeat_blocks_concurrent_sync():
    setup()
    fs.ds.financial_summary = fake_fin
    fs._set_meta("sync_hb", "9999999999")        # 模拟另一进程刚开了同步
    r = fs.sync(codes=["600001"])
    ck(r["ok"] is False and "还在跑" in r["msg"], f"心跳未过期应跳过: {r}")


def test_single_failure_does_not_break_batch():
    setup()

    def flaky(code, periods=4):
        if code == "600002":
            raise OSError("模拟网络失败")
        return fake_fin(code)

    fs.ds.financial_summary = flaky
    r = fs.sync(codes=["600001", "600002", "600003"])
    ck(r["ok"] and r["done"] == 3, f"失败也要计入已处理: {r}")
    ck(fs.of("600001") != [] and fs.of("600003") != [], "其余股票应正常落库")
    ck(fs.of("600002") == [], "失败的那只应没有数据且不抛异常")


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
