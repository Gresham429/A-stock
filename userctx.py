"""当前用户上下文：把「个人数据」按用户隔离到 data/users/<uid>/。

# 为什么这么做

本项目原本是单人本地跑的：watchlist.json / portfolio.json / notes.db /
agents.db 等都是全局单份。多人共用后，个人数据必须按人分开，但公共数据
（新闻库、全市场池、因子库、复盘）应该继续共享——共享的那几个库恰恰最大
（universe.db 已 40MB+），按人复制既费盘又没意义，而且抓取还会被数据源
按 IP 限流。

# 实现方式：路径解析，不是加 user_id 列

每个 store 模块的 DB_PATH 从「模块常量」改成「调用时解析」，指向当前用户的
目录。好处是不用改任何一行 SQL、不用改任何函数签名、不用动 70 个路由的
调用点。代价是每个用户一份 sqlite 文件——在几个人的规模上这是优点不是缺点
（互不锁表、单独备份、删人就是删目录）。

# 线程传播（关键坑）

contextvars 不会自动跨 threading.Thread：新线程起来是一份空 Context，
拿不到当前用户，个人数据会写到「无人」目录里。所以后台任务一律用本模块的
spawn() / submit() 启动，它们用 contextvars.copy_context() 把当前上下文
复制进新线程。app.py 里原来直接 threading.Thread(...) 的地方都要换掉。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")          # 公共数据（所有人共享）
USERS_DIR = os.path.join(DATA_DIR, "users")        # 个人数据（每人一个子目录）

# 用户名既是登录名也是目录名，必须严格限制字符集——否则 "../../etc" 这类
# 名字会把写操作带出 data/ 目录。只允许小写字母数字和 _- ，且不能以 - 开头。
UID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# 系统任务（调度器里跑公共活儿时）用这个哨兵值，表示「当前没有具体用户」。
# 个人 store 在这个上下文里被调用属于逻辑错误，会抛异常而不是静默写错地方。
NO_USER = ""

_uid_var: contextvars.ContextVar[str] = contextvars.ContextVar("astock_uid", default=NO_USER)


def valid_uid(uid: str) -> bool:
    """用户名是否合法（同时防路径穿越）。"""
    return bool(uid) and bool(UID_RE.match(uid))


def set_uid(uid: str) -> None:
    """设置当前请求/线程的用户。非法用户名直接拒绝，不做「清洗后凑合用」。"""
    if uid and not valid_uid(uid):
        raise ValueError(f"非法用户名: {uid!r}")
    _uid_var.set(uid)


def get_uid() -> str:
    """当前用户；没有则返回空串。"""
    return _uid_var.get()


def require_uid() -> str:
    """当前用户，没有就抛异常。

    个人数据的读写路径都走这里。宁可 500 也不能在「无用户」时静默落到某个
    共享文件上——那等于把一个人的持仓写进别人的库。
    """
    uid = _uid_var.get()
    if not uid:
        raise RuntimeError(
            "当前上下文没有用户，个人数据不可访问。"
            "后台线程请用 userctx.spawn()/submit() 启动，或用 with userctx.as_user(uid)。")
    return uid


@contextlib.contextmanager
def as_user(uid: str) -> Iterator[str]:
    """临时切换当前用户（调度器遍历所有人时用）。退出时恢复原值。"""
    token = _uid_var.set(uid)
    try:
        yield uid
    finally:
        _uid_var.reset(token)


def user_dir(uid: str = "") -> str:
    """某人的数据目录，不存在则创建。"""
    uid = uid or require_uid()
    if not valid_uid(uid):
        raise ValueError(f"非法用户名: {uid!r}")
    d = os.path.join(USERS_DIR, uid)
    os.makedirs(d, exist_ok=True)
    return d


def user_path(filename: str, uid: str = "") -> str:
    """某人目录下的一个文件路径（如 notes.db / watchlist.json）。"""
    if os.sep in filename or (os.altsep and os.altsep in filename) or filename.startswith("."):
        raise ValueError(f"文件名不能含路径分隔符: {filename!r}")
    return os.path.join(user_dir(uid), filename)


def shared_path(filename: str) -> str:
    """公共数据目录下的一个文件路径（news.db / universe.db / factors.db ...）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, filename)


def list_uids() -> list[str]:
    """磁盘上已有数据目录的用户。

    注意这是「有过数据的人」，不等于「账号列表」——账号以 auth.users 表为准。
    调度器遍历用户时应该用 auth.list_uids()，本函数主要给备份/清理脚本用。
    """
    if not os.path.isdir(USERS_DIR):
        return []
    return sorted(d for d in os.listdir(USERS_DIR)
                  if valid_uid(d) and os.path.isdir(os.path.join(USERS_DIR, d)))


# ── 跨线程传播 ────────────────────────────────────────────────────────────────

def spawn(target: Callable[..., Any], *args: Any, **kwargs: Any) -> threading.Thread:
    """起一个带当前用户上下文的守护线程，替代 threading.Thread(...).start()。

    app.py 里所有 `threading.Thread(target=..., daemon=True).start()` 都应该
    换成这个；否则后台任务（深挖新闻、跑 agent、回测）会在无用户上下文里
    访问个人库而炸掉，或者更糟——写到错误的地方。
    """
    ctx = contextvars.copy_context()
    name = getattr(target, "__name__", "task")
    t = threading.Thread(target=lambda: ctx.run(target, *args, **kwargs),
                         name=f"astock-{name}", daemon=True)
    t.start()
    return t


def Thread(target=None, args=(), kwargs=None, daemon=None,
           name=None, **rest) -> threading.Thread:
    """threading.Thread 的替身：签名完全一样，但会把当前用户上下文带进新线程。

    存在的唯一目的是让 app.py 的改动降到最小——把 `threading.Thread(` 原样
    换成 `userctx.Thread(` 即可，参数一个都不用动。直接用原生 Thread 的话，
    新线程里 contextvars 是空的，个人 store 会因为「没有当前用户」而抛异常。
    """
    ctx = contextvars.copy_context()
    kwargs = kwargs or {}

    def _runner() -> None:
        ctx.run(target, *args, **kwargs)

    return threading.Thread(
        target=_runner, daemon=daemon,
        name=name or f"astock-{getattr(target, '__name__', 'task')}", **rest)


def submit(pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any, **kwargs: Any):
    """向线程池提交一个带当前用户上下文的任务。

    只在任务会碰个人数据时才需要；纯取行情的池子（_overview_rows 那种）用原生
    submit/map 就行，那些只读公共数据源。
    """
    ctx = contextvars.copy_context()
    return pool.submit(lambda: ctx.run(fn, *args, **kwargs))


# ── 给 store 模块用的连接工厂 ──────────────────────────────────────────────────

def open_db(path: str, timeout: int = 10):
    """打开 sqlite 并统一开 WAL。

    上多用户 + gunicorn 多 worker 后，默认的 rollback journal 模式下「一个人
    写、其他人全部读阻塞」会很明显。WAL 让读写并发，是多进程访问同一个 sqlite
    的必备设置。busy_timeout 兜住偶发的写写冲突。
    """
    import sqlite3
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    except Exception as e:  # noqa: BLE001 老文件/只读盘上 PRAGMA 可能失败，不致命
        logger.debug("设置 PRAGMA 失败（不影响使用）: %s", e)
    return conn
