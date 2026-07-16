"""structure.py 的 K 线结构摘要（零依赖，离线，不打网络）。

**为什么有这个文件**：2026-07-16 早盘 12 个 agent 有 11 个产出 0 条意向，
理由清一色「数据不足：缺少K线、趋势、均线」——规则库注入 84 条价格行为规则
（其中 37 条要 K线/均线/趋势），提示词却只给静态标量，AI 只能援引 R6「看不懂就等」。

本模块是补上的那份数据。它必须守住两条线：
  1. **只给原料、不下判断**——不算「趋势=涨/跌」这类带阈值的结论（PITFALLS#1：
     拍脑袋定方向已被数据打脸 5 次）。趋势由 AI 读均线关系自己判。
  2. **数据不足时返回 None，不外推**——全市场池数据质量参差（0 收盘、退市残留）。

跑法：python3 tests/test_structure.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structure as st  # noqa: E402


def _bars(closes: list[float]) -> list[dict]:
    """按收盘价造日K（OHLC 围绕收盘价小幅浮动，时间正序）。

    日期用真实递增日历日（早先写 f"2026-01-{i+1:02d}" 会造出「2026-01-58」这种
    不存在的日期，断言跟着写错还以为是代码的锅）。
    """
    d0 = date(2026, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "open": c * 0.99, "high": c * 1.02,
             "low": c * 0.98, "close": c, "volume": 1e6}
            for i, c in enumerate(closes)]


def test_digest_none_when_too_few_bars():
    """K 线不足以算 MA20 时返回 None —— 宁可无数据，不可外推出假均线。"""
    assert st.digest(_bars([10.0] * 5)) is None, "5 根就出摘要 → MA20 是编的"
    assert st.digest([]) is None, "空 K 线未返回 None"


def test_digest_ma_values_correct():
    """MA 必须是真算的算术平均，不是近似。"""
    closes = [float(i) for i in range(1, 61)]      # 1..60，收盘=序号
    d = st.digest(_bars(closes))
    assert d is not None
    assert d["ma5"] == round(sum(closes[-5:]) / 5, 2), f"MA5 错: {d['ma5']}"
    assert d["ma20"] == round(sum(closes[-20:]) / 20, 2), f"MA20 错: {d['ma20']}"
    assert d["ma60"] == round(sum(closes[-60:]) / 60, 2), f"MA60 错: {d['ma60']}"


def test_digest_ma60_none_when_insufficient():
    """不够 60 根时 MA60=None，但 MA5/MA20 照给 —— 部分缺失不该拖垮整只。"""
    d = st.digest(_bars([float(i) for i in range(1, 31)]))
    assert d is not None
    assert d["ma60"] is None, "不足 60 根却给出了 MA60"
    assert d["ma5"] is not None and d["ma20"] is not None, "MA5/20 不该被 MA60 连累"


def test_digest_hi_lo_span_20_bars():
    """近20日高/低取自最后 20 根的 high/low，不是收盘价。"""
    closes = [10.0] * 40 + [12.0] + [10.0] * 19   # 第 41 根冒尖
    bars = _bars(closes)
    d = st.digest(bars)
    assert d is not None
    # 尖峰在倒数第 20 根开外 → 不该进近20日高
    assert d["hi20"] == round(max(b["high"] for b in bars[-20:]), 2), "hi20 未取近20根 high"
    assert d["lo20"] == round(min(b["low"] for b in bars[-20:]), 2), "lo20 未取近20根 low"


def test_digest_last_bars_are_ohlc_time_ascending():
    """最近3根必须是 OHLC 原样、时间正序 —— AI 要靠它读信号棒。"""
    closes = [float(i) for i in range(1, 61)]
    d = st.digest(_bars(closes))
    assert d is not None
    assert len(d["last"]) == 3, "未给最近3根"
    assert [b["close"] for b in d["last"]] == [58.0, 59.0, 60.0], "顺序错（应时间正序）"
    for b in d["last"]:
        assert {"open", "high", "low", "close"} <= set(b), "K 线缺 OHLC 字段"


def test_digest_survives_dirty_bars():
    """含 0 收盘/None 的脏数据不得抛异常（全市场池必踩，见 PITFALLS#11）。"""
    bars = _bars([float(i) for i in range(1, 61)])
    bars[10]["close"] = 0.0
    bars[20]["high"] = None
    try:
        st.digest(bars)          # 不抛即可，返回 None 或摘要都接受
    except Exception as e:       # noqa: BLE001 —— 这里就是要证明它不抛
        raise AssertionError(f"脏数据穿透: {type(e).__name__}: {e}") from e


def test_no_threshold_judgement_in_module():
    """守住红线：本模块只给原料，不得出现「趋势/看涨/看跌」这类结论字段。

    一旦我方替 AI 下方向判断，就回到 PITFALLS#1 的老路（拍的阈值方向大概率是错的）。
    """
    import inspect
    src = inspect.getsource(st)
    for banned in ['"trend"', "'trend'", '"bullish"', '"signal"', '"是否"']:
        assert banned not in src, f"出现了带判断的字段 {banned} —— 方向该由 AI 读均线自己判"


def test_fmt_stock_compact_and_lossless():
    """成行必须紧凑（token 预算）且不丢关键数值。

    上界 128 的来历：决策提示词送 20 只候选 → 20×128 ≈ 2560 字符结构数据，
    叠加原 ≈8000 字提示词 ≈ 10.5k 字符。v4-pro 决策档 max_tokens=8000 覆盖
    「思考+正文」（PITFALLS#13，曾只给 5000 直接炸）。**这是护栏不是实测**
    ——真实占用以冒烟里的 finish_reason 为准。
    """
    d = st.digest(_bars([float(i) for i in range(1, 61)]))
    s = st.fmt_stock(d)
    assert len(s) < 145, f"单行 {len(s)} 字符，20 只会撑爆提示词预算"
    assert "58.0" in s and "MA20" in s, "关键数值丢失"


def test_fmt_stock_bars_carry_date():
    """每根K线必须带日期 —— 盘中日K最后一根是**昨天**，而候选行现价是今天。

    不标日期 AI 会把昨天的 bar 当今日信号棒读（实测 2026-07-16 12:35 午休时，
    工商银行日K末根=07-15 收7.51=昨收，而实时现价 7.43）。这种错不报错。
    """
    d = st.digest(_bars([float(i) for i in range(1, 61)]))
    s = st.fmt_stock(d)
    # 60 根从 2026-01-01 起算，末根 = 03-01；只断言末根，避免把日历算术写进断言。
    assert "0301:" in s, f"K线未带日期，AI 无法知道自己在看哪天: {s}"


def test_digest_keeps_date_on_bars():
    """digest 的 last 必须保留 date 字段（供 fmt 与停牌识别）。"""
    d = st.digest(_bars([float(i) for i in range(1, 61)]))
    assert d is not None
    for b in d["last"]:
        assert b.get("date"), "K线丢了日期"


def test_fmt_stock_ma_unambiguous():
    """均线必须带分隔符 —— 踩过：MA5=8.01 拼成「MA58.01」，与 MA58 无法区分。

    AI 会把每一条均线都读错，且这种错**不报错**，只是让它基于假均线做决策。
    """
    d = st.digest(_bars([8.01] * 60))
    s = st.fmt_stock(d)
    assert "MA5=" in s and "MA20=" in s and "MA60=" in s, f"均线名与数值粘连: {s}"
    assert "MA58" not in s and "MA208" not in s, f"均线歧义: {s}"


def test_fmt_stock_handles_none():
    """digest=None 时成行不得崩，要明说「无K线」让 AI 知道缺什么。"""
    s = st.fmt_stock(None)
    assert isinstance(s, str) and s, "None 未降级成字符串"


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
