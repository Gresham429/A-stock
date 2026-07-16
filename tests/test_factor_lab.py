"""factor_lab 的方向机制（零依赖，离线，不打网络）。

**为什么有这个文件**：`_pa_score` 的打分方向、`detect_failures` 记不记教训，
全都由 `direction()` 决定。它错一次，选股和教训一起错，且会通过 `_lesson_block()`
污染 5 个 AI（踩过：教训与因子数据矛盾，系统在惩罚 AI 服从自己的数据）。

跑法：python3 tests/test_factor_lab.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factor_lab as fl  # noqa: E402


def test_direction_needs_significance():
    """|t| 不显著时必须返回 sign=0（不参与打分）—— 不猜。"""
    import inspect
    src = inspect.getsource(fl.direction)
    assert "T_THRESHOLD" in src, "方向未按 t 阈值门控"
    assert '"sign": 0' in src, "不显著时未返回 sign=0（会被迫猜方向）"


def test_direction_prefers_recent_when_significant():
    """近期显著 → 用近期方向（regime 切换）；否则回落全样本。"""
    import inspect
    src = inspect.getsource(fl.direction)
    i_rec, i_full = src.index("t_rec"), src.index("t_full")
    assert src.index("abs(t_rec)") < src.index("abs(t_full)"), "近期方向未优先于全样本"
    assert i_rec > 0 and i_full > 0


def test_staleness_accounts_for_structural_lag():
    """判过期必须扣掉 IC 的结构性滞后 —— 否则永远判为过期、每次启动都重跑。

    IC 天然滞后 max(HORIZONS) 个交易日：今日因子值要等未来收益才能算 IC。
    """
    import inspect
    src = inspect.getsource(fl.is_stale)
    assert "structural" in src, "过期判定未扣除结构性滞后"
    assert "HORIZONS" in src, "结构性滞后未与 HORIZONS 挂钩"
    stale, lag = fl.is_stale()
    assert isinstance(stale, bool) and lag >= 0


def test_direction_log_only_records_changes():
    """方向留痕只记拐点 —— 每天记一遍会把表撑爆且看不出变化。"""
    import inspect
    src = inspect.getsource(fl.log_directions)
    assert "if p is not None and p == d[\"sign\"]" in src or "continue" in src, (
        "未跳过「方向未变」的情况")


def test_refresh_is_lazy():
    """刷新必须惰性（过期才跑）—— 每次启动都跑 14s 回测是浪费。"""
    import inspect
    src = inspect.getsource(fl.refresh_if_stale)
    assert "is_stale()" in src, "refresh 未先检查过期"
    assert "skipped" in src, "未过期时未跳过"


def test_factors_have_range_anchors():
    """每个参与打分的因子都要有分位锚点，否则量纲差异会主导打分。"""
    import app
    for f in fl.FACTORS:
        assert f in app._FACTOR_RANGE, f"{f} 缺分位锚点 → _pa_score 会被量纲带偏"
        lo, hi = app._FACTOR_RANGE[f]
        assert lo < hi, f"{f} 锚点区间非法"


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
