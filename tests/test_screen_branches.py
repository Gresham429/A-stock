"""_screen_rows 三条分支的存在性回归（零依赖，离线，不打网络）。

**为什么有这个文件**：`_balanced_pick` 曾被一次盲切片替换整个删除
（`s[:idx(_pa_score)] + new + s[idx(_screen_rows):]`——它正好夹在两者之间），
而当时的测试只跑了「关注板块」分支（走形态排序，不调 `_balanced_pick`），
于是 NameError 一路潜伏到 agent 跑全市场路径才炸。

教训：`_screen_rows` 有**三条互斥分支**，测一条不代表另两条活着。
本文件只做**离线的存在性与契约检查**——真实取数走冒烟测试。

跑法：python3 tests/test_screen_branches.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def test_screen_helpers_exist():
    """三条分支各自依赖的函数都必须在 —— 防盲切片再删掉谁。"""
    for fn in ("_pa_score", "_balanced_pick", "_screen_rows", "_metrics_of", "_safe_metrics"):
        assert callable(getattr(app, fn, None)), f"app.{fn} 不存在或不可调用"


def test_balanced_pick_respects_caps():
    """均衡采样：总量 ≤ cap_total、每个细分 ≤ cap_per_sub、跨一级轮询。"""
    smap = {f"{i:06d}": (f"一级{i % 4}", f"细分{i % 8}") for i in range(60)}
    codes = list(smap)
    picked = app._balanced_pick(codes, 12, 2, smap)
    assert len(picked) <= 12, f"总量超限: {len(picked)}"
    subs: dict[str, int] = {}
    for c in picked:
        subs[smap[c][1]] = subs.get(smap[c][1], 0) + 1
    assert all(v <= 2 for v in subs.values()), f"单细分超限: {subs}"
    assert len(picked) == len(set(picked)), "有重复"


def test_balanced_pick_spreads_across_primaries():
    """同一一级不应霸占全部名额（轮询的意义）。"""
    smap = {f"{i:06d}": ("大板块" if i < 50 else "小板块", f"细分{i % 20}") for i in range(60)}
    picked = app._balanced_pick(list(smap), 10, 5, smap)
    primaries = {smap[c][0] for c in picked}
    assert len(primaries) == 2, f"未跨一级轮询: {primaries}"


def test_balanced_pick_empty_and_small():
    assert app._balanced_pick([], 10, 3, {}) == []
    smap = {"000001": ("银行", "股份制银行")}
    assert app._balanced_pick(["000001"], 10, 3, smap) == ["000001"]


def test_pa_score_guards():
    """vol 缺失 / 超出硬门 → None（该股出局），不该抛异常。"""
    assert app._pa_score({"vol": None}) is None
    assert app._pa_score({"vol": app.VOL_FLOOR - 1}) is None
    assert app._pa_score({"vol": app.VOL_CEIL + 1}) is None
    s = app._pa_score({"vol": 50, "cum20": 5, "range_pos": 40})
    assert s is None or 0 <= s <= 100, f"分数越界: {s}"


def test_pa_score_respects_cohort_dirs():
    """cohort 修复核心：打分方向跟传入 dirs 走。range_pos 高时，−1(反转)给低分、+1(动量)给高分。

    这正是「大盘池 range_pos=+1」修复的效果——同一只近高点大盘股不再被反转方向判低分。
    """
    m = {"vol": 50, "cum20": 5, "range_pos": 90}          # 近区间高点
    neutral = {"vol": {"sign": 0}, "cum20": {"sign": 0}}  # 只让 range_pos 生效
    s_rev = app._pa_score(m, {**neutral, "range_pos": {"sign": -1}})
    s_mom = app._pa_score(m, {**neutral, "range_pos": {"sign": +1}})
    assert s_rev is not None and s_mom is not None
    assert s_mom > s_rev, f"方向未跟 dirs 走：动量应给近高点更高分 (mom={s_mom} rev={s_rev})"
    assert abs(s_rev - 10.0) < 1e-6 and abs(s_mom - 90.0) < 1e-6, f"分值不对: rev={s_rev} mom={s_mom}"


def test_pa_score_default_dirs_backward_compat():
    """不传 dirs → 退回 factor_lab.directions()（向后兼容、不抛）。"""
    s = app._pa_score({"vol": 50, "cum20": 5, "range_pos": 40})
    assert s is None or 0 <= s <= 100


# ── 分层候选池（2026-09-17 重构）：三条分支用假数据实跑 ────────────────────────
class _FakeEnv:
    """把 _screen_rows 的外部依赖换成假数据，跑通分支逻辑（不打网络、不碰 db）。"""

    def __init__(self, quotes, smap, layers_of):
        self.quotes, self.smap, self.layers_of = quotes, smap, layers_of
        self.saved = {}

    def __enter__(self):
        import screening
        self.s = screening
        patch = {
            (screening.universe_store, "codes_of"): lambda focus="", **k: (
                [c for c, (p, s) in self.smap.items() if focus in (p, s)] if focus
                else list(self.quotes)),
            (screening.universe_store, "sectors_map"): lambda pool: dict(self.smap),
            (screening.universe_store, "sector_of"): lambda c: self.smap.get(c, ("一级", "二级")),
            (screening.universe_store, "taxonomy"): lambda: {"一级": ["二级"]},
            (screening.ds, "tencent_quote"): lambda codes: {c: self.quotes[c] for c in codes},
            (screening, "_metrics_of"): lambda codes: {
                c: {"vol": 30.0 + (int(c[-3:]) % 60), "cum20": 1.0, "range_pos": 50.0,
                    "net20": None, "net5": None, "series": []} for c in codes},
            (screening.factor_lab, "scoring_directions"): lambda cohort="layer_large", **k: {
                "vol": {"sign": 1}, "cum5": {"sign": 0}, "cum20": {"sign": 0},
                "cum60": {"sign": 0}, "cum120": {"sign": 0}, "range_pos": {"sign": 0}},
            (screening.factor_lab, "directions"): lambda **k: {
                "vol": {"sign": 1}, "cum20": {"sign": 0}, "range_pos": {"sign": 0}},
        }
        for (obj, name), fn in patch.items():
            self.saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, fn)
        return self

    def __exit__(self, *a):
        for (obj, name), fn in self.saved.items():
            setattr(obj, name, fn)
        return False


def _fake_pool():
    """三层各 60 只（大盘 vol 高、小盘 vol 低），外加每层一只 20 亿的越界股。"""
    quotes, smap = {}, {}
    for i in range(60):
        for tag, mcap in (("L", 900 - i), ("M", 400 - i), ("S", 90 - i)):
            c = f"{tag}{i:03d}"
            quotes[c] = {"name": c, "price": 10.0, "float_mcap_yi": float(mcap),
                         "lot_cost": 1000.0, "turnover": 1.0, "pe_ttm": 10.0, "pb": 1.0}
            smap[c] = ("一级", f"二级{tag}{i}")      # 各自不同细分 -> 行业上限不挡名额
    for tag in ("L", "M", "S"):
        c = f"{tag}999"
        quotes[c] = {"name": c, "price": 10.0, "float_mcap_yi": 20.0, "lot_cost": 1000.0,
                     "turnover": 1.0, "pe_ttm": 10.0, "pb": 1.0}
        smap[c] = ("一级", "越界层")
    return quotes, smap


def test_screen_rows_layered_quota_per_layer():
    """全市场分支：三层各 12 个名额，越界（<30 亿）不入选。小盘分数低也照样有 12 个名额。"""
    quotes, smap = _fake_pool()
    with _FakeEnv(quotes, smap, None):
        rows = app._screen_rows(100000.0, "")
    codes = [r["code"] for r in rows]
    assert len(codes) == 36, f"三层各 12 只，共 36，得 {len(codes)}"
    for tag in ("L", "M", "S"):
        n = sum(1 for c in codes if c.startswith(tag))
        assert n == 12, f"{tag} 层名额应为 12，得 {n}"
    assert all("999" not in c for c in codes), "20 亿的股不该进候选（30 亿门槛）"
    # 行业上限：每层内各自不同细分，不该出现重复细分被挡的情况
    subs = [r["sub"] for r in rows]
    assert len(subs) == len(set(subs)), f"细分重复（同二级 >1 只）: {subs}"


def test_screen_rows_layered_respects_sub_cap():
    """行业上限生效：某层 60 只全在同一申万二级时，该层最多进 2 只（其余名额不跨层补）。"""
    quotes, smap = _fake_pool()
    for c in list(smap):
        if c.startswith("S"):
            smap[c] = ("一级", "同一个细分")
    with _FakeEnv(quotes, smap, None):
        rows = app._screen_rows(100000.0, "")
    small = [r["code"] for r in rows if r["code"].startswith("S")]
    assert len(small) == 2, f"同一细分上限 2 只，得 {len(small)}"
    assert sum(1 for r in rows if r["code"].startswith("L")) == 12, "小盘被限不该影响大盘名额"


def test_screen_rows_focus_branch_keeps_sector_only():
    """指定板块分支：只在该板块内取，按分数排序，不强制分层。"""
    quotes, smap = _fake_pool()
    with _FakeEnv(quotes, smap, None):
        rows = app._screen_rows(100000.0, "二级L0")
    assert [r["code"] for r in rows] == ["L000"], \
        f"focus 应只返回该细分成分: {[r['code'] for r in rows]}"


def test_screen_rows_marginal_capital_falls_back_to_pool():
    """买不起任何 1 手时退回全池给参考（旧行为保留），且仍受 30 亿门槛约束。"""
    quotes, smap = _fake_pool()
    for q in quotes.values():
        q["lot_cost"] = 999999.0
    with _FakeEnv(quotes, smap, None):
        rows = app._screen_rows(100.0, "")
    assert rows, "买不起时不该返回空（退回全池给参考）"
    assert all("999" not in r["code"] for r in rows), "退回全池也要守住 30 亿门槛"


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
