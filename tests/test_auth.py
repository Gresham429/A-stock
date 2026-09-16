"""auth 单测（零依赖离线）：临时 auth.db + 最小 Flask 应用挂 auth.init_app，不 import app.py。
钉住 D5 的每一条：未登录 401、登录 CSRF、按 uid 与按 ip 的失败锁、停用账号通用文案、
会话表只存哈希、请求结束清上下文、保留名、站长解析。时间相关直接改表，不 sleep。

跑：python3 tests/test_auth.py
"""
import logging
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
[os.environ.pop(k, None) for k in ("ASTOCK_FLEET_OWNER", "ASTOCK_COOKIE_SECURE", "ASTOCK_ENV")]

import userctx  # noqa: E402

TMP = tempfile.mkdtemp()
userctx.DATA_DIR = TMP
userctx.USERS_DIR = os.path.join(TMP, "users")

import auth  # noqa: E402
import auth_password as pw  # noqa: E402
from flask import Flask, g, jsonify  # noqa: E402

auth.DB_PATH = os.path.join(TMP, "auth.db")
PW = "correct-horse-1"
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _db():
    return sqlite3.connect(auth.DB_PATH)


def _mk_app():
    a = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    auth.init_app(a)

    @a.route("/api/x")
    def x():  # noqa: ANN202
        return jsonify({"uid": g.uid, "ctx": userctx.get_uid()})

    @a.route("/api/w", methods=["POST"])
    def w():  # noqa: ANN202
        return jsonify({"ok": True})
    return a


app = _mk_app()
client = app.test_client()


def test_fleet_owner_resolution():
    """init() 后站长 = created_at 最早的管理员；环境变量优先；无管理员时为空。"""
    ck(auth.COOKIE_SECURE is False, "无 ASTOCK_ENV 时默认不开 Secure")
    auth.init()
    ck(userctx.fleet_uid() == "", "无管理员时不应设站长")
    auth.create_user("bob", PW)
    auth.create_user("alice", PW, is_admin=True)
    auth.create_user("carol", PW, is_admin=True)
    with _db() as db:   # 直接改 created_at 决定先后，不 sleep
        db.execute("UPDATE users SET created_at='2020-01-02T00:00:00+00:00' WHERE uid='alice'")
        db.execute("UPDATE users SET created_at='2020-01-01T00:00:00+00:00' WHERE uid='carol'")
    auth.init()
    ck(userctx.fleet_uid() == "carol" == auth.fleet_uid_hint(), f"应取最早管理员 carol，实得 {userctx.fleet_uid()!r}")
    os.environ["ASTOCK_FLEET_OWNER"] = "bob"
    ck(auth.fleet_uid_hint() == "bob" and userctx.fleet_uid() == "bob", "环境变量应优先")
    os.environ.pop("ASTOCK_FLEET_OWNER")
    ck(os.path.isdir(os.path.join(userctx.USERS_DIR, "bob")), "建用户应立即建数据目录")


def test_reserved_and_invalid_names():
    """保留名 system 不能注册；非法名/弱口令/重名都 ValueError。"""
    for uid, p in (("system", PW), ("fleet", PW), ("Bad", PW), ("bob", PW), ("dave", "123"), ("dave", "1234567890")):
        try:
            auth.create_user(uid, p)
            ck(False, f"create_user({uid!r}) 应被拒")
        except ValueError:
            ck(True, "")


def test_unauth_and_login_csrf():
    """未登录 /api/* 401 且 code=auth_required；页面 302；跨站 POST /login 403；同源放行。"""
    r = client.get("/api/x")
    ck(r.status_code == 401 and r.get_json()["code"] == "auth_required", f"未登录应 401: {r.status_code}")
    r = client.post("/login", data={"uid": "alice", "password": PW},
                    headers={"Origin": "https://evil.example"})
    ck(r.status_code == 403, f"跨站 POST /login 应 403，实得 {r.status_code}")
    r = client.post("/login", data={"uid": "alice", "password": PW},
                    headers={"Origin": "http://localhost"})
    ck(r.status_code == 302, f"同源登录应 302，实得 {r.status_code}")
    cookie = r.headers.get("Set-Cookie", "")
    ck("astock_sid=" in cookie and "HttpOnly" in cookie and "Secure" not in cookie
       and "SameSite=Strict" in cookie, f"cookie 标志错: {cookie}")


def test_session_stored_hashed_and_ctx_cleared():
    """sessions.token 只存 64 位 hex；登录后拿到 uid；请求结束 userctx 清空；跨站写 403。"""
    token = client.get_cookie(auth.COOKIE_NAME).value
    with _db() as db:
        rows = [r[0] for r in db.execute("SELECT token FROM sessions")]
    ck(rows and token not in rows, "会话表里不应有明文 token")
    ck(all(len(t) == 64 and int(t, 16) >= 0 for t in rows), f"应为 sha256 hex: {rows}")
    ck(auth._token_hash(token) in rows, "应按哈希落库")
    r = client.get("/api/x")
    ck(r.status_code == 200 and r.get_json() == {"uid": "alice", "ctx": "alice"}, f"登录后: {r.get_json()}")
    ck(userctx.get_uid() == "", f"请求结束后 uid 应清空: {userctx.get_uid()!r}")
    ck(client.post("/api/w", headers={"Origin": "https://evil.example"}).status_code == 403, "登录态跨站写应 403")
    ck(client.post("/api/w").status_code == 200, "无 Origin 的同源写应放行")
    # tailscale serve / 反代：Host 被改写成 127.0.0.1:5000，回环来源时信任 X-Forwarded-Host。
    # Host 变了 cookie jar 不会自动带 cookie（域名不同），所以再给 127.0.0.1 域种一次同一个 token。
    client.set_cookie(auth.COOKIE_NAME, token, domain="127.0.0.1")
    xfh_ok = {"Origin": "https://astock.example.ts.net", "Host": "127.0.0.1:5000",
              "X-Forwarded-Host": "astock.example.ts.net"}
    ck(client.post("/api/w", headers=xfh_ok, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200,
       "回环来源 + XFH 与 Origin 同源应放行")
    ck(client.post("/api/w", headers=xfh_ok, environ_base={"REMOTE_ADDR": "::1"}).status_code == 200,
       "::1 来源 + XFH 同源也应放行")
    xfh_bad = {**xfh_ok, "X-Forwarded-Host": "other.example.ts.net"}
    ck(client.post("/api/w", headers=xfh_bad, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 403,
       "回环来源 + XFH 与 Origin 不同源应 403")
    ck(client.post("/api/w", headers=xfh_ok, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 403,
       "非回环来源不信任 XFH，仍按 Host 比较应 403")
    ck(client.post("/api/w", headers={"Origin": "https://astock.example.ts.net", "Host": "127.0.0.1:5000"},
                   environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 403,
       "回环来源但无 XFH 时按 Host 比较应 403")
    # 滑动续期：10 分钟内不写；把 last_seen 改到很久前则续
    with _db() as db:
        seen0 = db.execute("SELECT last_seen FROM sessions").fetchone()[0]
    client.get("/api/x")
    with _db() as db:
        ck(db.execute("SELECT last_seen FROM sessions").fetchone()[0] == seen0, "10 分钟内不应续期")
        db.execute("UPDATE sessions SET last_seen='2020-01-01T00:00:00+00:00'")
    client.get("/api/x")
    with _db() as db:
        ck(db.execute("SELECT last_seen FROM sessions").fetchone()[0] != "2020-01-01T00:00:00+00:00",
           "超 10 分钟应续期")


def test_login_next_sanitized():
    """已登录 GET /login?next=... 只允许站内路径：制表符/换行/协议相对/绝对 URL 都回 /，且不 500。"""
    for nxt, want in (("/ok?x=1", "/ok?x=1"), ("/%09/evil.com", "/"), ("/%0a", "/"),
                      ("//evil.com", "/"), ("https://evil.com/", "/"), ("/a%20b", "/")):
        r = client.get("/login?next=" + nxt)
        ck(r.status_code == 302 and r.headers.get("Location") == want,
           f"next={nxt!r} 应 302 到 {want!r}，实得 {r.status_code} {r.headers.get('Location')!r}")


def test_logout_kick_disabled():
    """logout/kick 按哈希删会话；停用账号返回通用文案且踢掉会话。"""
    t, err = auth.login("bob", PW, ip="9.9.9.9")
    ck(t and auth.session_user(t) == "bob", f"登录失败: {err}")
    auth.logout(t)
    ck(auth.session_user(t) == "", "logout 后应失效")
    t2, _ = auth.login("bob", PW, ip="9.9.9.9")
    auth.set_disabled("bob", True)
    ck(auth.session_user(t2) == "", "停用应踢掉会话")
    tok, err = auth.login("bob", PW, ip="1.1.1.1")
    ck(tok == "" and err == "用户名或密码不对", f"停用账号文案应通用: {err!r}")
    auth.set_disabled("bob", False)
    t3, _ = auth.login("bob", PW, ip="9.9.9.9")
    ck(auth.kick("bob") == 1 and auth.session_user(t3) == "", "kick 应删掉会话")


def test_lock_by_uid():
    """同 uid 错密码 MAX_FAILS 次（换 ip）后锁定；其他 uid 不受影响。"""
    auth.create_user("dave", PW)
    for i in range(pw.MAX_FAILS):
        _, err = auth.login("dave", "wrong-password-x", ip=f"10.0.0.{i}")
        ck(err == "用户名或密码不对", err)
    _, err = auth.login("dave", PW, ip="10.0.9.9")
    ck(err.startswith("尝试次数过多"), f"应按 uid 锁定: {err}")
    tok, err = auth.login("carol", PW, ip="10.0.9.8")
    ck(bool(tok), f"别的 uid 不应受影响: {err}")
    # 锁到期后计数从零重来：再错 1 次不应立刻重新锁定
    with _db() as db:
        db.execute("UPDATE login_fails SET locked_until=1 WHERE uid='dave'")
    _, err = auth.login("dave", "wrong-password-x", ip="10.0.9.7")
    ck(err == "用户名或密码不对", f"锁到期后错 1 次不应再锁: {err}")
    with _db() as db:
        ck(db.execute("SELECT fails FROM login_fails WHERE uid='dave'").fetchone()[0] == 1, "到期后应从 1 重计")
    tok, err = auth.login("dave", PW, ip="10.0.9.6")
    ck(bool(tok), f"锁到期后正确密码应能登录: {err}")


def test_lock_by_ip():
    """同 ip 换 uid（含不存在的名字）也锁定；不存在的名字不进表；HTTP 路径信任本机 XFF。"""
    for name in ("nobody1", "nobody2", "carol", "zzz", "carol")[:pw.MAX_FAILS]:
        _, err = auth.login(name, "wrong-password-x", ip="8.8.8.8")
        ck(err == "用户名或密码不对", err)
    _, err = auth.login("carol", PW, ip="8.8.8.8")
    ck(err.startswith("尝试次数过多"), f"应按 ip 锁定: {err}")
    with _db() as db:
        keys = {r[0] for r in db.execute("SELECT uid FROM login_fails")}
    ck("ip:8.8.8.8" in keys and "nobody1" not in keys and "carol" in keys, f"锁定键集合错: {keys}")
    for i in range(pw.MAX_FAILS):
        r = client.post("/login", data={"uid": f"ghost{i}", "password": "wrong-password-x"},
                        headers={"X-Forwarded-For": "7.7.7.7"})
        ck(r.status_code == 401, "错密码应 401")
    r = client.post("/login", data={"uid": "alice", "password": PW},
                    headers={"X-Forwarded-For": "7.7.7.7"})
    ck(r.status_code == 401 and "尝试次数过多".encode() in r.data, "HTTP 路径应按 XFF ip 锁定")


def test_hash_burn_constant_work():
    """用户不存在 / 密码错 / 已停用 三条路径都恰好算 1 次哈希（防计时探测）。"""
    calls = []
    orig = pw.hash_password
    pw.hash_password = lambda p_, s_: (calls.append(1), orig(p_, s_))[1]
    try:
        auth.login("nobody-here", "wrong-password-x", ip="6.6.6.1"); n1 = len(calls); calls.clear()
        auth.login("carol", "wrong-password-x", ip="6.6.6.2"); n2 = len(calls); calls.clear()
        auth.set_disabled("carol", True)
        auth.login("carol", PW, ip="6.6.6.3"); n3 = len(calls)
    finally:
        pw.hash_password = orig
        auth.set_disabled("carol", False)
    ck(n1 == n2 == n3 == 1, f"哈希次数应一致: {(n1, n2, n3)}")


def test_fleet_owner_stable_and_lazy():
    """站长被停用后仍是站长；站长为空时不重启也能按 resolver 认出新管理员；
    ASTOCK_FLEET_OWNER 指向不存在的账号只报 error 不改值。"""
    auth.set_disabled("carol", True)
    try:
        auth.init()
        ck(userctx.fleet_uid() == "carol" == auth.fleet_uid_hint(), "停用站长不应换人")
    finally:
        auth.set_disabled("carol", False)
    userctx.set_fleet_uid("")
    userctx.set_fleet_resolver(auth._earliest_admin)   # 等价于「进程启动时还没有管理员」
    ck(userctx.fleet_uid() == "carol", "站长为空时应惰性重查到最早管理员，不需要重启")
    rec = []
    h = logging.Handler()
    h.emit = rec.append
    auth.logger.addHandler(h)
    os.environ["ASTOCK_FLEET_OWNER"] = "ghost"
    try:
        auth.init()
        ck(any(r.levelno == logging.ERROR and "ghost" in r.getMessage() for r in rec),
           f"不存在的 ASTOCK_FLEET_OWNER 应报 error: {[r.getMessage() for r in rec]}")
        ck(userctx.fleet_uid() == "ghost", "环境变量仍最高优先级，不悄悄换人")
    finally:
        auth.logger.removeHandler(h)
        os.environ.pop("ASTOCK_FLEET_OWNER")


if __name__ == "__main__":
    for fn in (test_fleet_owner_resolution, test_reserved_and_invalid_names, test_unauth_and_login_csrf,
               test_session_stored_hashed_and_ctx_cleared, test_login_next_sanitized, test_logout_kick_disabled,
               test_lock_by_uid, test_lock_by_ip, test_hash_burn_constant_work, test_fleet_owner_stable_and_lazy):
        fn()   # 有顺序依赖（先建用户再登录），不按字母序
    print(f"OK — test_auth 全过（{_n} 断言）")
