"""tests/test_llm_picks.py  提示词函数：monkeypatch llm._chat，验证校验逻辑。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import llm
import llm_picks as lp

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

LEVELS = {"600519": {"price": 10.0, "atr_pct": 2.0, "levels": [{"px": 9.5, "kind": "lo20"}, {"px": 11.0, "kind": "hi20"}, {"px": 9.2, "kind": "atr_lo2"}]}}
CANDS = [{"code": "600519", "name": "贵州茅台", "primary": "食品", "sub": "白酒", "price": 10.0, "pe_ttm": 20, "pb": 5,
          "vol": 30, "cum20": 3.0, "range_pos": 40, "net20": 1.2, "turnover": 1.5, "lot_cost": 1000}]

def fake_chat(reply):
    def _c(messages, **kw):
        _c.last = messages
        return json.dumps(reply, ensure_ascii=False)
    return _c

def test_validate_snaps_and_drops_unknown():
    parsed = {"calls": [
        {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy", "entry": [9.7, 9.9], "exit": [10.9, 11.3],
         "stop": 8.0, "decision": "bogus", "trigger": "xx", "thesis": "t", "trigger_note": "n", "basis": {"signals": ["区间位置"], "rules": ["R1"]}},
        {"code": "000000", "name": "不在候选", "horizon": "short", "stance": "buy", "entry": [1, 2], "exit": [3, 4], "stop": 0.5}]}
    out = lp.validate_calls(parsed, LEVELS, {"600519"})
    ck(len(out) == 1, "不在候选的被丢弃")
    c = out[0]
    ck(c["entry_lo"] == 9.5 and c["stop"] == 9.2, f"价位吸附到候选: {c['entry_lo']} {c['stop']}")
    ck(c["decision"] == "new" and c["trigger"] == "none", "非法 decision/trigger 置默认")
    ck(json.loads(c["basis_json"])["adjusted"] is True, "被调整过的标 adjusted")
    ck(isinstance(c["basis_json"], str), "basis 存字符串")

def test_validate_survives_malformed_rows():
    parsed = {"calls": [
        "not a dict",
        {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy", "entry": 9.6, "stop": 9.2,
         "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n", "basis": {}},
        {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy", "entry": [9.5, 9.6], "exit": [10.9, 11.3],
         "stop": 9.2, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n", "basis": {}}]}
    out = lp.validate_calls(parsed, LEVELS, {"600519"})
    ck(len(out) == 2, f"非字典项被跳过、其余两条按条通过: {len(out)}")
    b, c = out
    ck(b["entry_lo"] == b["entry_hi"] == 9.5, f"标量区间吸附成 [x,x] 再吸附到候选: {b['entry_lo']} {b['entry_hi']}")
    ck(b["exit_lo"] is None, "缺失 exit 置空而不是抛异常")
    ck(c["code"] == "600519", "第三条完整行正常通过")

def test_horizon_picks_prompt_and_parse():
    reply = {"calls": [{"code": "600519", "name": "贵州茅台", "horizon": "mid", "stance": "buy", "entry": [9.5, 9.6],
                        "exit": [11.0, 11.0], "stop": 9.2, "decision": "new", "trigger": "none", "thesis": "板块强", "trigger_note": "破位走", "basis": {}}]}
    orig = llm._chat
    llm._chat = fake_chat(reply)
    try:
        out = lp.horizon_picks("mid", CANDS, LEVELS, {"600519": "（该股尚无历史观点）"}, {"regime": "震荡"}, 10000)
        txt = llm._chat.last[1]["content"]
        ck("中线" in txt and "候选价位" in txt and "尚无历史观点" in txt, "提示词含周期、候选价位、记忆块")
        ck("维持" in txt and "触发" in txt, "提示词写明改口规则")
        ck(out[0]["horizon"] == "mid" and out[0]["px_at_call"] == 10.0, "输出带 horizon 与给出价")
    finally:
        llm._chat = orig

def test_watchlist_points_bad_json_returns_empty():
    orig = llm._chat
    llm._chat = lambda messages, **kw: "not json"
    try:
        out = lp.watchlist_points(CANDS, LEVELS, {}, None, 10000)
        ck(out == [], "解析失败返回空列表而不是抛")
    finally:
        llm._chat = orig

if __name__ == "__main__":
    for fn in (test_validate_snaps_and_drops_unknown, test_validate_survives_malformed_rows,
               test_horizon_picks_prompt_and_parse, test_watchlist_points_bad_json_returns_empty):
        fn()
    print(f"OK — test_llm_picks 全过（{N[0]} 断言）")
