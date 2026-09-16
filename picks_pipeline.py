"""三周期选股流水线：候选池、富化、运行、结算、定时循环。

候选池都压到 POOL_N 只再喂模型（每周期一次提示词）。取数全部来自现有模块，
测试里整体 monkeypatch。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from zoneinfo import ZoneInfo

import datasources as ds
import llm_picks
import news_store
import picks_levels
import picks_store
import profile_store
import screening
import store
import universe_store
import userctx
from review import store as review_store

logger = logging.getLogger(__name__)

POOL_N = 30
KLINE_N = 90
LONG_PREFILTER = 120
_SNAP_TTL = 600
_snap_cache: dict[str, Any] = {"ts": 0.0, "rows": []}


def _snapshot() -> list[dict[str, Any]]:
    """全市场快照，带 TTL 缓存——short_pool/long_pool 各调一次，别各拉一遍全市场。

    只缓存取数成功且非空的结果；失败或空结果直接返回空表、不写缓存，
    好让下一次调用立刻重试，不会把一次瞬时失败锁死成 10 分钟无候选池。
    """
    now = time.time()
    if now - _snap_cache["ts"] < _SNAP_TTL:
        return _snap_cache["rows"]
    try:
        rows = ds.sina_all_stocks() or []
    except Exception as e:  # noqa: BLE001 取数失败按空处理，池子退化不崩，不写缓存以便下次重试
        logger.warning("picks: 全市场快照失败: %s", e)
        return []
    if not rows:
        return []
    _snap_cache["ts"] = now
    _snap_cache["rows"] = rows
    return rows


def short_pool(n: int = POOL_N) -> list[str]:
    """短线：当天复盘的题材/涨停池优先，再补全市场换手率最高的。"""
    out: list[str] = []
    env = review_store.latest() or {}
    for r in (env.get("raw_theme") or []):
        c = str(r.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            return out[:n]
    snap = [s for s in _snapshot() if s.get("turnover") and s.get("amount")]
    snap.sort(key=lambda s: (float(s["turnover"]), float(s["amount"])), reverse=True)
    for s in snap:
        c = str(s.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            break
    return out[:n]


def mid_pool(n: int = POOL_N) -> list[str]:
    """中线：排名前列板块的龙头与成员，按主力 20 日净流入为正筛。"""
    out: list[str] = []
    for row in universe_store.sector_ranking(limit=10):
        lead = str(row.get("leader_code") or "")
        if lead and lead not in out:
            out.append(lead)
        for c in universe_store.codes_of(row.get("sector", ""))[:8]:
            if c not in out:
                out.append(c)
    if not out:
        return []
    m = screening._metrics_of(out)
    good = [c for c in out if (m.get(c) or {}).get("net20") is not None and m[c]["net20"] > 0]
    rest = [c for c in out if c not in good]
    return (good + rest)[:n]


def _fin_ok(code: str) -> bool:
    """营收、利润同比是否双正；单只财报失败按不合格处理，不拖垮整批并发取数。"""
    try:
        fin = ds.financial_summary(code) or []
    except Exception as e:  # noqa: BLE001 单只财报失败按不合格处理，不影响其余并发请求
        logger.warning("picks: %s 财报失败: %s", code, e)
        return False
    if not fin:
        return False
    f0 = fin[0]
    return (f0.get("revenue_yoy") or 0) > 0 and (f0.get("profit_yoy") or 0) > 0


def long_pool(n: int = POOL_N) -> list[str]:
    """长线：估值分位低（PE、PB 在快照里排前 LONG_PREFILTER）且营收、利润同比双正。

    财报逐只请求是网络调用，串行 120 次太慢，用小并发池并行取。
    """
    snap = [s for s in _snapshot() if s.get("pe_ttm") and s["pe_ttm"] > 0 and s.get("pb") and s["pb"] > 0]
    snap.sort(key=lambda s: (float(s["pe_ttm"]) * float(s["pb"])))
    cands = snap[:LONG_PREFILTER]
    if not cands:
        return []
    with ThreadPoolExecutor(max_workers=6) as ex:
        pairs = list(ex.map(lambda s: (s["code"], _fin_ok(s["code"])), cands))
    out = [str(code) for code, ok in pairs if ok]
    return out[:n]


def enrich(codes: list[str], short: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """行情 + 指标 + 板块 + 候选价位。缺行情的代码剔除，不进 rows/levels。"""
    if not codes:
        return [], {}
    quotes = ds.tencent_quote(codes)
    valid: list[str] = []
    dropped = 0
    for c in codes:
        q = quotes.get(c) or {}
        if q.get("price") is None:
            dropped += 1
            continue
        valid.append(c)
    if dropped:
        logger.warning("picks: %d 只无行情已剔除", dropped)
    if not valid:
        return [], {}
    metrics = screening._metrics_of(valid)
    rows: list[dict[str, Any]] = []
    levels: dict[str, dict] = {}
    for c in valid:
        q = quotes.get(c) or {}
        m = metrics.get(c) or {}
        try:
            primary, sub = universe_store.sector_of(c)
        except Exception:  # noqa: BLE001 归属缺失不影响选股
            primary, sub = "", ""
        rows.append({"code": c, "name": q.get("name", c), "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"), "range_pos": m.get("range_pos"),
                     "net20": m.get("net20"), "turnover": q.get("turnover"), "lot_cost": q.get("lot_cost")})
        try:
            levels[c] = picks_levels.candidate_levels(ds.sina_kline(c, KLINE_N), short=short)
        except Exception as e:  # noqa: BLE001 单只 K 线失败不拖垮整批
            logger.warning("picks: %s K线失败: %s", c, e)
            levels[c] = {"price": q.get("price"), "atr_pct": None, "levels": []}
    return rows, levels


LOCK_DIR = userctx.DATA_DIR
LOCK_STALE_SEC = 1800
FULL_AT = (16, 0)
MORNING_AT = (9, 5)
_last: dict[str, str] = {}


def _today() -> str:
    """上海时区的今天：服务器可能跑在别的时区，交易日判定、账本 created_at、
    额度计费全部以 A 股所在的时区为准，不用进程本地时区。"""
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _lock_path(scope: str) -> str:
    return os.path.join(LOCK_DIR, f".picks-running-{scope}")


def _user_lock_path() -> str:
    """个人自选股跑批锁：data/users/<uid>/.picks-running，当前用户自己一份。"""
    return os.path.join(userctx.user_dir(), ".picks-running")


def acquire_lock(scope: str, path: str | None = None) -> bool:
    p = path or _lock_path(scope)
    try:
        if os.path.exists(p) and time.time() - os.path.getmtime(p) > LOCK_STALE_SEC:
            logger.warning("picks: 锁 %s 超过 30 分钟，视为陈旧覆盖", p)
            os.remove(p)
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logger.warning("picks: 锁文件不可用，放行: %s", e)
        return True


def release_lock(scope: str, path: str | None = None) -> None:
    try:
        os.remove(path or _lock_path(scope))
    except OSError:
        pass


def is_running(scope: str) -> bool:
    """按锁文件判断是否在跑：scope='public' 查公共锁；'watchlist' 查当前用户自己的锁。

    锁文件不存在或已陈旧（超过 LOCK_STALE_SEC）都算未在跑，跟 acquire_lock 的
    陈旧判定口径一致。当前上下文没有用户时按未在跑处理，不让路由因此 500。
    """
    try:
        path = _user_lock_path() if scope == "watchlist" else _lock_path(scope)
    except RuntimeError:
        return False
    if not os.path.exists(path):
        return False
    return time.time() - os.path.getmtime(path) <= LOCK_STALE_SEC


def settle(scope: str, today: str) -> dict[str, Any]:
    """到期 + 结果贴回（拉日 K）。

    结算对象是「近 WINDOW_DAYS 天内出现过的全部代码」（`recent_codes`），不是
    只看当前 open 行——本函数先调 expire() 才结算，短线行大概率此时已经被标成
    expired、如果只看 open 行就永远轮不到它，20 日收益会一直空着。
    """
    expired = picks_store.expire(scope, today)
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=picks_store.WINDOW_DAYS)).isoformat()
    codes = picks_store.recent_codes(scope, since)
    n = 0
    for c in codes:
        try:
            n += picks_store.staple(scope, c, ds.sina_kline(c, 40), today)
        except Exception as e:  # noqa: BLE001 单只结算失败不影响其余
            logger.warning("picks: %s 结算失败: %s", c, e)
    return {"expired": expired, "stapled": n}


def _persist(scope: str, calls: list[dict[str, Any]], today: str, run_id: str) -> int:
    n = 0
    for c in calls:
        try:
            picks_store.apply(scope, c, today, run_id)
            n += 1
        except Exception as e:  # noqa: BLE001 单条落库失败不影响其余
            logger.warning("picks: %s 落库失败: %s", c.get("code"), e)
    return n


def run_public(market_ctx: dict[str, Any] | None = None,
               horizons: tuple[str, ...] = ("short", "mid", "long"), capital: float = 10000) -> dict[str, Any]:
    """跑公共三周期。每个周期独立 try/except：一个周期失败不影响其余周期，也不回滚
    已经成功落库的行——`calls`/`error` 都如实反映实际发生的事。"""
    today = _today()
    run_id = f"pub-{today}-{int(time.time())}"
    picks_store.init("public")
    if not acquire_lock("public"):
        return {"run_id": run_id, "calls": 0, "horizons": [], "error": "已在生成"}
    try:
        total = 0
        errors: list[str] = []
        try:
            settle("public", today)
        except Exception as e:  # noqa: BLE001 结算失败不影响选股
            logger.warning("picks: 公共结算失败: %s", e)
            errors.append(f"settle: {e}")
        for h in horizons:
            try:
                pool = {"short": short_pool, "mid": mid_pool, "long": long_pool}[h]()
                rows, levels = enrich(pool, short=(h == "short"))
                memory = {r["code"]: picks_store.memory_block("public", r["code"], (levels.get(r["code"]) or {}).get("price"), today) for r in rows}
                calls = llm_picks.horizon_picks(h, rows, levels, memory, market_ctx, capital)
                total += _persist("public", calls, today, run_id)
                if not calls and llm_picks.LAST_ERROR:
                    errors.append(f"{h}: {llm_picks.LAST_ERROR}")
            except Exception as e:  # noqa: BLE001 单周期失败不拖累其余周期，已落库的行保留
                logger.exception("picks: 周期 %s 失败", h)
                errors.append(f"{h}: {e}")
        return {"run_id": run_id, "calls": total, "horizons": list(horizons), "error": "; ".join(errors) or None}
    finally:
        release_lock("public")


def run_watchlist(market_ctx: dict[str, Any] | None = None, capital: float | None = None) -> dict[str, Any]:
    """跑个人自选股。跨进程按当前用户的锁文件互斥（`_user_lock_path`），一个用户
    在同一时刻只允许一轮在跑；锁与账本落库无关，任何失败路径都在 finally 里释放。"""
    today = _today()
    run_id = f"wl-{userctx.get_uid() or 'nouser'}-{today}-{int(time.time())}"
    picks_store.init("watchlist")
    lock_path = _user_lock_path()
    if not acquire_lock("watchlist", lock_path):
        return {"run_id": run_id, "calls": 0, "error": "已在生成"}
    try:
        codes = [str(c) for c in (store.load_watchlist() or []) if c]   # load_watchlist 返回代码列表
        if not codes:
            return {"run_id": run_id, "calls": 0, "error": "自选股为空"}
        if capital is None:
            capital = float((profile_store.get_active() or {}).get("cash") or 10000)
        settle("watchlist", today)
        rows, levels = enrich(codes, short=True)
        memory = {c: picks_store.memory_block("watchlist", c, (levels.get(c) or {}).get("price"), today) for c in codes}
        calls = llm_picks.watchlist_points(rows, levels, memory, market_ctx, capital)
        error = f"llm: {llm_picks.LAST_ERROR}" if not calls and llm_picks.LAST_ERROR else None
        return {"run_id": run_id, "calls": _persist("watchlist", calls, today, run_id), "error": error}
    except Exception as e:  # noqa: BLE001 整轮失败账本不动
        logger.exception("picks: 自选股运行失败")
        return {"run_id": run_id, "calls": 0, "error": str(e)}
    finally:
        release_lock("watchlist", lock_path)


def due_slot(now: dt.datetime, last: dict[str, str]) -> str:
    """到点判定：16:00 后当天未跑全量返回 full；09:05 到 16:00 之间当天未跑早盘返回 morning。"""
    if not news_store.is_trading_day(now.date()):
        return ""
    today = now.date().isoformat()
    hm = (now.hour, now.minute)
    if hm >= FULL_AT and last.get("full") != today:
        return "full"
    if MORNING_AT <= hm < FULL_AT and last.get("morning") != today:
        return "morning"
    return ""


def tick(uids_fn: Callable[[], list[str]],
        market_ctx_fn: Callable[[], dict[str, Any] | None] | None = None) -> str:
    """单次到点判定与执行，供 `loop_forever` 每 60 秒调用一次；返回实际跑的 slot（或 ""）。

    到点即把 `_last[slot]` 标记为今天已处理——仓库「错过不补」的约定，桶一旦开始就
    算数，不因为账号内部失败而当天重跑。公共三周期失败也不能让整个 tick 提前退出：
    `run_public` 包一层 try/except，失败只记日志，随后仍照跑每个账号的自选股。
    随后每个账号的自选股运行各自 try/except：一个账号出错（无论是 `run_watchlist`
    本身还是它调用的取数）只记日志、跳到下一个账号，不拖累其余账号那一桶。
    """
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    slot = due_slot(now, _last)
    if not slot:
        return ""
    _last[slot] = _today()
    mctx = None
    if market_ctx_fn:
        try:
            with userctx.as_fleet():
                mctx = market_ctx_fn()
        except Exception as e:  # noqa: BLE001 站长上下文取大盘失败不影响后续跑批
            logger.warning("picks: 大盘研判获取失败: %s", e)
            mctx = None
    horizons = ("short", "mid", "long") if slot == "full" else ("short",)
    logger.info("picks: 到点 %s，公共三周期 %s", slot, horizons)
    try:
        run_public(mctx, horizons)
    except Exception as e:  # noqa: BLE001 公共失败不能跳过逐用户自选股循环
        logger.warning("picks: run_public 异常: %s", e)
    for uid in uids_fn():
        try:
            with userctx.as_user(uid):
                r = run_watchlist(mctx)
                logger.info("picks: [%s] 自选股 %s", uid, r)
        except Exception as e:  # noqa: BLE001 一个账号出错不能拖累其他账号
            logger.warning("picks: [%s] 自选股运行异常: %s", uid, e)
    return slot


def loop_forever(uids_fn: Callable[[], list[str]],
                 market_ctx_fn: Callable[[], dict[str, Any] | None] | None = None) -> None:
    """定时循环：每 60 秒探一次到点，调用 tick()。"""
    while True:
        try:
            tick(uids_fn, market_ctx_fn)
        except Exception as e:  # noqa: BLE001 循环绝不停摆
            logger.warning("picks: 定时循环异常: %s", e)
        time.sleep(60)
