"""ratelimit 单测（零依赖离线）：临时 usage.db，不起 Flask 应用、不打网络。

钉住 D3「1 单位 = 1 次真实 DeepSeek 调用」：check() 对 ai 桶只做门不计数、heavy 按请求计数、
计数只在 record_ai_call()、allow_llm 的全站/个人两层预算、system 只受全站约束、
日期键按北京时间、consume() 已删除。

跑：python3 tests/test_ratelimit.py
"""
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ratelimit  # noqa: E402

ratelimit.DB_PATH = os.path.join(tempfile.mkdtemp(), "usage.db")
ratelimit.init()
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def test_bucket_of():
    """路径归桶：GET 一律不计；scenario 零 LLM 不计；deepen 归 heavy；<id>/run 归 ai、/runs 不计。"""
    b = ratelimit.bucket_of
    ck(b("/api/recommend/daily", "POST") == "ai", "recommend/daily 应归 ai")
    ck(b("/api/recommend/daily", "GET") == "", "GET 不计")
    ck(b("/api/rules/scenario", "POST") == "", "rules/scenario 零 LLM 不应计费")
    ck(b("/api/news/deepen", "POST") == "heavy", "news/deepen 应归 heavy")
    ck(b("/api/agents/1/run", "POST") == "ai", "agents/<id>/run 应归 ai")
    ck(b("/api/agents/1/runs", "POST") == "", "agents/<id>/runs 不应计费")
    ck(b("/api/agents/run_all", "POST") == "ai", "run_all 应归 ai")
    ck(b("/api/universe/refresh", "POST") == "heavy", "universe/refresh 应归 heavy")
    ck(b("/api/watchlist", "POST") == "", "普通写路径不计")
    # 唯一的 GET 例外：/api/market/overview?refresh=1 绕过缓存直接调 v4-pro
    ck(b("/api/market/overview", "GET", refresh=True) == "ai", "market/overview?refresh=1 应归 ai")
    ck(b("/api/market/overview", "GET", refresh=False) == "", "market/overview 不带 refresh 不计")
    ck(b("/api/market/overview", "GET") == "", "market/overview 默认不计")
    ck(b("/api/config", "GET", refresh=True) == "", "refresh 只对 market/overview 生效")


def test_check_refresh_passthrough():
    """check() 把 refresh 透传给 bucket_of：GET overview?refresh=1 过最小间隔门，不计数。"""
    ok, why, code = ratelimit.check("ref", "/api/market/overview", "GET", refresh=True)
    ck(ok and code == 200, f"第一次应放行: {(ok, why, code)}")
    ok, why, code = ratelimit.check("ref", "/api/market/overview", "GET", refresh=True)
    ck(not ok and code == 429 and "太密集" in why, f"最小间隔内第二次 refresh 应 429: {(ok, why)}")
    ok, why, code = ratelimit.check("ref", "/api/market/overview", "GET", refresh=False)
    ck(ok and code == 200, f"不带 refresh 的 GET 不受 AI 最小间隔约束: {(ok, why, code)}")
    ck(ratelimit.my_usage("ref")["ai_used"] == 0, "GET refresh 在 HTTP 层不计数")


def test_ai_cache_shared_kinds():
    """ai_cache.SHARED_KINDS（macro/profile）跨用户共用；其余 kind 按 uid 隔离。用临时 _CACHE_FILE。"""
    import ai_cache
    import userctx
    old_file = ai_cache._CACHE_FILE
    ai_cache._CACHE_FILE = os.path.join(tempfile.mkdtemp(), "ai_cache.json")
    try:
        ck(ai_cache.SHARED_KINDS == frozenset({"macro", "profile"}), f"SHARED_KINDS 应为 macro/profile: {ai_cache.SHARED_KINDS}")
        inputs = {"code": "600519"}
        with userctx.as_user("alice"):
            ai_cache.put("macro", inputs, {"r": "m"})
            ai_cache.put("profile", inputs, {"r": "p"})
            ai_cache.put("entry", inputs, {"r": "e"})
        with userctx.as_user("bob"):
            hit = ai_cache.get("macro", inputs)
            ck(hit is not None and hit["result"] == {"r": "m"}, f"macro 应跨用户命中: {hit}")
            hit = ai_cache.get("profile", inputs)
            ck(hit is not None and hit["result"] == {"r": "p"}, f"profile 应跨用户命中: {hit}")
            ck(ai_cache.get("entry", inputs) is None, "entry 注入了个人数据，不应跨用户命中")
        with userctx.as_user("alice"):
            ck(ai_cache.get("entry", inputs)["result"] == {"r": "e"}, "entry 本人应命中")
        ck(ai_cache.get("macro", inputs) is not None, "无用户上下文（调度器）也应命中 macro")
        ck(ai_cache._key("macro", inputs).split(":")[1] == "-", "macro 的 uid 段应固定为 -")
        with userctx.as_user("alice"):
            ck(ai_cache._key("macro", inputs).split(":")[1] == "-", "有用户时 macro 的 uid 段仍为 -")
            ck(ai_cache._key("entry", inputs).split(":")[1] == "alice", "entry 的 uid 段应为当前用户")
    finally:
        ai_cache._CACHE_FILE = old_file


def test_check_ai_gate_does_not_count():
    """check 对 ai 路径放行且不计数；heavy 路径按请求计数。"""
    ok, why, code = ratelimit.check("bob", "/api/recommend/daily", "POST")
    ck(ok and code == 200 and why == "", f"应放行: {(ok, why, code)}")
    ck(ratelimit.my_usage("bob")["ai_used"] == 0, "ai 桶不应在 HTTP 层计数")
    ok, why, code = ratelimit.check("bob", "/api/recommend/daily", "POST")
    ck(not ok and code == 429 and "太密集" in why, f"最小间隔内第二次应 429: {(ok, why)}")
    ck(ratelimit.my_usage("bob")["ai_used"] == 0, "被拦的请求更不该计数")
    ok, _, code = ratelimit.check("bob", "/api/universe/refresh", "POST")
    ck(ok and ratelimit.my_usage("bob")["heavy_used"] == 1, "heavy 应按请求计 1")
    ratelimit.check("bob", "/api/news/deepen", "POST")
    ck(ratelimit.my_usage("bob")["heavy_used"] == 2, "heavy 第二次应计 2")
    ok, _, _ = ratelimit.check("bob", "/api/config", "GET")
    ck(ok and ratelimit.my_usage("bob")["heavy_used"] == 2, "GET 不应计数")


def test_heavy_per_user_cap():
    """heavy 到个人上限返回 429，文案含北京时间。"""
    old = ratelimit.HEAVY_PER_USER_DAY
    ratelimit.HEAVY_PER_USER_DAY = 2
    try:
        ok, why, code = ratelimit.check("bob", "/api/factors/backtest", "POST")
        ck(not ok and code == 429 and "北京时间" in why, f"heavy 满应 429: {(ok, why)}")
        ck(ratelimit.my_usage("bob")["heavy_used"] == 2, "被拦的 heavy 不应计数")
    finally:
        ratelimit.HEAVY_PER_USER_DAY = old


def test_record_ai_call_counts():
    """record_ai_call 后 ai_used==1、全站计数含它；空 uid 记在 system；calls.path 记模型名。"""
    ratelimit.record_ai_call("bob", "deepseek-v4-pro")
    u = ratelimit.my_usage("bob")
    ck(u["ai_used"] == 1, f"record_ai_call 后 ai_used 应为 1: {u}")
    ck(u["ai_global_used"] == 1, f"全站计数应含 bob: {u}")
    ratelimit.record_ai_call("", "deepseek-v4-flash")
    rows = ratelimit.all_usage(1)
    ck(any(r["uid"] == ratelimit.SYSTEM_UID and r["ai"] == 1 for r in rows), f"空 uid 应记 system: {rows}")
    ck(ratelimit.my_usage("bob")["ai_global_used"] == 2, "全站计数应含 system")
    with ratelimit._conn() as c:
        paths = [r["path"] for r in c.execute("SELECT path FROM calls WHERE bucket='ai'")]
    ck(sorted(paths) == ["deepseek-v4-flash", "deepseek-v4-pro"], f"calls.path 应记模型名: {paths}")
    ratelimit.record_llm_tokens("bob", 100, 20)
    ratelimit.record_llm_tokens("bob", 1, 2)
    ck(ratelimit.my_usage("bob")["tokens"] == {"prompt": 101, "completion": 22}, "token 应累加")


def test_day_key_is_shanghai():
    """日期键等于 Asia/Shanghai 今日（服务器时区无关）。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    ck(ratelimit._today() == today, f"_today 应为北京时间: {ratelimit._today()} vs {today}")
    ck(ratelimit.my_usage("bob")["day"] == today, "my_usage.day 应为北京时间")
    ck(all(r["day"] == today for r in ratelimit.all_usage(1)), "all_usage 的 day 应为北京时间")


def test_allow_llm_global_cap():
    """全站满：allow_llm 对任何人（含 system）False 且文案含「北京时间」；check 也 429。"""
    ok, why = ratelimit.allow_llm("bob")
    ck(ok and why == "", f"正常应放行: {(ok, why)}")
    old = ratelimit.AI_GLOBAL_DAY
    ratelimit.AI_GLOBAL_DAY = 2      # 已有 bob 1 + system 1
    try:
        ok, why = ratelimit.allow_llm("bob")
        ck(not ok and "北京时间" in why and "全站" in why, f"全站满应 False: {(ok, why)}")
        ok, why = ratelimit.allow_llm("")
        ck(not ok, "system 也受全站上限约束")
        ok, why, code = ratelimit.check("carol", "/api/recommend/daily", "POST")
        ck(not ok and code == 429 and "全站" in why, f"check 在全站满时应 429: {(ok, why, code)}")
        ck(ratelimit.my_usage("carol")["ai_used"] == 0, "被拦的不计")
    finally:
        ratelimit.AI_GLOBAL_DAY = old


def test_allow_llm_personal_cap():
    """个人满：本人 False；别人与 system 不受影响。"""
    old = ratelimit.AI_PER_USER_DAY
    ratelimit.AI_PER_USER_DAY = 1    # bob 已用 1
    try:
        ok, why = ratelimit.allow_llm("bob")
        ck(not ok and "北京时间" in why, f"个人满应 False: {(ok, why)}")
        ok, _ = ratelimit.allow_llm("carol")
        ck(ok, "别人不受 bob 个人上限影响")
        ok, _ = ratelimit.allow_llm("")
        ck(ok, "system 不受个人上限约束")
        ratelimit.record_ai_call("", "deepseek-v4-flash")
        ck(ratelimit.allow_llm("")[0], "system 记了 2 次仍不受个人上限约束")
        # 舰队：记在 FLEET_UID 名下、单独一行，同样只受全站约束
        ratelimit.record_ai_call(ratelimit.FLEET_UID, "deepseek-v4-pro")
        ratelimit.record_ai_call(ratelimit.FLEET_UID, "deepseek-v4-pro")
        ck(ratelimit.allow_llm(ratelimit.FLEET_UID)[0], "舰队记了 2 次仍不受个人上限约束")
        ck(ratelimit.my_usage(ratelimit.FLEET_UID)["ai_used"] == 2, "舰队应单独一行记账")
        ck(ratelimit.my_usage("bob")["ai_used"] == 1, "舰队调用不应算进个人")
    finally:
        ratelimit.AI_PER_USER_DAY = old


def test_req_per_min_window():
    """每分钟总请求数窗口：超过 REQ_PER_MIN 返回 429（内存态，进程内）。"""
    old = ratelimit.REQ_PER_MIN
    ratelimit.REQ_PER_MIN = 3
    try:
        codes = [ratelimit.check("zoe", "/api/config", "GET")[2] for _ in range(4)]
        ck(codes == [200, 200, 200, 429], f"第 4 次应 429: {codes}")
    finally:
        ratelimit.REQ_PER_MIN = old


def test_consume_removed_and_purge():
    """consume() 已删除（调度器不再预扣）；purge 不误删今天的数据。"""
    ck(not hasattr(ratelimit, "consume"), "ratelimit.consume 应已删除")
    ratelimit.purge(60)
    ck(ratelimit.my_usage("bob")["ai_used"] == 1, "purge 不应删今天的计数")
    with ratelimit._conn() as c:
        c.execute("INSERT INTO usage(day,uid,bucket,n) VALUES('2000-01-01','old','ai',5)")
        c.execute("INSERT INTO calls(ts,uid,bucket,path) VALUES('2000-01-01T00:00:00','old','ai','m')")
    ck(ratelimit.purge(60) == 1, "purge 应删掉旧 calls")
    with ratelimit._conn() as c:
        ck(c.execute("SELECT COUNT(*) n FROM usage WHERE day='2000-01-01'").fetchone()["n"] == 0,
           "purge 应删掉旧 usage")


if __name__ == "__main__":
    for fn in (test_bucket_of, test_check_refresh_passthrough, test_ai_cache_shared_kinds,
               test_check_ai_gate_does_not_count, test_heavy_per_user_cap,
               test_record_ai_call_counts, test_day_key_is_shanghai, test_allow_llm_global_cap,
               test_allow_llm_personal_cap, test_req_per_min_window, test_consume_removed_and_purge):
        fn()   # 有顺序依赖（计数累积），不按字母序
    print(f"OK — test_ratelimit 全过（{_n} 断言）")
