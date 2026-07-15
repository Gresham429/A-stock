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
