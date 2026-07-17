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


def test_journal_self_view_shows_decisions_with_reasons():
    """P2（连续性）：自视图喂回自己最近的决策(买/观望)+**当时写下的理由原话**+regime。"""
    _fresh_db()
    import agent_loop as al
    agent_store.journal_add(17, "002354", "天娱数科", "2026-07-17", "早盘",
                            "down/cold", {"pa_score": 77}, "buy", "形态分最高、低位顺势")
    agent_store.journal_add(17, "", "", "2026-07-16", "尾盘", "down/cold", {},
                            "skip", "大盘弱、观望等企稳")
    block = al._agent_journal_block(17)
    assert "买入" in block and "观望" in block, f"缺买/观望分类: {block}"
    assert "形态分最高" in block and "观望等企稳" in block, "缺当时理由原话"
    assert "大盘down/cold" in block, "缺 regime 标注"


def test_journal_outcome_stapled_and_idempotent():
    """P2：结算后结果贴回买入行，自视图显示超额+冻结分位；二次 staple 不改（幂等）。"""
    _fresh_db()
    import agent_loop as al
    agent_store.journal_add(17, "002354", "天娱", "2026-06-10", "早盘", "flat/neutral",
                            {}, "buy", "试一笔")
    agent_store.journal_staple_outcome(17, "002354", "2026-06-10", -9.99, 8)
    block = al._agent_journal_block(17)
    assert "20日超额 -9.99%" in block and "第 8 百分位" in block, f"结果未贴回: {block}"
    agent_store.journal_staple_outcome(17, "002354", "2026-06-10", 5.0, 90)   # 再贴
    assert agent_store.journal_of(17)[0]["x20"] == -9.99, "二次 staple 改了已结算行（应幂等）"


def test_journal_isolated_per_agent():
    """P2：自视图只看自己的 journal（account 隔离，多-agent 对照干净）。"""
    _fresh_db()
    import agent_loop as al
    agent_store.journal_add(17, "002354", "天娱", "2026-07-17", "早盘", "", {}, "buy", "a")
    agent_store.journal_add(18, "600519", "茅台", "2026-07-17", "早盘", "", {}, "buy", "b")
    assert "002354" in al._agent_journal_block(17) and "600519" not in al._agent_journal_block(17)


def test_stock_house_view_cross_agent():
    """P2c：个股 house-view 汇集**全体 agent** 在这只票上的买入决策+理由+结果（共享底座按 code 查）。"""
    _fresh_db()
    import ai_blocks
    agent_store.journal_add(17, "002354", "天娱", "2026-07-17", "早盘", "down/cold", {}, "buy", "低位顺势")
    agent_store.journal_add(18, "002354", "天娱", "2026-07-10", "尾盘", "flat/neutral", {}, "buy", "反弹试仓")
    agent_store.journal_staple_outcome(18, "002354", "2026-07-10", -6.5, 15)
    hv = ai_blocks._stock_house_view("002354")
    assert "低位顺势" in hv and "反弹试仓" in hv, f"house-view 应含全体 agent 的理由: {hv}"
    assert "20日超额 -6.50%" in hv, "已结算笔应显示结果"
    assert ai_blocks._stock_house_view("600519") == "", "无记录的票应返回空（不注入空块）"


def test_journal_for_regime_fleet_buys_only():
    """P3：regime-view 底座——按 regime 查全舰队买入决策；只取 buy、跨 agent、regime 精确匹配。"""
    _fresh_db()
    agent_store.journal_add(17, "002354", "天娱", "2026-07-17", "早盘", "up/hot", {}, "buy", "追热点")
    agent_store.journal_add(18, "600519", "茅台", "2026-07-16", "尾盘", "up/hot", {}, "buy", "白马")
    agent_store.journal_add(19, "", "", "2026-07-15", "早盘", "up/hot", {}, "skip", "观望")   # 非 buy 不计
    agent_store.journal_add(17, "000001", "平安", "2026-07-14", "早盘", "down/cold", {}, "buy", "别的行情")
    rows = agent_store.journal_for_regime("up/hot")
    codes = {r["code"] for r in rows}
    assert codes == {"002354", "600519"}, f"应只含 up/hot 的买入、跨 agent: {codes}"
    assert agent_store.journal_for_regime("") == [], "空 regime 应返回 []"
    assert agent_store.journal_for_regime("never/seen") == [], "无历史的 regime 应返回 []"


def test_regime_view_aggregates_settled_and_open():
    """P3：_regime_view 出「已结算均值+胜率」聚合 + 逐条；无 regime/无历史 → 空块。"""
    _fresh_db()
    import ai_blocks
    agent_store.journal_add(17, "002354", "天娱", "2026-07-17", "早盘", "up/hot", {}, "buy", "追热点")
    agent_store.journal_staple_outcome(17, "002354", "2026-07-17", 6.0, 88)
    agent_store.journal_add(18, "600519", "茅台", "2026-07-16", "尾盘", "up/hot", {}, "buy", "白马")
    agent_store.journal_staple_outcome(18, "600519", "2026-07-16", -2.0, 30)
    agent_store.journal_add(19, "300750", "宁德", "2026-07-15", "早盘", "up/hot", {}, "buy", "开新仓")  # 未结算
    view = ai_blocks._regime_view("up/hot")
    assert "已结算 2 次买入" in view and "跑赢大盘 1/2 次" in view, f"聚合行不对: {view}"
    assert "平均 20 日超额 +2.00%" in view, f"均值算错(应 (6-2)/2=+2.00%): {view}"
    assert "追热点" in view and "结果未定" in view, "缺逐条理由/未结算标注"
    assert ai_blocks._regime_view("") == "", "空 regime 应空块"
    assert ai_blocks._regime_view("never/seen") == "", "无历史 regime 应空块"


def test_regime_tag_coarse_and_degrades():
    """P2：regime tag 粗粒度 趋势/情绪；数据缺则降级 flat/neutral（不抛）。"""
    import agent_loop as al
    up = {"上证指数": {"ma20": 3000.0, "last": [{"close": 3100.0}]}}
    assert al._regime_tag(up, {"limit_up": 80, "limit_down": 5}) == "up/hot"
    down = {"上证指数": {"ma20": 3000.0, "last": [{"close": 2900.0}]}}
    assert al._regime_tag(down, {"limit_up": 5, "limit_down": 40}) == "down/cold"
    assert al._regime_tag({}, {}) == "flat/neutral", "缺数据未降级"


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
