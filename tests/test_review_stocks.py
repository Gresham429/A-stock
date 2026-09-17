"""复盘个股线索（零依赖、离线、不打网络）。

**为什么有这个文件**：这一层最容易出的错不是崩，而是
「AI 顺手给了买卖点」与「模型多编出一只数据里没有的股票」——
两者都会让复盘与子项目 B 的结论打架，或者出现一个用户查不到的标的。
这里把挑股的三路来源、白名单重建、以及「不给买卖点」钉死。

跑法：python3 tests/test_review_stocks.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review.stocks as st  # noqa: E402

N = [0]


def ck(cond, msg):
    N[0] += 1
    assert cond, msg


def zt(code, name, limit_days=1, seal=1e8, industry="", pct=10.0, turnover=5.0):
    return {"code": code, "name": name, "limit_days": limit_days, "seal_fund": seal,
            "industry": industry, "pct": pct, "turnover": turnover, "price": 10.0,
            "amount": 1e8, "float_cap": 50e8}


# ── 挑股 ──────────────────────────────────────────────────────────────────
def test_pick_three_sources_with_reasons():
    """三路来源都要出现，且每条都带得出理由（谁入选一眼能查）。"""
    stocks = [zt("600001", "板块股", industry="锂电池"),
              zt("600002", "连板股", limit_days=4),
              zt("600003", "普通涨停", limit_days=1, seal=1e7)]
    lhb = [{"code": "600004", "name": "龙虎榜股", "net_buy_wan": 8000.0, "change_pct": 6.0,
            "reason": "日涨幅偏离值达7%"}]
    sectors = [{"name": "锂电池", "main_net_yi": 12.3}, {"name": "别的板块", "main_net_yi": 5.0}]
    got = st.pick(stocks, lhb, sectors, limit=10)
    codes = [r["code"] for r in got]
    ck("600001" in codes, f"板块资金流成员应入选: {codes}")
    ck("600002" in codes, f"高连板应入选: {codes}")
    ck("600004" in codes, f"龙虎榜净买应入选: {codes}")
    ck(all(r.get("reason") for r in got), f"每条都要有理由: {got}")
    srcs = {r.get("source") for r in got}
    ck({"sector", "ladder", "lhb"} <= srcs, f"三路来源都要在: {srcs}")


def test_pick_dedupes_and_caps():
    """同一只被多路挑中只留一条（先到的理由优先），并遵守上限。"""
    stocks = [zt("600001", "双料股", limit_days=3, industry="锂电池")]
    sectors = [{"name": "锂电池", "main_net_yi": 9.9}]
    got = st.pick(stocks, [], sectors, limit=1)
    ck(len(got) == 1 and got[0]["code"] == "600001", f"应去重并受上限约束: {got}")
    ck("板块资金流入" in got[0]["reason"], f"先到的理由（板块）应保留: {got[0]['reason']}")


def test_pick_falls_back_to_sector_members():
    """板块名与涨停池的行业名对不上时，用板块成员兜底，不能整条来源丢失。"""
    import review.stocks as stocks_mod
    stocks_mod.universe_store.codes_of = lambda name: ["600009", "600010"]
    got = st.pick([], [], [{"name": "某概念", "main_net_yi": 3.0}], limit=5)
    ck([r["code"] for r in got] == ["600009", "600010"], f"应取板块成员兜底: {got}")
    ck(all(r["source"] == "sector" for r in got), "兜底来的也标来源")


def test_pick_empty_inputs():
    ck(st.pick([], [], []) == [], "空输入应返回空列表")
    ck(st.pick([], [{"code": "600005", "net_buy_wan": 100, "change_pct": -3.0}], []) == [],
       "龙虎榜只收当日上涨的")


# ── 输出契约 ──────────────────────────────────────────────────────────────
def test_analyze_whitelists_and_drops_price_levels():
    """模型多给的价位字段必须丢掉；多编的代码必须丢掉。"""
    rows = [{"code": "600001", "name": "某股", "primary": "电子", "sub": "元件",
             "price": 10.0, "reason": "3 连板"}]
    st.config.llm_enabled = lambda: True
    st.llm._chat = lambda *a, **k: ('{"items": ['
                                    '{"code": "600001", "name": "某股", "tagline": "为什么看",'
                                    ' "fundamentals": "主营", "financials": "同比转正",'
                                    ' "news": "当日无显著消息", "driver": "驱动", "risk": "风险",'
                                    ' "watch": "盯量能",'
                                    ' "entry_lo": 9.8, "entry_hi": 10.2, "target": 12.0, "buy": "建议买入"},'
                                    ' {"code": "999999", "name": "编的股", "tagline": "x"}]}')
    items = st.analyze("20260917", rows)
    ck(len(items) == 1, f"数据里没有的代码应丢弃: {items}")
    got = items[0]
    for bad in ("entry_lo", "entry_hi", "target", "buy"):
        ck(bad not in got, f"价位/买卖类字段不该落盘: {bad}")
    ck(got["code"] == "600001" and got["tagline"] == "为什么看", f"白名单字段要留住: {got}")
    ck(got["price"] == 10.0 and got["reason"] == "3 连板", "原始数据（价格、入选理由）要贴回")


def test_analyze_degrades_without_key_or_bad_shape():
    rows = [{"code": "600001", "name": "某股"}]
    st.config.llm_enabled = lambda: False
    ck(st.analyze("20260917", rows) == [], "未配 key 应返回空而不是抛")
    st.config.llm_enabled = lambda: True
    st.llm._chat = lambda *a, **k: '{"items": "不是数组"}'
    ck(st.analyze("20260917", rows) == [], "结构不对应返回空")
    st.llm._chat = lambda *a, **k: (_ for _ in ()).throw(st.llm.LLMError("boom"))
    ck(st.analyze("20260917", rows) == [], "调用失败应返回空（复盘本体不受影响）")


# ── 个人块落盘 ────────────────────────────────────────────────────────────
def test_watchlist_cached_per_day_and_saved_even_when_degraded():
    import userctx
    userctx.USERS_DIR = os.path.join(tempfile.mkdtemp(), "users")
    userctx.set_uid("u1")
    st.config.llm_enabled = lambda: True
    st.profile = lambda codes, lhb=None: {c: {"code": c, "name": c} for c in codes}
    calls = []

    def fake_analyze(date, rows):
        calls.append(len(rows))
        return [{"code": r["code"], "name": r["name"]} for r in rows]

    st.analyze = fake_analyze
    b1 = st.build_watchlist(["600001", "600002"], "20260917")
    ck(len(b1["items"]) == 2 and calls == [2], f"首次应生成: {b1}")
    b2 = st.build_watchlist(["600001"], "20260917")
    ck(calls == [2], f"同一天再调应命中缓存、不再烧 AI: {calls}")
    ck(b2["items"] == b1["items"], "缓存内容应一致")
    st.analyze = lambda date, rows: []          # AI 失败
    b3 = st.build_watchlist(["600001"], "20260917", force=True)
    ck(b3["degraded"] is True and st.load_user() is not None,
       "AI 失败也要落盘（当天算跑过，否则调度每个心跳重试）")


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
