"""tests/test_picks_routes.py  Blueprint：临时目录 + Flask 测试客户端，不 import app.py。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"
from flask import Flask, g
import userctx, picks_store as ps, picks_pipeline as pp, picks_routes

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

tmp = tempfile.mkdtemp()
ps.DB_PATHS["public"] = os.path.join(tmp, "pub.db"); ps.DB_PATHS["watchlist"] = os.path.join(tmp, "wl.db")
ps.init("public"); ps.init("watchlist"); pp.LOCK_DIR = tmp
picks_routes.auth.get_user = lambda uid: {"uid": uid, "is_admin": uid == "boss"}
picks_routes._market_ctx = lambda: None          # 不要在测试里 import app
import news_store
news_store.is_trading_day = lambda d=None: True  # 零网络
userctx.set_fleet_uid("owner")

app = Flask(__name__)
app.register_blueprint(picks_routes.bp)
@app.before_request
def _fake_gate():
    g.uid = app.config.get("TEST_UID", "friend")
    userctx.set_uid(g.uid)

def prop(code):
    return {"code": code, "name": "N", "horizon": "short", "stance": "buy", "entry_lo": 9.5, "entry_hi": 9.8,
            "exit_lo": 10.8, "exit_hi": 11.2, "stop": 9.2, "thesis": "t", "trigger_note": "n", "basis_json": "{}",
            "decision": "new", "trigger": "none", "px_at_call": 9.9}

def test_public_and_chain_on_fresh_db():
    """首次部署/重启后 16:00 跑批之前，表还没建过——读路由不能 500（Fix round 1 critical）。"""
    orig_public, orig_watchlist = ps.DB_PATHS["public"], ps.DB_PATHS["watchlist"]
    fresh = tempfile.mkdtemp()
    ps.DB_PATHS["public"] = os.path.join(fresh, "pub_fresh.db")
    ps.DB_PATHS["watchlist"] = os.path.join(fresh, "wl_fresh.db")
    try:
        c = app.test_client()
        r = c.get("/api/picks/public")
        j = r.get_json()
        ck(r.status_code == 200 and j["calls"] == [] and j["generated_at"] is None, "空库 public 不 500")
        r = c.get("/api/picks/chain/600519?scope=public")
        ck(r.status_code == 200 and r.get_json()["chain"] == [], "空库 chain(public) 不 500")
        r = c.get("/api/picks/chain/600519?scope=watchlist")
        ck(r.status_code == 200, "空库 chain(watchlist) 不 500")
        r = c.get("/api/picks/chain/600519?scope=bogus")
        ck(r.status_code == 400, "非法 scope 仍 400")
    finally:
        ps.DB_PATHS["public"] = orig_public
        ps.DB_PATHS["watchlist"] = orig_watchlist

def test_public_and_chain():
    ps.apply("public", prop("600519"), "2026-09-16", "r1")
    c = app.test_client()
    j = c.get("/api/picks/public?horizon=short").get_json()
    ck(len(j["calls"]) == 1 and j["calls"][0]["code"] == "600519", "公共列表")
    j = c.get("/api/picks/chain/600519?scope=public").get_json()
    ck(len(j["chain"]) == 1, "观点链")

def test_watchlist_is_per_user():
    c = app.test_client()
    with userctx.as_user("friend"):
        ps.apply("watchlist", prop("000001"), "2026-09-16", "r")
    j = c.get("/api/picks/watchlist").get_json()
    ck([x["code"] for x in j["calls"]] == ["000001"], "当前用户的自选股观点")

def test_run_permissions_and_status():
    c = app.test_client()
    pp.run_watchlist = lambda *a, **k: {"calls": 0, "error": None}
    pp.run_public = lambda *a, **k: {"calls": 0, "error": None}
    r = c.post("/api/picks/run_public")
    ck(r.status_code == 403, "普通用户不能跑公共")
    app.config["TEST_UID"] = "boss"
    r = c.post("/api/picks/run_public")
    ck(r.status_code == 200 and r.get_json()["status"] in ("started", "running"), "管理员可跑公共")
    r = c.post("/api/picks/run")
    ck(r.status_code == 200, "个人可跑")
    j = c.get("/api/picks/status").get_json()
    ck("public_running" in j and "watchlist_running" in j, "状态字段")

if __name__ == "__main__":
    for fn in (test_public_and_chain_on_fresh_db, test_public_and_chain,
               test_watchlist_is_per_user, test_run_permissions_and_status):
        fn()
    print(f"OK — test_picks_routes 全过（{N[0]} 断言）")
