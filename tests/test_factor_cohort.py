"""cohort-aware 因子方向（零依赖、离线、不打网络）。

**为什么有这个文件**：`_pa_score`/`detect_failures` 的打分对象是预筛后的**大盘池**，其因子
方向可与「小盘主导的全池」相反（2026-07-18 覆盖分析：h=10 range_pos 全池 −1 vs 大盘 +1）。
这里验证 direction/scoring_directions 按 cohort 读对表、冷启动回退全池、有数据但不显著不回退，
以及 backtest/backtest_large 共用的纯 IC helper 正确。

跑法：python3 tests/test_factor_cohort.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factor_lab as fl  # noqa: E402


def _fresh_db():
    d = tempfile.mkdtemp()
    fl.DB_PATH = os.path.join(d, "factors.db")
    fl.init()


def _sig_ics(sign: int, n: int = 60) -> list[float]:
    """构造 n 个均值显著、方向为 sign 的 IC 值（交替 ±ε 制造小方差，t 值很大）。"""
    base = 0.05 * sign
    return [base + (0.01 if i % 2 else -0.01) for i in range(n)]


def _flat_ics(n: int = 60) -> list[float]:
    """均值≈0、有方差 → 有数据但不显著（basis=均不显著，非「数据不足」）。"""
    return [(0.02 if i % 2 else -0.02) for i in range(n)]


def _inject(table: str, factor: str, ics: list[float], horizon: int = 10, cohort: str = ""):
    with fl._conn() as c:
        for i, ic in enumerate(ics):
            if table == "ic_daily":
                c.execute("INSERT INTO ic_daily(date,factor,horizon,ic,n) VALUES(?,?,?,?,?)",
                          (f"{i:04d}", factor, horizon, ic, 50))
            else:
                c.execute("INSERT INTO ic_cohort(date,factor,horizon,cohort,ic,n) VALUES(?,?,?,?,?,?)",
                          (f"{i:04d}", factor, horizon, cohort, ic, 50))
        c.commit()


def test_direction_reads_correct_table_per_cohort():
    """cohort='all' 读 ic_daily、其它读 ic_cohort —— 同因子两表方向相反时不串。"""
    _fresh_db()
    _inject("ic_daily", "range_pos", _sig_ics(-1))            # 全池：反转 −1
    _inject("ic_cohort", "range_pos", _sig_ics(+1), cohort="large")   # 大盘：动量 +1
    assert fl.direction("range_pos", 10, "all")["sign"] == -1, "all 应读 ic_daily（−1）"
    assert fl.direction("range_pos", 10, "large")["sign"] == +1, "large 应读 ic_cohort（+1）"


def test_scoring_directions_falls_back_when_cohort_empty():
    """冷启动：cohort 表无数据 → 回退全池（避免打分全体中性、shortlist 崩）。"""
    _fresh_db()
    for f in fl.FACTORS:
        _inject("ic_daily", f, _sig_ics(+1))    # 全池有显著方向
    # 不注入任何 ic_cohort
    sd = fl.scoring_directions("large", 10)
    assert all(sd[f]["sign"] == +1 for f in fl.FACTORS), f"cohort 空时未回退全池: {sd}"


def test_scoring_directions_respects_insignificant_cohort():
    """cohort 有数据但不显著 → 尊重 sign 0（数据说此池中性），**不回退**全池。"""
    _fresh_db()
    for f in fl.FACTORS:
        _inject("ic_daily", f, _sig_ics(+1))              # 全池显著 +1
        _inject("ic_cohort", f, _flat_ics(), cohort="large")   # 大盘有数据但不显著
    sd = fl.scoring_directions("large", 10)
    assert all(sd[f]["sign"] == 0 for f in fl.FACTORS), (
        f"cohort 有数据但不显著时错误回退全池（应尊重 0）: {sd}")


def test_scoring_directions_all_is_full_universe():
    """cohort='all' 的 scoring_directions 就是全池 directions（不走回退分支）。"""
    _fresh_db()
    _inject("ic_daily", "vol", _sig_ics(+1))
    assert fl.scoring_directions("all", 10)["vol"]["sign"] == +1


def test_daily_ic_rows_pure_and_correct():
    """纯 helper：per_day → IC 行；因子与 fwd 完全同序 → IC≈+1，每 (factor,horizon) 一行。"""
    seq = list(range(10))                       # 10 个点，_spearman 需 ≥8
    per_day = {"d0": {}}
    for f in fl.FACTORS:
        per_day["d0"][f] = [float(x) for x in seq]
    for h in fl.HORIZONS:
        per_day["d0"][f"fwd{h}"] = [float(x) for x in seq]   # 与因子完全正相关
    rows = fl._daily_ic_rows(per_day)
    assert len(rows) == len(fl.FACTORS) * len(fl.HORIZONS), f"行数不对: {len(rows)}"
    for (date, factor, horizon, ic, n) in rows:
        assert date == "d0" and factor in fl.FACTORS and horizon in fl.HORIZONS
        assert abs(ic - 1.0) < 1e-6, f"完全同序应 IC≈1，得 {ic}"
        assert n == 10


def test_daily_ic_rows_skips_length_mismatch():
    """因子与 fwd 长度不一致（数据缺口）→ 跳过该 (factor,horizon)，不产错行。"""
    per_day = {"d0": {"vol": [1.0, 2.0, 3.0]}}   # 只有 vol、无 fwd → 全部 mismatch
    assert fl._daily_ic_rows(per_day) == [], "长度不匹配应跳过、不产行"


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
