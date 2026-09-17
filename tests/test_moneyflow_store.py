"""资金流历史单测（零依赖离线，临时库，不打网络）。

钉住：解析东财 daykline 的字段位置、secid 市场映射、按 (date, code) 幂等、days/purge、
sync 的断点续传、单只失败按空处理（不抛、计入失败数）。

跑法：python3 tests/test_moneyflow_store.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import moneyflow_store as mf  # noqa: E402

_n = 0
FAKE = json.dumps({"data": {"klines": [
    "2026-09-16,-357538224.0,-60331.0,357598560.0,-274012544.0,-83525680.0,-11.02,-0.00,11.03,-8.45,-2.58",
    "2026-09-15,123456.0,1.0,2.0,3.0,4.0,5.5,0.1,0.2,0.3,0.4",
]}})


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        raise AssertionError(msg)


def setup() -> None:
    mf.DB_PATH = os.path.join(tempfile.mkdtemp(), "moneyflow.db")
    mf._inited = False
    mf.init()


def test_parse_fields_and_upsert():
    setup()
    mf.ds.em_get = lambda url: FAKE
    rows = mf.fetch_history("600519")
    ck(len(rows) == 2, f"应解析出两行: {rows}")
    ck(abs(rows[0]["main_net"] + 357538224.0) < 1e-6, f"主力净流入字段位置不对: {rows[0]}")
    ck(abs(rows[0]["main_pct"] + 11.02) < 1e-9, f"占比字段位置不对: {rows[0]}")
    ck(rows[0]["date"] == "2026-09-16", rows[0])
    mf.save_many("600519", rows)
    mf.save_many("600519", rows)
    seq = mf.of("600519")
    ck(len(seq) == 2, f"重复写不应产生重复行: {len(seq)}")
    ck(seq[0]["date"] == "2026-09-15", "应按日期升序返回")


def test_secid_market_mapping():
    ck(mf._secid("600519") == "1.600519", "沪市主板应走 1")
    ck(mf._secid("688981") == "1.688981", "科创板也在沪市，应走 1")
    for code in ("000001", "300750", "920000"):
        ck(mf._secid(code).startswith("0."), f"{code} 应走 0")


def test_days_and_purge():
    setup()
    mf.ds.em_get = lambda url: FAKE
    mf.save_many("600519", mf.fetch_history("600519"))
    ck(mf.days() == 2, mf.days())
    with mf._conn() as c:
        c.execute("INSERT INTO moneyflow_daily(date,code,main_net) VALUES('2023-01-03','600519',1)")
    ck(mf.days() == 3, "旧行也要计入天数")
    ck(mf.purge(days=730) == 1, "三年前那行应被清掉")
    ck(mf.days() == 2, "窗口内的数据必须保留")


def test_sync_resumable():
    setup()
    mf.ds.em_get = lambda url: FAKE
    codes = ["600001", "600002", "600003"]
    r1 = mf.sync(limit=2, codes=codes)
    ck(r1["ok"] and r1["done"] == 2, f"第一批应抓 2 只: {r1}")
    ck(mf.status()["synced_at"] == "", "没跑完一整轮不写 synced_at")
    r2 = mf.sync(codes=codes)
    ck(r2["ok"] and r2["done"] == 1, f"第二批应只抓剩下的 1 只: {r2}")
    ck(mf.status()["synced_at"] != "", "跑完一整轮应写 synced_at")
    ck(mf.status()["codes"] == 3 and mf.status()["days"] == 2, mf.status())


def test_fetch_failure_is_isolated():
    setup()

    def boom(url):
        raise OSError("模拟被限流")

    mf.ds.em_get = boom
    ck(mf.fetch_history("600519") == [], "抓取失败应返回空列表而不是抛异常")
    r = mf.sync(codes=["600001", "600002"])
    ck(r["ok"] and r["done"] == 2 and r["failed"] == 2, f"失败要计入统计: {r}")
    ck(mf.status()["rows"] == 0, "失败不应写脏数据")


def test_snapshot_falls_back_to_mirror_host():
    """主站 push2 连不通时（2026-09-17 实测：阿里云服务器 IP 被拒）要自动换镜像 push2delay。

    第一台返回空 data 视为不可用，第二台出数据就用它，否则服务器上的日频资金流快照永远是 0 行。
    """
    setup()
    seen = []

    def fake_em_get(url, ref="https://data.eastmoney.com/", timeout=20):
        seen.append(url.split("/api")[0])
        if mf._CLIST_HOSTS[1] not in url:
            return json.dumps({"data": None})
        return json.dumps({"data": {"total": 1, "diff": [{"f12": "600519", "f62": 100.0,
                                                          "f184": 5.0}]}})

    mf.ds.em_get = fake_em_get
    n = mf.snapshot("2026-09-16")
    ck(n == 1, f"备用主机应拿到 1 只: {n}")
    ck(mf._CLIST_HOSTS[1] in seen, f"未尝试备用主机: {seen}")
    ck(mf._CLIST_HOSTS[0] in seen[0], f"应先试主站: {seen}")


def test_snapshot_from_clist_pages():
    """clist 分页快照：两页拼全、按 (date, code) 落库、has() 幂等判定。"""
    setup()
    pages = {
        1: {"data": {"total": 3, "diff": [{"f12": "600519", "f62": 100.0, "f184": 5.0},
                                          {"f12": "000001", "f62": -50.0, "f184": -2.0}]}},
        2: {"data": {"total": 3, "diff": [{"f12": "300750", "f62": 20.0, "f184": 1.0}]}},
    }

    def fake_em_get(url):
        pn = int(url.split("pn=")[1].split("&")[0])
        return json.dumps(pages.get(pn, {"data": {"total": 3, "diff": []}}))

    mf.ds.em_get = fake_em_get
    n = mf.snapshot("2026-09-16")
    ck(n == 3, f"两页应拼出 3 只: {n}")
    ck(mf.has("2026-09-16") is True and mf.has("2026-09-15") is False, "has() 判定不对")
    seq = mf.of("600519")
    ck(len(seq) == 1 and abs(seq[0]["main_net"] - 100.0) < 1e-9, seq)
    mf.snapshot("2026-09-16")                       # 同日重跑：幂等，不新增行
    ck(mf.status()["rows"] == 3 and mf.status()["days"] == 1, mf.status())


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
