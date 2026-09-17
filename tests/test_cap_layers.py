"""流通市值分层与名额（零依赖、离线、不打网络）。

**为什么有这个文件**：分层与名额是这次选股重构的核心口径（每层都留名额、同申万二级有上限），
`_pa_score` 的候选池、picks 三周期候选池、factor_lab 的分层 IC 都引用它。边界算错或行业上限
跨层累计，会让某一层整层消失而没人发现（症状只是「名单看起来怪」）。

跑法：python3 tests/test_cap_layers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cap_layers as cl  # noqa: E402


def _row(code, mcap, sub="", score=0.0):
    return {"code": code, "mcap_yi": mcap, "sub": sub, "score": score}


def test_layer_boundaries():
    """边界值归层要精确：500 亿算大盘、100 亿算中盘、30 亿算小盘，29.9 亿不纳入。"""
    assert cl.layer_of(500.0) == "large"
    assert cl.layer_of(499.99) == "mid"
    assert cl.layer_of(100.0) == "mid"
    assert cl.layer_of(99.99) == "small"
    assert cl.layer_of(30.0) == "small"
    assert cl.layer_of(29.99) is None, "30 亿以下不该进候选"
    for bad in (None, 0, -1, "x"):
        assert cl.layer_of(bad) is None, f"{bad!r} 应归 None"
    assert cl.layer_of("123.5") == "mid", "字符串数字应能解析（快照字段常是字符串）"


def test_select_quota_per_layer():
    """每层各留名额：小盘再便宜也不能把大盘的名额挤掉。"""
    rows = [_row(f"L{i}", 900 - i, f"subL{i}") for i in range(10)]
    rows += [_row(f"M{i}", 400 - i, f"subM{i}") for i in range(10)]
    rows += [_row(f"S{i}", 90 - i, f"subS{i}") for i in range(10)]
    got = cl.select(rows, per_layer=4, sub_cap=2)
    assert got == ["L0", "L1", "L2", "L3", "M0", "M1", "M2", "M3", "S0", "S1", "S2", "S3"], got


def test_select_sub_cap_is_per_layer():
    """行业上限在层内计数：小盘某细分拥挤不能吃掉大盘层的名额。"""
    rows = [_row("L0", 900, "银行"), _row("L1", 800, "银行"), _row("L2", 700, "银行"),
            _row("L3", 600, "白酒")]
    rows += [_row("M0", 400, "银行"), _row("M1", 390, "银行"), _row("M2", 380, "银行")]
    got = cl.select(rows, per_layer=3, sub_cap=2)
    # 大盘层：银行 2 只用完上限，第 3 只银行被跳过，轮到白酒
    # 中盘层重新计数：银行又可以进 2 只（跨层不累计），第三只银行被上限挡住、层内无替补
    assert got == ["L0", "L1", "L3", "M0", "M1"], got


def test_select_skips_below_threshold_and_unknown_sub():
    """30 亿以下不入选；sub 为空视作不受限，不互相挤。"""
    rows = [_row("X0", 29.0, "银行"), _row("L0", 900, ""), _row("L1", 800, "") ]
    got = cl.select(rows, per_layer=2, sub_cap=1)
    assert got == ["L0", "L1"], f"空行业不该被 sub_cap 挡掉、30 亿以下不该入选: {got}"


def test_select_underfilled_layer_is_not_backfilled():
    """某层候选不足就少给名额，不从别的层借（否则大盘不足时全变中小盘）。"""
    rows = [_row("L0", 900, "a"), _row("S0", 90, "b"), _row("S1", 80, "c")]
    got = cl.select(rows, per_layer=4, sub_cap=2)
    assert got == ["L0", "S0", "S1"], got


def test_layer_counts():
    rows = [_row("L", 900), _row("M", 300), _row("S", 50), _row("X", 10), _row("Y", 0)]
    assert cl.layer_counts(rows) == {"large": 1, "mid": 1, "small": 1, "none": 2}


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
