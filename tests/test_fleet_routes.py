"""舰队站长上下文 + 公共任务无用户上下文 单测（零依赖离线，临时目录，不打网络）。

D1/D2/D6：舰队全站只有一套 = 站长的库。钉住：
任何用户请求下 ai_blocks 三个只读视图读的都是站长库；/api/agents GET 对普通用户开放、写操作
非站长非管理员 403、站长未设置 503；run_all 的线程与线程池都带站长 uid；
store.load_all_watchlists 并集且坏文件跳过；news_store._watch_codes 无用户时取并集。

import app 会挂 auth/ratelimit，二者的 DB 都指到临时目录；共享库（news/templates/factors）
只做幂等建表。跑：python3 tests/test_fleet_routes.py
"""
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"            # import app 不打网络，但保险起见绕开终端代理
os.environ["ASTOCK_FLEET_OWNER"] = "owner"
[os.environ.pop(k, None) for k in ("ASTOCK_COOKIE_SECURE", "ASTOCK_ENV")]

import userctx  # noqa: E402

TMP = tempfile.mkdtemp()
userctx.USERS_DIR = os.path.join(TMP, "users")   # 个人库全落临时目录，不碰 data/users/

import auth  # noqa: E402
import ratelimit  # noqa: E402

auth.DB_PATH = os.path.join(TMP, "auth.db")
ratelimit.DB_PATH = os.path.join(TMP, "usage.db")

import app as web  # noqa: E402
import agent_loop  # noqa: E402
import agent_store  # noqa: E402
import ai_blocks  # noqa: E402
import news_store  # noqa: E402
import store  # noqa: E402
from flask import g  # noqa: E402

PW = "Passw0rd!xyz"
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def call(view, uid, **kw):
    """以某个已登录用户的身份直接调用视图（绕过 cookie，只测舰队装饰器本身）。"""
    with web.app.test_request_context(json={}):
        g.uid = uid
        userctx.set_uid(uid)
        try:
            r = view(**kw)
        finally:
            userctx.set_uid("")
    code = r[1] if isinstance(r, tuple) else 200
    return code, (r[0] if isinstance(r, tuple) else r).get_json()


def _udir(uid: str) -> str:
    return os.path.join(userctx.USERS_DIR, uid)


def test_setup_users():
    auth.init()
    auth.create_user("owner", PW)
    auth.create_user("boss", PW, is_admin=True)
    auth.create_user("friend", PW)
    ck(userctx.fleet_uid() == "owner", "环境变量应指定站长")


def test_views_read_fleet_db():
    """普通用户请求下三个视图读的是站长库：站长 journal 里的记录对 friend 可见；friend 库不被建。"""
    web.ensure_user_stores("owner")
    with userctx.as_fleet():
        agent_store.journal_add(1, "000001", "平安银行", "2026-09-15", "早盘", "震荡", {}, "buy", "站长的理由")
    with userctx.as_user("friend"):
        hv = ai_blocks._stock_house_view("000001")
        ck(isinstance(hv, str) and "000001" in hv and "站长的理由" in hv, f"house-view 应读站长库: {hv!r}")
        rv = ai_blocks._regime_view("震荡")
        ck(isinstance(rv, str) and "震荡" in rv, f"regime-view 应读站长库: {rv!r}")
        ck(isinstance(ai_blocks._lesson_block(), str), "lesson_block 应返回字符串")
        ck(ai_blocks._regime_view("") == "", "regime 空应空块")
    ck(userctx.get_uid() == "", "视图退出后应恢复无用户")
    ck(not os.path.exists(os.path.join(_udir("friend"), "agents.db")), "friend 的 agents.db 不应被建")
    ck(os.path.exists(os.path.join(_udir("owner"), "agents.db")), "站长的 agents.db 应存在")


def test_views_empty_when_no_fleet():
    """站长未设置：三个视图不抛、返回空串（与 journal 空时空块一致）。"""
    os.environ["ASTOCK_FLEET_OWNER"] = ""
    userctx.set_fleet_uid("")
    try:
        ck(ai_blocks._lesson_block() == "", "无站长 lesson_block 应空")
        ck(ai_blocks._stock_house_view("000001") == "", "无站长 house-view 应空")
        ck(ai_blocks._regime_view("震荡") == "", "无站长 regime-view 应空")
        ck(call(web.api_agents, "friend")[0] == 503, "无站长 /api/agents 应 503")
        ck(call(web.api_agents_run_all, "boss")[0] == 503, "无站长 run_all 应 503（先于权限门）")
    finally:
        os.environ["ASTOCK_FLEET_OWNER"] = "owner"


def test_agents_routes_permissions():
    """GET 对普通用户 200 且读站长库；写操作 friend 403、站长与管理员 200；线程带站长 uid。"""
    code, body = call(web.api_agents, "friend")
    ck(code == 200 and "agents" in body and "lessons" in body, f"普通用户 GET 应 200: {code} {body}")
    ck(call(web.api_agents_runs, "friend", gid=1)[0] == 200, "普通用户 GET runs 应 200")
    ck(call(web.api_agents_run_all, "friend")[0] == 403, "普通用户 run_all 应 403")
    ck(call(web.api_agents_delete, "friend", gid=1)[0] == 403, "普通用户 DELETE 应 403")
    ck(call(web.api_agents_create, "friend")[0] == 403, "普通用户 POST 建 agent 应 403")
    seen = []
    done = threading.Event()
    orig = agent_loop.run_all

    def fake_run_all(dry_run=False, **kw):
        seen.append((userctx.get_uid(), threading.current_thread().name, dry_run))
        done.set()
    agent_loop.run_all = fake_run_all
    try:
        for who in ("owner", "boss"):
            done.clear()
            code, body = call(web.api_agents_run_all, who)
            ck(code == 200 and body.get("running") is True and body.get("agents") == 0,
               f"{who} run_all 应 200: {code} {body}")
            ck(done.wait(5), "后台线程应跑起来")
    finally:
        agent_loop.run_all = orig
    ck([u for u, _, _ in seen] == ["owner", "owner"], f"后台线程应带站长 uid: {seen}")
    ck(all(t != threading.current_thread().name for _, t, _ in seen), "run_all 应在后台线程")
    ck(call(web.api_agents_delete, "boss", gid=999)[0] == 200, "管理员 DELETE 不存在的 agent 应 200")
    ck(not os.path.exists(os.path.join(_udir("boss"), "agents.db")), "管理员操作舰队不应建自己的 agents.db")


def test_run_all_pool_carries_fleet_uid():
    """D2：as_fleet 下 run_all 的线程池每个任务都读到站长 uid（不调 LLM、不看日历）。"""
    seen = {}
    origs = (agent_loop.run_day, agent_store.list_agents, news_store.is_trading_day)

    def fake_run_day(agent_id, **kw):
        seen[agent_id] = (userctx.get_uid(), threading.current_thread().name)
        return {"ok": True, "agent": f"a{agent_id}"}
    agent_loop.run_day = fake_run_day
    agent_store.list_agents = lambda active_only=False: [{"id": i, "name": f"a{i}"} for i in (1, 2, 3)]
    news_store.is_trading_day = lambda d=None: True
    try:
        with userctx.as_fleet():
            out = agent_loop.run_all(require_open=False, dry_run=True)
    finally:
        agent_loop.run_day, agent_store.list_agents, news_store.is_trading_day = origs
    ck(len(out) == 3 and all(r.get("ok") for r in out), f"三个 agent 都应返回: {out}")
    ck(sorted(seen) == [1, 2, 3] and all(u == "owner" for u, _ in seen.values()), f"池线程应读到站长: {seen}")
    ck(all(t != threading.current_thread().name for _, t in seen.values()), "应在池线程里跑")
    ck(userctx.get_uid() == "", "退出 as_fleet 后应无用户")


def test_load_all_watchlists_union():
    """并集按 code 去重、记录来源 uid、坏文件与非列表跳过；无用户时 _watch_codes 取并集。"""
    with userctx.as_user("owner"):
        store.save_watchlist(["000001", "000002"])
    with userctx.as_user("friend"):
        store.save_watchlist(["000002", "000003"])
    os.makedirs(_udir("boss"), exist_ok=True)
    with open(os.path.join(_udir("boss"), "watchlist.json"), "w", encoding="utf-8") as f:
        f.write("{bad json")
    os.makedirs(_udir("zed"), exist_ok=True)
    with open(os.path.join(_udir("zed"), "watchlist.json"), "w", encoding="utf-8") as f:
        json.dump({"codes": "not-a-list"}, f)
    u = store.load_all_watchlists()
    ck(sorted(r["code"] for r in u) == ["000001", "000002", "000003"] and len(u) == 3, f"并集错: {u}")
    ck(sorted(next(r for r in u if r["code"] == "000002")["uids"]) == ["friend", "owner"], f"来源 uid 错: {u}")
    ck(all("boss" not in r["uids"] and "zed" not in r["uids"] for r in u), "坏文件/非列表应跳过")
    ck(sorted(news_store._watch_codes()) == ["000001", "000002", "000003"], "无用户应取并集")
    with userctx.as_user("friend"):
        ck(news_store._watch_codes() == ["000002", "000003"], "有用户应取他自己的")
    ck(userctx.get_uid() == "", "结束应无用户")


def test_agent_watch_waits_for_fleet_owner():
    """全新安装「先起 app、后 adduser」也要能在站长出现后补启盘中调度器。

    修前 app.__main__ 只在启动瞬间判一次 `fleet_uid()`：为空就只打一行 warning，
    盘中调度器永远不启动，要重启 app 才恢复（那段时间 agent 静默不跑）。
    """
    calls: list = []
    seq = ["", "", "owner"]
    orig_fleet, orig_sched = web.userctx.fleet_uid, web._agent_scheduler
    try:
        web.userctx.fleet_uid = lambda: seq.pop(0) if seq else "owner"
        web._agent_scheduler = lambda: calls.append(web.userctx.get_uid())
        web._agent_watch(poll_sec=0, max_polls=5)
    finally:
        web.userctx.fleet_uid = orig_fleet
        web._agent_scheduler = orig_sched
    ck(calls == ["owner"], f"应在站长出现后、于站长上下文里启动调度器: {calls}")


if __name__ == "__main__":
    for fn in (test_setup_users, test_views_read_fleet_db, test_views_empty_when_no_fleet,
               test_agents_routes_permissions, test_run_all_pool_carries_fleet_uid,
               test_load_all_watchlists_union, test_agent_watch_waits_for_fleet_owner):
        fn()   # 有顺序依赖（先建用户），不按字母序
    print(f"OK — test_fleet_routes 全过（{_n} 断言）")
