"""登录鉴权：用户表 + 服务端会话 + 全局访问闸门。

# 威胁模型

即使走 Tailscale 私有组网、公网扫不到，登录仍然不能省：
  - 朋友的笔记本丢了 / 被家人拿去用；
  - tailnet 里任何一台设备被入侵，就能直接摸到看板；
  - 多用户本来就需要「你是谁」才能定位到你的数据目录。
组网挡的是「陌生人」，登录挡的是「不是你本人」，两层不互相替代。

# 几个选择及其理由

密码哈希用标准库的 hashlib.scrypt，不引第三方（本项目原本只依赖 flask，
保持这个优点）。scrypt 是内存硬的，比 pbkdf2 抗 GPU 爆破。

会话用服务端 token 表而不是 Flask 自带的签名 cookie。签名 cookie 一旦泄漏
在过期前无法作废；服务端表可以「踢掉某个人的所有登录」，换密码即全量失效。

CSRF 靠 SameSite=Strict + 不安全方法校验 Origin 两道。本项目所有写操作都是
同源 JS 发的 POST，这两道足够，不用再引 CSRF token 改前端。
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   make_response, url_for)

import userctx

logger = logging.getLogger(__name__)

DB_PATH = userctx.shared_path("auth.db")
_LOCK = threading.Lock()

COOKIE_NAME = "astock_sid"
SESSION_DAYS = int(os.environ.get("ASTOCK_SESSION_DAYS", "14"))
# 走 tailscale serve / nginx 时一定是 https，Secure 必须开；本机裸跑调试才关。
COOKIE_SECURE = os.environ.get("ASTOCK_COOKIE_SECURE", "1") not in ("0", "false", "False")

# 登录失败节流：同一用户名连续失败 N 次后锁 M 秒。挡的是慢速撞库，不影响手滑。
MAX_FAILS = int(os.environ.get("ASTOCK_MAX_LOGIN_FAILS", "5"))
LOCK_SECONDS = int(os.environ.get("ASTOCK_LOGIN_LOCK_SEC", "300"))

SCRYPT_N, SCRYPT_R, SCRYPT_P, DK_LEN = 2 ** 14, 8, 1, 32

# 不需要登录就能访问的路径。故意写得很短——白名单越小越安全。
PUBLIC_PATHS = ("/login", "/logout", "/healthz")
PUBLIC_PREFIXES = ("/static/",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  uid          TEXT PRIMARY KEY,
  display_name TEXT DEFAULT '',
  pw_hash      BLOB NOT NULL,
  pw_salt      BLOB NOT NULL,
  is_admin     INTEGER DEFAULT 0,
  disabled     INTEGER DEFAULT 0,
  created_at   TEXT,
  last_login   TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  token      TEXT PRIMARY KEY,
  uid        TEXT NOT NULL,
  created_at TEXT,
  last_seen  TEXT,
  expires_at TEXT,
  ip         TEXT,
  ua         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
CREATE TABLE IF NOT EXISTS login_fails(
  uid        TEXT PRIMARY KEY,
  fails      INTEGER DEFAULT 0,
  locked_until REAL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn():
    return userctx.open_db(DB_PATH, timeout=10)


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── 密码 ─────────────────────────────────────────────────────────────────────

def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)


def check_password_strength(pw: str) -> str:
    """返回空串表示通过，否则返回给人看的原因。

    只挡最糟的情况（太短、纯数字、常见弱口令）。规则定得太狠会逼人把密码
    写在便签上，反而更差。
    """
    if len(pw) < 10:
        return "密码至少 10 位"
    if pw.isdigit():
        return "不能是纯数字"
    weak = {"1234567890", "password12", "qwertyuiop", "0123456789", "1111111111"}
    if pw.lower() in weak:
        return "这个密码太常见了"
    return ""


# ── 用户管理 ──────────────────────────────────────────────────────────────────

def create_user(uid: str, password: str, display_name: str = "",
                is_admin: bool = False) -> None:
    if not userctx.valid_uid(uid):
        raise ValueError("用户名只能是小写字母/数字/下划线/连字符，2-32 位，且不能以 - 开头")
    why = check_password_strength(password)
    if why:
        raise ValueError(why)
    salt = secrets.token_bytes(16)
    with _LOCK, _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
            raise ValueError(f"用户 {uid} 已存在")
        c.execute("INSERT INTO users(uid,display_name,pw_hash,pw_salt,is_admin,created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (uid, display_name or uid, _hash(password, salt), salt,
                   1 if is_admin else 0, _now()))
    userctx.user_dir(uid)  # 立刻建好数据目录，第一次登录不用等
    logger.info("已创建用户 %s(admin=%s)", uid, is_admin)


def set_password(uid: str, password: str) -> None:
    """改密码，并踢掉该用户所有已有会话（改密码就该让旧登录全失效）。"""
    why = check_password_strength(password)
    if why:
        raise ValueError(why)
    salt = secrets.token_bytes(16)
    with _LOCK, _conn() as c:
        n = c.execute("UPDATE users SET pw_hash=?,pw_salt=? WHERE uid=?",
                      (_hash(password, salt), salt, uid)).rowcount
        if not n:
            raise ValueError(f"用户 {uid} 不存在")
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        c.execute("DELETE FROM login_fails WHERE uid=?", (uid,))


def set_disabled(uid: str, disabled: bool) -> None:
    """停用/启用账号。停用会立刻踢掉在线会话——朋友不玩了或手机丢了用这个，
    数据目录保留，比删号安全。"""
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET disabled=? WHERE uid=?", (1 if disabled else 0, uid))
        if disabled:
            c.execute("DELETE FROM sessions WHERE uid=?", (uid,))


def list_users() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT uid,display_name,is_admin,disabled,created_at,last_login,"
            "(SELECT COUNT(*) FROM sessions s WHERE s.uid=u.uid) AS sessions"
            " FROM users u ORDER BY uid").fetchall()
    return [dict(r) for r in rows]


def list_uids(include_disabled: bool = False) -> list[str]:
    """调度器遍历用户时用这个（按账号表，不是按磁盘目录）。"""
    q = "SELECT uid FROM users" + ("" if include_disabled else " WHERE disabled=0")
    with _conn() as c:
        return [r["uid"] for r in c.execute(q + " ORDER BY uid").fetchall()]


def get_user(uid: str) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT uid,display_name,is_admin,disabled FROM users WHERE uid=?",
                      (uid,)).fetchone()
    return dict(r) if r else None


def user_count() -> int:
    with _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"])


# ── 登录 / 会话 ───────────────────────────────────────────────────────────────

def _locked_for(uid: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT locked_until FROM login_fails WHERE uid=?", (uid,)).fetchone()
    if not r:
        return 0
    return max(0, int(r["locked_until"] - time.time()))


def _note_fail(uid: str) -> None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT fails FROM login_fails WHERE uid=?", (uid,)).fetchone()
        fails = (r["fails"] if r else 0) + 1
        until = time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0
        c.execute("INSERT INTO login_fails(uid,fails,locked_until) VALUES(?,?,?)"
                  " ON CONFLICT(uid) DO UPDATE SET fails=excluded.fails,"
                  " locked_until=excluded.locked_until", (uid, fails, until))
    if until:
        logger.warning("用户 %s 连续登录失败 %d 次，锁定 %d 秒", uid, fails, LOCK_SECONDS)


def _clear_fails(uid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM login_fails WHERE uid=?", (uid,))


def login(uid: str, password: str, ip: str = "", ua: str = "") -> tuple[str, str]:
    """验证密码并开一个会话。返回 (token, 错误原因)；成功时错误原因为空串。"""
    uid = (uid or "").strip().lower()
    if not userctx.valid_uid(uid):
        return "", "用户名或密码不对"

    wait = _locked_for(uid)
    if wait:
        return "", f"尝试次数过多，请 {wait} 秒后再试"

    with _conn() as c:
        r = c.execute("SELECT pw_hash,pw_salt,disabled FROM users WHERE uid=?", (uid,)).fetchone()

    if not r:
        # 用户不存在也走一遍同样开销的哈希，避免用响应时间探测哪些用户名存在
        _hash(password, b"0" * 16)
        _note_fail(uid)
        return "", "用户名或密码不对"
    if r["disabled"]:
        return "", "账号已停用"
    if not hmac.compare_digest(_hash(password, bytes(r["pw_salt"])), bytes(r["pw_hash"])):
        _note_fail(uid)
        return "", "用户名或密码不对"

    _clear_fails(uid)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO sessions(token,uid,created_at,last_seen,expires_at,ip,ua)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (token, uid, now.isoformat(timespec="seconds"),
                   now.isoformat(timespec="seconds"),
                   (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"),
                   ip[:64], (ua or "")[:200]))
        c.execute("UPDATE users SET last_login=? WHERE uid=?",
                  (now.isoformat(timespec="seconds"), uid))
    logger.info("登录成功: %s from %s", uid, ip)
    return token, ""


def session_user(token: str) -> str:
    """token -> uid；无效或过期返回空串。顺带滑动续期。"""
    if not token:
        return ""
    with _conn() as c:
        r = c.execute("SELECT uid,expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if not r:
            return ""
        if r["expires_at"] and r["expires_at"] < _now():
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return ""
        u = c.execute("SELECT disabled FROM users WHERE uid=?", (r["uid"],)).fetchone()
        if not u or u["disabled"]:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return ""
        now = datetime.now(timezone.utc)
        c.execute("UPDATE sessions SET last_seen=?,expires_at=? WHERE token=?",
                  (now.isoformat(timespec="seconds"),
                   (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), token))
        return r["uid"]


def logout(token: str) -> None:
    if not token:
        return
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def purge_sessions() -> int:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),)).rowcount


# ── Flask 集成 ────────────────────────────────────────────────────────────────

def _wants_json() -> bool:
    """这个请求是页面导航还是前端 fetch？决定 401 是跳转还是返回 JSON。"""
    return (request.path.startswith("/api/")
            or "application/json" in (request.headers.get("Accept") or ""))


def _client_ip() -> str:
    """真实来源 IP。只信任本机反代加的 X-Forwarded-For 最后一跳，
    不裸信整串——否则来源 IP 可以被伪造，登录节流就废了。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and request.remote_addr in ("127.0.0.1", "::1"):
        return xff.split(",")[-1].strip()
    return request.remote_addr or ""


def _origin_ok() -> bool:
    """写操作的同源校验（CSRF 第二道）。

    没有 Origin/Referer 的请求放行——curl 和部分老浏览器不发，而真正的
    跨站请求由浏览器强制带上 Origin，所以「有且不同源」才是攻击信号。
    """
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    if not host:
        return True
    from urllib.parse import urlparse
    return urlparse(origin).netloc == host


def init_app(app: Flask) -> None:
    """挂上登录路由和全局闸门。

    闸门用 before_request 而不是给 70 个路由挨个加装饰器：默认全关，白名单
    极短。新增路由时不会因为忘了加装饰器而裸奔——这是「安全默认」和「安全
    选项」的区别。
    """
    init()

    @app.before_request
    def _gate():  # noqa: ANN202
        p = request.path
        if p in PUBLIC_PATHS or any(p.startswith(x) for x in PUBLIC_PREFIXES):
            return None

        uid = session_user(request.cookies.get(COOKIE_NAME, ""))
        if not uid:
            if _wants_json():
                return jsonify({"error": "未登录", "code": "auth_required"}), 401
            return redirect(url_for("login_page", next=p))

        if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok():
            logger.warning("拒绝跨站写请求: %s %s origin=%s",
                           request.method, p, request.headers.get("Origin"))
            return jsonify({"error": "跨站请求被拒绝"}), 403

        g.uid = uid
        userctx.set_uid(uid)   # 本次请求内，所有个人 store 自动落到这个人的目录
        return None

    @app.after_request
    def _harden(resp):  # noqa: ANN202
        """基础安全响应头。看板是自包含的单页，CSP 可以收得比较紧。"""
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return resp

    @app.route("/healthz")
    def healthz():  # noqa: ANN202
        """给 systemd / 监控探活用，不含任何数据。"""
        return jsonify({"ok": True})

    @app.route("/login", methods=["GET", "POST"])
    def login_page():  # noqa: ANN202
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"   # 只允许站内相对跳转，挡 open redirect
        if request.method == "GET":
            if session_user(request.cookies.get(COOKIE_NAME, "")):
                return redirect(nxt)
            return render_template("login.html", error="", next=nxt)

        form = request.form if request.form else (request.get_json(silent=True) or {})
        token, err = login(form.get("uid", ""), form.get("password", ""),
                           ip=_client_ip(), ua=request.headers.get("User-Agent", ""))
        if err:
            return render_template("login.html", error=err, next=nxt), 401
        resp = make_response(redirect(nxt))
        resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=COOKIE_SECURE, samesite="Strict", path="/")
        return resp

    @app.route("/logout", methods=["GET", "POST"])
    def logout_page():  # noqa: ANN202
        logout(request.cookies.get(COOKIE_NAME, ""))
        resp = make_response(redirect("/login"))
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.route("/api/me")
    def whoami():  # noqa: ANN202
        u = get_user(g.uid) or {}
        return jsonify({"uid": g.uid, "display_name": u.get("display_name", g.uid),
                        "is_admin": bool(u.get("is_admin"))})

    logger.info("鉴权已启用：%d 个账号，会话 %d 天，Secure cookie=%s",
                user_count(), SESSION_DAYS, COOKIE_SECURE)


def admin_required(fn: Callable) -> Callable:
    """管理接口装饰器（用量总览、用户管理）。"""
    @functools.wraps(fn)
    def wrapper(*a, **kw):  # noqa: ANN202
        u = get_user(getattr(g, "uid", "")) or {}
        if not u.get("is_admin"):
            return jsonify({"error": "需要管理员权限"}), 403
        return fn(*a, **kw)
    return wrapper
