"""超额收益分位分布 + 判罪线（零依赖，离线，不打网络）。

**为什么有这个文件**：这是**判罪线的唯一合法来源**。「超额多负算失败」是个阈值，
按 PITFALLS#1 的战绩（拍的方向被数据打脸 5 次：`_pa_score` 两个分量方向全反、止损线、
教训阈值、「方向会抖动」的判断）——**不能拍**。这里让「这笔在历史上有多差」成为
可查的事实。

守住三条：
  1. **分位数必须真的是分位数**（算错了，判罪线整体偏移，且不报错）。
  2. **分布是超额口径**，不是绝对收益 —— 否则 beta 混进判罪线（PITFALLS#5）。
  3. **没有分布就不判罪**，返回 None 而不是退回某个默认阈值（退回=偷偷拍了个数）。

跑法：python3 tests/test_excess_dist.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factor_lab as fl  # noqa: E402


def test_percentile_math_is_correct():
    """分位数算错 → 判罪线整体偏移，且不报错。用已知数列钉死。"""
    xs = [float(i) for i in range(1, 101)]      # 1..100
    assert fl._percentile(xs, 50) == 50.5, f"p50 错: {fl._percentile(xs, 50)}"
    assert fl._percentile(xs, 1) == 1.99, f"p1 错: {fl._percentile(xs, 1)}"
    assert fl._percentile(xs, 100) == 100.0, f"p100 错: {fl._percentile(xs, 100)}"
    assert fl._percentile(xs, 0) == 1.0, f"p0 错: {fl._percentile(xs, 0)}"


def test_percentile_handles_tiny_and_empty():
    """样本极少/为空时不得抛 —— 分布没建好是常态（首次运行、回测失败）。"""
    assert fl._percentile([], 50) is None, "空样本未返回 None"
    assert fl._percentile([3.0], 50) == 3.0, "单样本应返回它自己"


def test_rank_of_returns_none_without_distribution():
    """**没有分布就不判罪**：返回 None，绝不退回某个默认阈值。

    退回默认值 = 偷偷拍了个数，正是 PITFALLS#1 要防的。
    """
    r = fl.rank_of(20, -9.99, dist={})
    assert r is None, f"无分布却给出了分位: {r}"
    r2 = fl.rank_of(20, -9.99, dist={20: {}})
    assert r2 is None, f"该地平线无分布却给出了分位: {r2}"


def test_rank_of_maps_value_to_percentile():
    """把超额收益映射到历史分位。分布：p10=-10, p50=0, p90=+10。"""
    dist = {20: {1: -30.0, 5: -20.0, 10: -10.0, 25: -5.0, 50: 0.0,
                 75: 5.0, 90: 10.0, 95: 20.0, 99: 30.0}}
    assert fl.rank_of(20, -10.0, dist) == 10, "落在 p10 上应报 10"
    assert fl.rank_of(20, -25.0, dist) <= 5, "比 p5 还差应 ≤5"
    assert fl.rank_of(20, 0.0, dist) == 50, "落在中位数应报 50"
    assert fl.rank_of(20, 100.0, dist) >= 99, "好过 p99 应 ≥99"


def test_rank_of_is_monotonic():
    """越差的超额 → 分位越低。非单调说明映射写反了（会把最好的判成最差）。"""
    dist = {20: {1: -30.0, 5: -20.0, 10: -10.0, 25: -5.0, 50: 0.0,
                 75: 5.0, 90: 10.0, 95: 20.0, 99: 30.0}}
    ranks = [fl.rank_of(20, v, dist) for v in (-40, -25, -12, -6, 0, 6, 12, 25, 40)]
    assert ranks == sorted(ranks), f"分位映射非单调: {ranks}"


def test_dist_pcts_cover_both_tails():
    """判罪看下尾，但上尾也要留 —— 不然无法说「这笔其实很好」。"""
    assert min(fl.DIST_PCTS) <= 5 and max(fl.DIST_PCTS) >= 95, f"分位点覆盖不足: {fl.DIST_PCTS}"
    assert 50 in fl.DIST_PCTS, "缺中位数"


def test_horizons_align_with_outcome():
    """分布的地平线必须与 outcome/factor_lab 一致，否则查不到对应的线。"""
    import outcome
    assert tuple(outcome.HORIZONS) == tuple(fl.HORIZONS), "地平线不一致"


def test_bench_sym_is_single_source():
    """基准必须单一事实源：判罪线(分布)与被判的数若不同口径，错得不报错。"""
    import agent_loop as al
    assert al._BENCH_SYM is fl.BENCH_SYM, "agent_loop 复制了基准而非引用 factor_lab"


# ── 判罪（_judge_entry）：不碰真实库，stub 掉落库与分布 ────────────────────
_ENTRY = {"agent_id": 99, "code": "002354", "name": "天娱数科", "entry_date": "2026-06-10",
          "entry_price": 8.87, "pa_score": 77.2, "range_pos": 20.0, "vol": 111.0, "cum20": 5.9}
_DIST = {20: {1: -30.0, 5: -20.0, 10: -12.0, 25: -6.5, 50: -0.9,
              75: 5.7, 90: 14.9, 95: 23.0, 99: 49.3}}


def _judge(x, dist=_DIST):
    """跑 _judge_entry，stub 掉分布与落库；返回 (是否记教训, 教训文案)。"""
    import agent_loop as al
    import agent_store
    box = {}

    def fake_add(agent_id, kind, evidence, code="", rule_id=None):
        box["text"] = agent_store.LESSON_KINDS[kind][1].format(v=evidence)
        return True

    o_dist, o_add = fl.excess_dist, agent_store.add_lesson
    fl.excess_dist, agent_store.add_lesson = (lambda: dist), fake_add
    try:
        # 分位由调用方（settle_entries）算好传入；测试同样用 stub 分布算 rank 再传。
        # 与 settle_entries 一致：x 为 None 不算 rank（否则 rank_of 会崩）。
        rank = fl.rank_of(al.JUDGE_H, x) if x is not None else None
        return bool(al._judge_entry(dict(_ENTRY), x, rank)), box.get("text", "")
    finally:
        fl.excess_dist, agent_store.add_lesson = o_dist, o_add


def test_judge_records_only_bottom_decile():
    """只有落到底部十分位才记 —— 判罪线来自分布，不是拍的。"""
    import agent_loop as al
    assert al.LESSON_PCT == 10, "判罪线变了，本测试的预期要跟着改"
    assert _judge(-25.0)[0], "第1百分位却没记"
    assert _judge(-12.5)[0], "第9百分位却没记"
    assert not _judge(-9.99)[0], "第16百分位不该记（没差到底部十分位）"
    assert not _judge(0.0)[0], "中位数附近记了教训"
    assert not _judge(8.0)[0], "跑赢了还记教训"


def test_judge_refuses_without_distribution():
    """**无分布必须拒判**，不许退回默认阈值（退回=偷偷拍了个数，PITFALLS#1）。"""
    assert not _judge(-25.0, dist={})[0], "无分布却judge了"
    assert not _judge(-25.0, dist={20: {}})[0], "该地平线无分布却judge了"


def test_judge_refuses_when_excess_missing():
    """超额算不出（基准缺失/停牌）→ 拒判，**绝不拿绝对收益顶替**（beta 会混进来）。"""
    assert not _judge(None)[0], "超额为 None 却judge了"


def test_judge_uses_longest_horizon():
    """判罪必须看 20 日档 —— 5 日与 20 日实测 33.1% 判反。"""
    import agent_loop as al
    assert al.JUDGE_H == max(fl.HORIZONS) == 20, f"判罪地平线错: {al.JUDGE_H}"


def test_lesson_text_is_case_not_rule():
    """文案必须是**个案陈述**，且不得宣称因果 —— n 太小（784 笔≈31年，PITFALLS#5）。

    踩过：chase_high 写「>85 视为追高」是普适规则口吻，证据却只有 n=1。
    """
    ok, text = _judge(-14.5)
    assert ok
    assert "百分位" in text, "未给出历史分位（读者无法判断有多差）"
    assert "个案" in text and "不代表因果" in text, f"缺个案/因果免责，会被当成规律: {text}"
    assert "002354" in text and "2026-06-10" in text, "未锚定到具体这一笔"


def test_chase_high_text_no_longer_claims_outcome():
    """chase_high 的文案不得再宣称「此后回落」—— 它在成交当天就记，那时没有「此后」。"""
    import agent_store
    _, tpl = agent_store.LESSON_KINDS["chase_high"]
    assert "此后" not in tpl and "回落" not in tpl, f"仍在宣称未经验证的结果: {tpl}"


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
