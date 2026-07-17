"""agent 个体记忆：教训按 agent 隔离召回 + 自己的战绩块（零依赖，离线）。

**为什么有这个文件**：2026-07-16 用户指出——12 个对照 agent 若共读一个教训池，记忆会
收敛到一起，分不清 A 表现好是策略好还是碰巧读了 B 的教训。故：
  · agent 决策召回**自己的**教训（A）；用户面 5 个 AI 仍读全体池。
  · agent 决策提示词带**自己的**历史建仓+结果（B）——个体情节记忆。

守住的线：**只喂事实，不让 agent 写反思**（反思=拟合噪音，设计文档红线）；
**account 隔离**（每个存档只看自己那个仓）。

跑法：python3 tests/test_agent_memory.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_store  # noqa: E402


def _fresh_db():
    """全新临时库（不复制真实数据，避免污染断言）。"""
    d = tempfile.mkdtemp()
    agent_store.DB_PATH = os.path.join(d, "agents.db")
    agent_store.init()


def test_lesson_recall_is_per_agent():
    """A：agent 只召回自己的教训，不串到别的 agent。"""
    _fresh_db()
    agent_store.add_lesson(101, "chase_high", 92, "600000")
    agent_store.add_lesson(101, "chase_high", 95, "600001")   # 同 agent 同 kind → 累加 hits
    agent_store.add_lesson(102, "oversize", 35, "000001")
    a101, a102 = agent_store.for_ai(agent_id=101), agent_store.for_ai(agent_id=102)
    assert "追高" in a101 and "超仓" not in a101, "101 串到了别的 agent 的教训"
    assert "超仓" in a102 and "追高" not in a102, "102 串到了别的 agent 的教训"


def test_lesson_pool_aggregates_all_agents():
    """用户面 5 个 AI（agent_id=None）仍读全体池——它们本就该学舰队集体教训。"""
    _fresh_db()
    agent_store.add_lesson(101, "chase_high", 92, "600000")
    agent_store.add_lesson(102, "oversize", 35, "000001")
    pool = agent_store.for_ai()   # 无 agent_id
    assert "追高" in pool and "超仓" in pool, "全体池漏了某个 agent 的教训"


def test_lesson_empty_returns_blank():
    """无教训 → 空串（不注入空块，省 token）。"""
    _fresh_db()
    assert agent_store.for_ai(agent_id=101) == "", "无教训却注入了非空块"


def test_history_block_shows_settled_and_holding():
    """B：战绩块分「已结算(带超额+分位)」与「持有中」两段。"""
    _fresh_db()
    import agent_loop as al
    agent_store.add_entry(17, "002354", "天娱数科", "2026-06-10", 8.87, 300, {"pa_score": 77})
    eid = agent_store.entries(17)[0]["id"]
    agent_store.update_entry(eid, {"r20": -8.906, "x20": -9.992}, done=True)
    agent_store.add_entry(17, "002602", "世纪华通", "2026-07-16", 13.5, 100, {"pa_score": 60})
    block = al._agent_history_block(17, 19)
    assert "已结算" in block and "持有中" in block, f"战绩块缺分段: {block}"
    assert "002354" in block and "超额 -9.99%" in block, "已结算笔缺超额收益"
    assert "002602" in block, "持有中笔漏了"
    # 不得出现「反思/我觉得/建议」这类主观词——只喂事实
    for banned in ("我觉得", "反思", "教训是", "应该"):
        assert banned not in block, f"战绩块混入了主观总结「{banned}」——只该喂事实"


def test_history_block_empty_when_no_entries():
    """空仓起步、无历史 → 空串。"""
    _fresh_db()
    import agent_loop as al
    assert al._agent_history_block(999, 999) == "", "无历史却注入了非空战绩块"


def test_history_only_own_account():
    """account 隔离：agent 只看自己 account 的建仓（entries 按 agent_id 查）。"""
    _fresh_db()
    import agent_loop as al
    agent_store.add_entry(17, "002354", "天娱", "2026-06-10", 8.87, 300)
    agent_store.add_entry(18, "600519", "茅台", "2026-06-10", 1600, 100)
    b17 = al._agent_history_block(17, 19)
    assert "002354" in b17 and "600519" not in b17, "agent 17 看到了 agent 18 的仓"


def test_pctile_is_frozen_from_column():
    """P1（稳定性）：战绩块历史分位读 entries.x20_pctile（结算时冻结），不再重算判罪线。

    fresh 库无 excess_dist → 旧代码调 rank_of 会得 None、无分位尾巴；现在显示出
    「第 8 百分位」只可能来自冻结列，证明记忆不再随判罪线漂移而变动。
    """
    _fresh_db()
    import agent_loop as al
    agent_store.add_entry(17, "002354", "天娱数科", "2026-06-10", 8.87, 300, {"pa_score": 77})
    eid = agent_store.entries(17)[0]["id"]
    agent_store.update_entry(eid, {"r20": -8.9, "x20": -9.99, "x20_pctile": 8}, done=True)
    block = al._agent_history_block(17, 19)
    assert "第 8 百分位" in block, f"未读到冻结分位（应显示第 8 百分位）: {block}"
    assert agent_store.entries(17)[0]["x20_pctile"] == 8, "x20_pctile 未写入/未持久"


def test_for_ai_deterministic_under_ties():
    """P1（稳定性）：等 hits 的教训 kind 顺序稳定（kind ASC 平局键）→ 两次调用输出一致。"""
    _fresh_db()
    agent_store.add_lesson(17, "chase_high", 90, "600000")
    agent_store.add_lesson(17, "against_sector", 91, "600001")   # 各 1 次，hits 相等
    a = agent_store.for_ai(agent_id=17)
    b = agent_store.for_ai(agent_id=17)
    assert a == b and a != "", "等 hits 下 for_ai 两次调用输出不一致（非确定性）"


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
