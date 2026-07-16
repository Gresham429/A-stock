"""Agent 的两道门：幂等门 + 交易时段门（零依赖，离线，不打网络/不调 LLM）。

**为什么有这个文件**：用户指出「现在不在交易时间，AI 跑了也买不了」，查出两个 bug——
  ① `dry_run` 会写「复盘」记录 → 幂等门以为今天跑过 → **开盘后的真跑被自己挡掉**，
     试跑本该无副作用
  ② 非交易时段跑决策 = 16 次 v4-pro 全部产出后被 `paper_store.order` 拒单，纯烧钱

跑法：python3 tests/test_agent_gates.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_loop  # noqa: E402
import agent_store  # noqa: E402


def test_dry_run_phase_is_prefixed():
    """试跑的阶段名必须带前缀 —— 幂等门靠它区分试跑与真跑。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "agent_loop.py"), encoding="utf-8").read()
    assert '"试跑-" + p' in src, "dry_run 的 phase 未加前缀 → 试跑会污染幂等门"
    assert src.count('_ph("') >= 5, "部分 log_run 未走 _ph()，试跑记录会混进真跑"


def test_idempotence_is_atomic_claim():
    """幂等必须靠**原子占位**，不能靠「查有没有跑过」。

    查记录是「开头查、180 秒后才写」——两个并发 run_all 会双双挤过去。实测发生过：
    10:19:58 与 10:20:08 各起一轮，两轮都跑完(179s/177s)，两个 agent 各跑了 2 次。
    """
    import inspect
    src = inspect.getsource(agent_loop.run_day)
    assert "claim_slot" in src, "run_day 未用原子占位做幂等"
    assert "release_slot" in src, "跑失败未释放占位 → 该桶当天再也跑不了"
    # claims 表必须靠 PRIMARY KEY 冲突保证原子性
    st = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "agent_store.py"), encoding="utf-8").read()
    assert "PRIMARY KEY (agent_id, date, slot)" in st, "claims 表缺复合主键，占位不原子"
    assert "sqlite3.IntegrityError" in st, "claim_slot 未靠主键冲突判定"


def test_claim_slot_is_exclusive():
    """同一 (agent, 日, 桶) 只能被抢到一次；不同桶互不影响。"""
    agent_store.init()
    aid, d = -12345, "1999-01-01"
    agent_store.release_slot(aid, d, "早盘")
    agent_store.release_slot(aid, d, "尾盘")
    assert agent_store.claim_slot(aid, d, "早盘") is True, "首次应抢到"
    assert agent_store.claim_slot(aid, d, "早盘") is False, "第二次应被挡 —— 竞态未修"
    assert agent_store.claim_slot(aid, d, "尾盘") is True, "不同桶应可跑"
    agent_store.release_slot(aid, d, "早盘")
    assert agent_store.claim_slot(aid, d, "早盘") is True, "释放后应可重试"
    agent_store.release_slot(aid, d, "早盘")
    agent_store.release_slot(aid, d, "尾盘")


def test_slots_defined_and_no_backfill():
    """时段桶定义合法；且**不得**有补跑逻辑（补跑必然引入未来函数）。"""
    import inspect
    names = [s[0] for s in agent_loop.SLOTS]
    assert len(names) == len(set(names)), "桶名重复"
    for name, lo, hi in agent_loop.SLOTS:
        assert 0 <= lo < hi <= 24 * 60, f"{name} 时间范围非法"
    src = inspect.getsource(agent_loop)
    assert "as-of" not in src.lower() or "无 as-of" in src, "若做补跑必须先解决全市场历史快照"


def test_run_all_has_market_gate():
    """非交易时段不该跑决策（会被 order 全拒，纯烧 LLM 钱）。"""
    import inspect
    sig = inspect.signature(agent_loop.run_all)
    assert "require_open" in sig.parameters, "run_all 缺 require_open 门"
    src = inspect.getsource(agent_loop.run_all)
    assert "_market_open()" in src, "run_all 未检查交易时段"
    assert "sweep_conditions" in src, "非交易时段应仍补判条件单（补的是历史触发）"


def test_boot_uses_market_gate():
    """启动自动跑必须带 require_open —— 否则半夜开 app 就烧 16 次 LLM。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read()
    seg = src[src.index("def _agent_boot"):src.index("def _universe_boot")]
    assert "require_open=True" in seg, "_agent_boot 未带 require_open"


def test_lesson_kinds_closed_set():
    """教训 kind 是闭集——闭集外的必须被丢弃，否则统计与反哺都会被污染。"""
    assert agent_store.add_lesson(-999, "不在闭集里的kind", "x") is False
    for k in agent_store.LESSON_KINDS:
        assert "{v}" in agent_store.LESSON_KINDS[k][1], f"{k} 的模板缺 {{v}} 占位"


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
