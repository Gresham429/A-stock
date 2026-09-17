"""tests/test_alerts_routes.py  Blueprint：临时目录 + Flask 测试客户端，不 import app.py。

**为什么单独测路由**：提醒的配置是「整表替换」加两个文本框，最危险的错误是
跨用户串数据（把 A 的手机配到 B 的账号上）与「保存失败但界面以为成功」。
这里用两个假账号往返一遍，确认只能改到自己的库、解析错会返回 400 而不是静默丢弃。
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"
from flask import Flask, g, request
import userctx, alerts, alerts_store, alerts_routes

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

TMP = tempfile.mkdtemp()
# 个人库必须落到临时目录：user_dir() 用的是模块级 USERS_DIR（import 时由 DATA_DIR 算出来），
# 只改 DATA_DIR 不够，测试会写进仓库的 data/users/ 并互相污染（2026-09-17 踩过）。
userctx.DATA_DIR = TMP
userctx.USERS_DIR = os.path.join(TMP, "users")
alerts_routes.picks_pipeline.visible_horizons = lambda: ["short"]
alerts._market_open = lambda: True
alerts.ds.tencent_quote = lambda codes: {"600519": {"price": 10.2}}

app = Flask(__name__)
app.register_blueprint(alerts_routes.bp)
@app.before_request
def _fake_gate():
    # 每个请求自带 uid：不能用 app.config 存，那是一次修改、后续所有客户端都跟着变，
    # 会把「u2 保存后 u1 再看一次」变成「u2 看自己」，隔离性就测不出来了。
    g.uid = request.headers.get("X-Test-Uid") or "u1"
    userctx.set_uid(g.uid)

def client():
    return app.test_client()

def hdr(uid):
    return {"X-Test-Uid": uid}

def test_roundtrip_and_isolation():
    c = client()
    r = c.post("/api/alerts/targets", json={"targets": "log\nbark key1 我的iPhone"}, headers=hdr("u1"))
    ck(r.status_code == 200 and r.get_json()["count"] == 2, f"保存两台手机: {r.get_json()}")
    r2 = c.get("/api/alerts", headers=hdr("u1"))
    ck(r2.get_json()["status"]["targets"] == 2, f"status 应记 2 台: {r2.get_json()['status']}")
    ck(r2.get_json()["targets"][0] == "log", f"反解应可编辑: {r2.get_json()['targets']}")
    # 另一个账号看不到、也改不到 u1 的配置
    ck(c.get("/api/alerts", headers=hdr("u2")).get_json()["status"]["targets"] == 0,
       "u2 不该看到 u1 的手机")
    c.post("/api/alerts/targets", json={"targets": "log u2自己的"}, headers=hdr("u2"))
    ck(c.get("/api/alerts", headers=hdr("u2")).get_json()["status"]["targets"] == 1,
       "u2 应看到自己那 1 台")
    ck(c.get("/api/alerts", headers=hdr("u1")).get_json()["status"]["targets"] == 2,
       "u2 保存不该动 u1 的配置")

def test_bad_input_returns_400_and_keeps_old_config():
    c = client()
    c.post("/api/alerts/targets", json={"targets": "log"}, headers=hdr("u3"))
    bad = c.post("/api/alerts/targets", json={"targets": "telegram 不支持"}, headers=hdr("u3"))
    ck(bad.status_code == 400 and bad.get_json()["ok"] is False, f"未知渠道应 400: {bad.status_code}")
    ck(c.get("/api/alerts", headers=hdr("u3")).get_json()["status"]["targets"] == 1,
       "失败不该清掉原配置")

def test_points_and_dry_run_check():
    c = client()
    r = c.post("/api/alerts/points", json={"points": "600519 10 10.5 - - 9 等回踩"}, headers=hdr("u4"))
    ck(r.status_code == 200 and r.get_json()["count"] == 1, f"保存手设点位: {r.get_json()}")
    bad = c.post("/api/alerts/points", json={"points": "600519 11 10"}, headers=hdr("u4"))
    ck(bad.status_code == 400, "下界大于上界应 400")
    j = c.post("/api/alerts/check", json={"dry_run": True}, headers=hdr("u4")).get_json()
    ck(j["ok"] and j["sent"] == 1 and j["hits"][0]["source"] == "manual",
       f"立即检查应命中手设买点: {j}")

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
    print(f"\n{len(fns) - failed}/{len(fns)} 通过（{N[0]} 断言）")
    sys.exit(1 if failed else 0)
