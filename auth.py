"""登录鉴权：用户表 + 服务端会话 + 全局访问闸门。

# 威胁模型

即使走 Tailscale 私有组网、公网扫不到，登录仍然不能省：
  - 朋友的笔记本丢了 / 被家人拿去用；
  - tailnet 里任何一台设备被入侵，就能直接摸到看板；
  - 多用户本来就需要「你是谁」才能定位到你的数据目录。
组网挡的是「陌生人」，登录挡的是「不是你本人」，两层不互相替代。

# 几个选择及其理由

密码哈希用标准库的 hashlib.scrypt，不引第三方（本项目原本只依赖 flask）。
哈希、口令强度、失败锁定的纯逻辑在 auth_password.py。

会话用服务端 token 表而不是 Flask 自带的签名 cookie：签名 cookie 泄漏后在过期前
无法作废，服务端表可以「踢掉某个人的所有登录」。表里存 token 的 sha256 十六进制，
cookie 里才是原 token，auth.db 被拷走（备份泄漏、误提交）时反推不出可用的 cookie。

CSRF 防护：cookie 带 SameSite=Strict，加上对所有非 GET 请求（含 POST /login）校验
Origin/Referer 与 Host 同源。本项目所有写操作都是同源 JS 发的 POST，这两道足够，
不用再引 CSRF token 改前端。登录失败锁定同时按用户名和来源 IP 计数（见 auth_password）。
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   make_response, url_for)

import auth_password as pw
import userctx

logger = logging.getLogger(__name__)

DB_PATH = userctx.shared_path("auth.db")
_LOCK = threading.Lock()

COOKIE_NAME = "astock_sid"
SESSION_DAYS = int(os.environ.get("ASTOCK_SESSION_DAYS", "14"))
# 走 tailscale serve / nginx 时一定是 https，Secure 必须开；本机裸跑调试才关。
# 默认跟 ASTOCK_ENV 走（production 开、其它关）；显式设了 ASTOCK_COOKIE_SECURE 以它为准。
_secure_env = os.environ.get("ASTOCK_COOKIE_SECURE")
COOKIE_SECURE = (os.environ.get("ASTOCK_ENV") == "production" if _secure_env is None
                 else _secure_env not in ("0", "false", "False"))

TOUCH_INTERVAL = timedelta(minutes=10)   # 会话续期最小间隔，免得只读页面也不停写 auth.db

# 不允许注册的用户名。"system" 是 ratelimit 给无用户上下文的 LLM 调用记账的名字
# （ratelimit.SYSTEM_UID），"fleet" 是舰队调用记账的名字（ratelimit.FLEET_UID）；
# 真有人叫这个名，用量表里就分不清是他还是调度器/舰队。
# 这里故意不 import ratelimit：auth 是底层模块，往上层引依赖容易绕成环。
# 所以手抄一份，值必须与 ratelimit.SYSTEM_UID / FLEET_UID 保持一致，改一处要同步另一处。
RESERVED_UIDS = frozenset({"system", "fleet"})

check_password_strength = pw.check_password_strength   # astockctl 通过 auth 引用

# 不需要登录就能访问的路径。故意写得很短——白名单越小越安全。
PUBLIC_PATHS = ("/login", "/logout", "/healthz")
PUBLIC_PREFIXES = ("/static/",)

# sessions.token 列存的是 sha256 十六进制（列名沿用，不需要迁移：旧库里
# 残留的明文 token 行按哈希查不到，等于自动失效，重新登录一次即可）。
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


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def init() -> None:
    """建表，并解析舰队站长（见 userctx.fleet_uid）。

    站长为空时还注册惰性重查（userctx.set_fleet_resolver），先起服务再 adduser --admin
    也不用重启。ASTOCK_FLEET_OWNER 指向不存在的账号时只报 error 不改值：环境变量
    优先级最高，写错了应该改 .env，而不是让程序悄悄换成别人。
    """
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)
    env_owner = (os.environ.get("ASTOCK_FLEET_OWNER") or "").strip()
    if env_owner:
        if userctx.valid_uid(env_owner) and not get_user(env_owner):
            logger.error("ASTOCK_FLEET_OWNER=%r 不是已有账号，舰队路径会一直读空目录；"
                         "请改 .env 或先建这个账号", env_owner)
        return
    owner = _earliest_admin()
    if owner:
        userctx.set_fleet_uid(owner)
    userctx.set_fleet_resolver(_earliest_admin)


def _earliest_admin() -> str:
    """users 表里最早创建的管理员；没有管理员（或表还没建）返回空串。

    故意不过滤 disabled：站长一旦选定就该稳定。停用只是「不能登录」，数据目录
    还在，任何管理员仍能管舰队；若这里跳过停用账号，重启后舰队会悄悄切到
    下一个管理员的空目录，各进程之间还可能不一致。
    """
    q = "SELECT uid FROM users WHERE is_admin=1 ORDER BY created_at, uid LIMIT 1"
    try:
        with _conn() as c:
            r = c.execute(q).fetchone()
    except sqlite3.OperationalError:   # init() 之前被 astockctl status 调到
        return ""
    return r["uid"] if r else ""


def fleet_uid_hint() -> str:
    """当前解析到的站长 uid（环境变量优先，否则最早的管理员），给 astockctl status 用。"""
    return (os.environ.get("ASTOCK_FLEET_OWNER") or "").strip() or _earliest_admin()


# 用户管理


def create_user(uid: str, password: str, display_name: str = "",
                is_admin: bool = False) -> None:
    if not userctx.valid_uid(uid):
        raise ValueError("用户名只能是小写字母/数字/下划线/连字符，2-32 位，且不能以 - 开头")
    if uid in RESERVED_UIDS:
        raise ValueError(f"用户名 {uid} 是系统保留名，不能注册")
    why = pw.check_password_strength(password)
    if why:
        raise ValueError(why)
    salt = pw.new_salt()
    with _LOCK, _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
            raise ValueError(f"用户 {uid} 已存在")
        c.execute("INSERT INTO users(uid,display_name,pw_hash,pw_salt,is_admin,created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (uid, display_name or uid, pw.hash_password(password, salt), salt,
                   1 if is_admin else 0, _now()))
    userctx.user_dir(uid)  # 立刻建好数据目录，第一次登录不用等
    logger.info("已创建用户 %s(admin=%s)", uid, is_admin)


def set_password(uid: str, password: str) -> None:
    """改密码，并踢掉该用户所有已有会话（改密码就该让旧登录全失效）。"""
    why = pw.check_password_strength(password)
    if why:
        raise ValueError(why)
    salt = pw.new_salt()
    with _LOCK, _conn() as c:
        n = c.execute("UPDATE users SET pw_hash=?,pw_salt=? WHERE uid=?",
                      (pw.hash_password(password, salt), salt, uid)).rowcount
        if not n:
            raise ValueError(f"用户 {uid} 不存在")
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        pw.clear_fails(c, uid)


def set_disabled(uid: str, disabled: bool) -> None:
    """停用/启用账号。停用会立刻踢掉在线会话——朋友不玩了或手机丢了用这个，
    数据目录保留，比删号安全。"""
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET disabled=? WHERE uid=?", (1 if disabled else 0, uid))
        if disabled:
            c.execute("DELETE FROM sessions WHERE uid=?", (uid,))


def kick(uid: str) -> int:
    """踢掉某人所有登录会话（不停号）。返回删掉的会话数。"""
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM sessions WHERE uid=?", (uid,)).rowcount


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


# 登录 / 会话


def _fail(keys: tuple[str, ...], burn: str | None = None) -> tuple[str, str]:
    """登录失败的统一出口：按键计数，返回同一句文案。

    burn 非 None 表示这条路径还没算过 scrypt（用户不存在/不合法），补烧一次让耗时
    与「用户存在但密码错」一致。三种失败都返回同一句话，不让调用方分辨账号状态。
    """
    if burn is not None:
        pw.burn_hash(burn)
    with _LOCK, _conn() as c:
        for k in keys:
            pw.note_fail(c, k)
    return "", "用户名或密码不对"


def login(uid: str, password: str, ip: str = "", ua: str = "") -> tuple[str, str]:
    """验证密码并开一个会话。返回 (token, 错误原因)；成功时错误原因为空串。

    失败计数同时按用户名和来源 IP 两个键（见 auth_password 模块说明）；
    用户名不合法或不存在时只计 IP 键，避免攻击者往表里灌垃圾用户名。
    """
    uid = (uid or "").strip().lower()
    ipk = pw.ip_key(ip)
    uid_ok = userctx.valid_uid(uid)

    with _conn() as c:
        wait = max(pw.locked_for(c, ipk), pw.locked_for(c, uid) if uid_ok else 0)
    if wait:
        return "", f"尝试次数过多，请 {wait} 秒后再试"
    if not uid_ok:
        return _fail((ipk,), burn=password)

    with _conn() as c:
        r = c.execute("SELECT pw_hash,pw_salt,disabled FROM users WHERE uid=?", (uid,)).fetchone()
    if not r:
        return _fail((ipk,), burn=password)
    # 停用账号也真算一次哈希再拒绝，耗时与密码错一致；文案不单独暴露「已停用」
    ok = hmac.compare_digest(pw.hash_password(password, bytes(r["pw_salt"])), bytes(r["pw_hash"]))
    if r["disabled"] or not ok:
        return _fail((ipk, uid))

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        pw.clear_fails(c, uid, ipk)
        c.execute("INSERT INTO sessions(token,uid,created_at,last_seen,expires_at,ip,ua)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (_token_hash(token), uid, ts, ts,
                   (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"),
                   (ip or "")[:64], (ua or "")[:200]))
        c.execute("UPDATE users SET last_login=? WHERE uid=?", (ts, uid))
    logger.info("登录成功: %s from %s", uid, ip)
    return token, ""


def _needs_touch(last_seen: str | None, now: datetime) -> bool:
    """上次活跃距今超过 TOUCH_INTERVAL 才续期；字段缺失或格式不对也续（顺手修正）。"""
    try:
        seen = datetime.fromisoformat(last_seen or "")
    except ValueError:
        return True
    return now - seen.replace(tzinfo=seen.tzinfo or timezone.utc) >= TOUCH_INTERVAL


def session_user(token: str) -> str:
    """token -> uid；无效或过期返回空串。顺带滑动续期（最多十分钟写一次）。"""
    if not token:
        return ""
    h = _token_hash(token)
    with _conn() as c:
        r = c.execute("SELECT uid,expires_at,last_seen FROM sessions WHERE token=?", (h,)).fetchone()
        if not r:
            return ""
        if r["expires_at"] and r["expires_at"] < _now():
            c.execute("DELETE FROM sessions WHERE token=?", (h,))
            return ""
        u = c.execute("SELECT disabled FROM users WHERE uid=?", (r["uid"],)).fetchone()
        if not u or u["disabled"]:
            c.execute("DELETE FROM sessions WHERE token=?", (h,))
            return ""
        now = datetime.now(timezone.utc)
        if _needs_touch(r["last_seen"], now):
            c.execute("UPDATE sessions SET last_seen=?,expires_at=? WHERE token=?",
                      (now.isoformat(timespec="seconds"),
                       (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), h))
        return r["uid"]


def logout(token: str) -> None:
    if not token:
        return
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (_token_hash(token),))


def purge_sessions() -> int:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),)).rowcount


# Flask 集成


def _wants_json() -> bool:
    """这个请求是页面导航还是前端 fetch？决定 401 是跳转还是返回 JSON。"""
    return request.path.startswith("/api/") or "application/json" in (request.headers.get("Accept") or "")


def _client_ip() -> str:
    """真实来源 IP。只信任本机反代加的 X-Forwarded-For 最后一跳，
    不裸信整串——否则来源 IP 可以被伪造，登录节流就废了。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and request.remote_addr in ("127.0.0.1", "::1"):
        return xff.split(",")[-1].strip()
    return request.remote_addr or ""


def _origin_ok() -> bool:
    """非 GET 请求的同源校验（CSRF 第二道，第一道是 SameSite=Strict）。

    没有 Origin/Referer 的请求放行——curl 和部分老浏览器不发，而真正的
    跨站请求由浏览器强制带上 Origin，所以「有且不同源」才是攻击信号。

    tailscale serve / 本机反代会把 Host 改写成 127.0.0.1:5000，而浏览器的 Origin
    仍是对外域名，直接比会把所有写请求误判成跨站。与 _client_ip 信任 XFF 的口径
    一致：只在 remote_addr 是回环地址时信任 X-Forwarded-Host，用它替代 Host 比较。
    """
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    xfh = request.headers.get("X-Forwarded-Host", "")
    if xfh and request.remote_addr in ("127.0.0.1", "::1"):
        host = xfh.split(",")[0].strip()
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
        # 同源校验放在白名单之前：POST /login 也要过，否则跨站页面能替人登录
        # （login CSRF）。GET 不校验，浏览器导航本来就不带可信 Origin。
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok():
            logger.warning("拒绝跨站写请求: %s %s origin=%s",
                           request.method, p, request.headers.get("Origin"))
            return jsonify({"error": "跨站请求被拒绝"}), 403

        if p in PUBLIC_PATHS or any(p.startswith(x) for x in PUBLIC_PREFIXES):
            return None

        uid = session_user(request.cookies.get(COOKIE_NAME, ""))
        if not uid:
            if _wants_json():
                return jsonify({"error": "未登录", "code": "auth_required"}), 401
            return redirect(url_for("login_page", next=p))

        g.uid = uid
        userctx.set_uid(uid)   # 本次请求内，所有个人 store 自动落到这个人的目录
        return None

    @app.teardown_request
    def _clear_ctx(_exc):  # noqa: ANN202, ANN001
        """请求结束清掉用户上下文。gthread 会复用线程，不清会把 uid 带进下一个请求。"""
        userctx.set_uid("")
        g.pop("uid", None)

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
        # 只允许站内相对跳转，挡 open redirect。空白/控制字符也拒绝：werkzeug 写响应头时
        # 会剥掉制表符，"/\t/evil.com" 会变成 "//evil.com"；换行则直接让 redirect() 抛 500。
        # 再用 urlsplit 复核一遍没有 scheme/netloc，不依赖前缀判断。
        if (not nxt.startswith("/") or nxt.startswith("//")
                or any(ord(ch) < 0x21 for ch in nxt)
                or urlsplit(nxt).netloc or urlsplit(nxt).scheme):
            nxt = "/"
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
