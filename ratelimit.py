"""按人限流 + 全局日预算：保护 DeepSeek 余额和数据源 IP。

# 为什么这是最重要的一层

登录挡住了陌生人，但挡不住「朋友手一抖循环点了 AI 研判」或「他家小孩拿
手机乱点」。deepseek-v4-pro 是推理模型，单次每日推荐要烧掉可观的 token，
/api/agents/run_all 一轮能跑 20 个 agent。没有预算闸门的话，一个失控的
前端轮询就能在一夜之间把余额清零——而且你第二天才会发现。

# 两类桶

ai    —— 会调 LLM 的接口，花的是真钱。按人日限 + 全局日预算双重封顶。
heavy —— 不花钱但会猛打东财/新浪（回填、回测、刷新全市场池）。这些接口被
         刷会让服务器 IP 吃到数据源风控，全站深挖变「无数据」。README 里
         你自己也记过东财限流这个坑。

# 计数放 sqlite 而不是内存

gunicorn 多 worker 时内存计数器每个进程一份，限额会被放大 N 倍。日预算必须
跨进程精确，所以落库。每分钟的粗粒度请求限流留在内存里，那个不精确无所谓。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, request

import userctx

logger = logging.getLogger(__name__)

DB_PATH = userctx.shared_path("usage.db")
_LOCK = threading.Lock()

# ── 额度（都可以在 .env 里调） ────────────────────────────────────────────────
AI_PER_USER_DAY = int(os.environ.get("ASTOCK_AI_PER_USER_DAY", "40"))
AI_GLOBAL_DAY = int(os.environ.get("ASTOCK_AI_GLOBAL_DAY", "150"))
AI_MIN_INTERVAL = float(os.environ.get("ASTOCK_AI_MIN_INTERVAL", "6"))   # 同一人两次 AI 调用最小间隔(秒)
HEAVY_PER_USER_DAY = int(os.environ.get("ASTOCK_HEAVY_PER_USER_DAY", "20"))
REQ_PER_MIN = int(os.environ.get("ASTOCK_REQ_PER_MIN", "180"))           # 每人每分钟总请求数

# ── 哪些路径算哪个桶 ─────────────────────────────────────────────────────────
# 只对写方法(POST/PUT/DELETE)计费——这些接口全是 POST 触发的。
AI_PREFIXES = (
    "/api/recommend/",        # daily / position / entry / screen 全在下面
    "/api/news/deepen",
    "/api/notes/structure",
    "/api/rules/scenario",
    "/api/review/run",
)
AI_PATTERNS = ("/api/agents/run_all",)     # /api/agents/<id>/run 由下面的函数单独判

HEAVY_PREFIXES = (
    "/api/universe/refresh",
    "/api/sectors/backfill",
    "/api/sectors/snapshot",
    "/api/factors/backtest",
    "/api/news/refresh",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage(
  day TEXT, uid TEXT, bucket TEXT, n INTEGER DEFAULT 0,
  PRIMARY KEY(day, uid, bucket)
);
CREATE TABLE IF NOT EXISTS llm_tokens(
  day TEXT, uid TEXT, prompt INTEGER DEFAULT 0, completion INTEGER DEFAULT 0,
  PRIMARY KEY(day, uid)
);
CREATE TABLE IF NOT EXISTS calls(
  ts TEXT, uid TEXT, bucket TEXT, path TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
"""

# 内存态：每分钟请求窗口 + 上次 AI 调用时刻。进程内即可，不求精确。
_req_win: dict[str, deque] = defaultdict(deque)
_last_ai: dict[str, float] = {}
_mem_lock = threading.Lock()


def _conn():
    return userctx.open_db(DB_PATH, timeout=10)


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def bucket_of(path: str, method: str) -> str:
    """这个请求算哪个桶；不计费返回空串。"""
    if method in ("GET", "HEAD", "OPTIONS"):
        return ""
    if path in AI_PATTERNS or any(path.startswith(p) for p in AI_PREFIXES):
        return "ai"
    # /api/agents/<gid>/run 但不是 /api/agents/<gid>/runs（后者是 GET 查历史）
    if path.startswith("/api/agents/") and path.endswith("/run"):
        return "ai"
    if any(path.startswith(p) for p in HEAVY_PREFIXES):
        return "heavy"
    return ""


def _count(day: str, uid: str, bucket: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT n FROM usage WHERE day=? AND uid=? AND bucket=?",
                      (day, uid, bucket)).fetchone()
    return int(r["n"]) if r else 0


def _global_count(day: str, bucket: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT SUM(n) s FROM usage WHERE day=? AND bucket=?",
                      (day, bucket)).fetchone()
    return int(r["s"] or 0)


def _bump(day: str, uid: str, bucket: str, path: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO usage(day,uid,bucket,n) VALUES(?,?,?,1)"
                  " ON CONFLICT(day,uid,bucket) DO UPDATE SET n=n+1", (day, uid, bucket))
        c.execute("INSERT INTO calls(ts,uid,bucket,path) VALUES(?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), uid, bucket, path[:120]))


def record_llm_tokens(uid: str, prompt: int, completion: int) -> None:
    """给 llm.py 回调用：记下真实 token 消耗。

    额度本身按「次数」封顶就够安全了，这张表是为了让你能看见钱花在谁身上、
    花在哪个模型上——出账单时不至于只能猜。
    """
    if not uid:
        return
    try:
        with _LOCK, _conn() as c:
            c.execute("INSERT INTO llm_tokens(day,uid,prompt,completion) VALUES(?,?,?,?)"
                      " ON CONFLICT(day,uid) DO UPDATE SET prompt=prompt+excluded.prompt,"
                      " completion=completion+excluded.completion",
                      (_today(), uid, int(prompt or 0), int(completion or 0)))
    except Exception as e:  # noqa: BLE001 记账失败绝不能影响正常功能
        logger.debug("记录 token 用量失败: %s", e)


def check(uid: str, path: str, method: str) -> tuple[bool, str, int]:
    """返回 (是否放行, 给人看的原因, HTTP 状态码)。"""
    now = time.time()

    # 1) 每分钟总请求数——挡住失控的前端轮询和脚本扫接口
    with _mem_lock:
        win = _req_win[uid]
        while win and now - win[0] > 60:
            win.popleft()
        if len(win) >= REQ_PER_MIN:
            return False, f"请求过于频繁（每分钟上限 {REQ_PER_MIN} 次），稍等一下", 429
        win.append(now)

    bucket = bucket_of(path, method)
    if not bucket:
        return True, "", 200

    day = _today()

    if bucket == "ai":
        # 2) 同一个人两次 AI 调用的最小间隔——挡住连点和重复提交
        with _mem_lock:
            last = _last_ai.get(uid, 0)
            if now - last < AI_MIN_INTERVAL:
                wait = int(AI_MIN_INTERVAL - (now - last)) + 1
                return False, f"AI 分析请求太密集，{wait} 秒后再试", 429

        # 3) 全局日预算——这是余额的最后一道保险，任何人都不能突破
        gn = _global_count(day, "ai")
        if gn >= AI_GLOBAL_DAY:
            return False, (f"今天全站 AI 调用已达上限 {AI_GLOBAL_DAY} 次，"
                           f"明天 0 点重置（防止 API 余额被意外耗尽）"), 429

        # 4) 每人日限——保证一个人刷不光所有人的份额
        n = _count(day, uid, "ai")
        if n >= AI_PER_USER_DAY:
            return False, f"你今天的 AI 分析次数已用完（{AI_PER_USER_DAY} 次/天）", 429

        with _mem_lock:
            _last_ai[uid] = now

    elif bucket == "heavy":
        n = _count(day, uid, "heavy")
        if n >= HEAVY_PER_USER_DAY:
            return False, (f"今天的重任务次数已用完（{HEAVY_PER_USER_DAY} 次/天）。"
                           f"全市场池刷新/回测很吃数据源配额，刷太多会让所有人的深挖变「无数据」"), 429

    _bump(day, uid, bucket, path)
    return True, "", 200


def my_usage(uid: str) -> dict:
    day = _today()
    with _conn() as c:
        t = c.execute("SELECT prompt,completion FROM llm_tokens WHERE day=? AND uid=?",
                      (day, uid)).fetchone()
    return {
        "day": day,
        "ai_used": _count(day, uid, "ai"), "ai_limit": AI_PER_USER_DAY,
        "heavy_used": _count(day, uid, "heavy"), "heavy_limit": HEAVY_PER_USER_DAY,
        "ai_global_used": _global_count(day, "ai"), "ai_global_limit": AI_GLOBAL_DAY,
        "tokens": {"prompt": t["prompt"] if t else 0, "completion": t["completion"] if t else 0},
    }


def all_usage(days: int = 7) -> list[dict]:
    """管理员视角：最近 N 天每人每天的用量。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            "SELECT u.day, u.uid,"
            " SUM(CASE WHEN u.bucket='ai' THEN u.n ELSE 0 END) ai,"
            " SUM(CASE WHEN u.bucket='heavy' THEN u.n ELSE 0 END) heavy,"
            " COALESCE(t.prompt,0) prompt, COALESCE(t.completion,0) completion"
            " FROM usage u LEFT JOIN llm_tokens t ON t.day=u.day AND t.uid=u.uid"
            " WHERE u.day>=? GROUP BY u.day,u.uid ORDER BY u.day DESC,u.uid", (since,)).fetchall()
    return [dict(r) for r in rows]


def purge(keep_days: int = 60) -> int:
    cut = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    with _LOCK, _conn() as c:
        n = c.execute("DELETE FROM calls WHERE ts < ?", (cut,)).rowcount
        c.execute("DELETE FROM usage WHERE day < ?", (cut,))
        c.execute("DELETE FROM llm_tokens WHERE day < ?", (cut,))
    return n


def init_app(app: Flask) -> None:
    """挂到 Flask 上。必须在 auth.init_app 之后注册，才拿得到 g.uid。"""
    init()

    @app.before_request
    def _limit():  # noqa: ANN202
        uid = getattr(g, "uid", "")
        if not uid:          # 未登录的请求已被 auth 闸门拦掉，这里不重复处理
            return None
        ok, why, code = check(uid, request.path, request.method)
        if not ok:
            logger.info("限流拦截 %s %s %s: %s", uid, request.method, request.path, why)
            return jsonify({"error": why, "code": "rate_limited"}), code
        return None

    @app.route("/api/usage")
    def api_usage():  # noqa: ANN202
        """前端可以拿这个在页面上显示「今天还剩几次 AI」。"""
        return jsonify(my_usage(g.uid))

    logger.info("限流已启用：AI 每人 %d 次/天，全站 %d 次/天，最小间隔 %.0fs",
                AI_PER_USER_DAY, AI_GLOBAL_DAY, AI_MIN_INTERVAL)


def consume(uid: str, bucket: str = "ai", path: str = "scheduler", n: int = 1) -> bool:
    """后台任务记账。返回 False 表示全站预算已用尽，调用方应当跳过本次任务。

    调度器自动跑 agent 也是在花你的 API 余额，所以它同样要过全局日预算这道门。
    否则「限流只管人不管定时任务」——半夜 20 个 agent 自动跑照样能把余额掏空。
    """
    day = _today()
    if bucket == "ai" and _global_count(day, "ai") + n > AI_GLOBAL_DAY:
        logger.warning("全站 AI 日预算已用尽（%d/%d），跳过 %s 的后台任务",
                       _global_count(day, "ai"), AI_GLOBAL_DAY, uid)
        return False
    for _ in range(max(1, n)):
        _bump(day, uid, bucket, path)
    return True
