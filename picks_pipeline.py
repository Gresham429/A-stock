"""三周期选股流水线：候选池、富化、运行、结算、定时循环。

候选池都压到 POOL_N 只再喂模型（每周期一次提示词）。取数全部来自现有模块，
测试里整体 monkeypatch。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from zoneinfo import ZoneInfo

import cap_layers
import datasources as ds
import factor_lab
import fundamentals_store
import llm_picks
import moneyflow_store
import news_store
import picks_levels
import picks_store
import picks_track
import profile_store
import screening
import store
import universe_store
import userctx
from review import store as review_store

logger = logging.getLogger(__name__)

POOL_N = 30
KLINE_N = 260           # 日K根数：长线 cum120 要 121 根，取 260 留余量；enrich 的候选价位同用这一份
_LAYER_DEPTH = 40       # 每层进打分的候选深度（三层合计 120 只，三周期各拉一次日K）
_THEME_N = 10           # 短线补的复盘题材股数量
_SECTOR_N = 5           # 中线补的前列板块数量（每个取龙头加两只成员）
_SNAP_TTL = 600
_snap_cache: dict[str, Any] = {"ts": 0.0, "rows": []}

CYCLE_FACTORS: dict[str, tuple[tuple[str, int], ...]] = {
    # 周期 -> ((因子, 地平线), ...)。因子必须都在 `factor_lab.FACTORS` 里（有 IC 回测），
    # 否则 `direction()` 恒为 0、白占权重。地平线按该周期持有期取：短线 5 日、中线 10 到 20 日、
    # 长线 20 日。方向取**该层**的 cohort（`layer_large` 等），整层无数据时回退全池。
    #
    # 因子清单按 2026-09-17 的分层方向图定（`python3 -c "import factor_lab"` 可复现，
    # 结论已写进设计文档第七节）：三层各自的显著因子不同，所以三层各取自己那套。
    # 短线加 cum20 是因为 cum5 在大盘层（t 0.39）与中盘层（t 1.20）都不显著，
    # 只留 cum5 会让这两层的分数全体中性、名单退化成层内市值前列；cum20 在 h=5 三层都显著。
    "short": (("cum5", 5), ("cum20", 5), ("range_pos", 5)),
    "mid": (("cum20", 10), ("cum60", 20)),
    "long": (("cum120", 20), ("vol", 20)),
}


def _snapshot() -> list[dict[str, Any]]:
    """全市场快照，带 TTL 缓存——三个周期的底池各调一次，别各拉一遍全市场。

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


def _pool_base() -> list[dict[str, Any]]:
    """三周期共用的底池：eligible（非 ST / 非停牌 / 非北交所）+ 流通市值 ≥30 亿，按市值降序。

    快照本身不区分 ST 与停牌，用 `codes_of()` 的 eligible 名单卡掉（PITFALLS：新浪快照里
    停牌股价格是 0，只用快照会拿停牌股当候选）。层名由 `cap_layers` 给，与筛选/回测同一套边界。
    """
    elig = set(universe_store.codes_of() or [])
    out: list[dict[str, Any]] = []
    for s in _snapshot():
        code = str(s.get("code") or "")
        if code not in elig or not (s.get("price") or 0) > 0:
            continue
        mcap_yi = (s.get("float_mcap") or 0) / 10000.0     # 新浪 nmc 的单位是万元
        layer = cap_layers.layer_of(mcap_yi)
        if not layer:
            continue
        out.append({"code": code, "name": s.get("name") or code, "mcap_yi": mcap_yi,
                    "layer": layer, "pe_ttm": s.get("pe_ttm"), "pb": s.get("pb"),
                    "turnover": s.get("turnover"), "amount": s.get("amount")})
    out.sort(key=lambda r: r["mcap_yi"], reverse=True)
    return out


def _stride_rows(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """层内等距抽样。只取每层市值头部等于在「大市值内部再排一次」，分层就白做了。"""
    if len(rows) <= n:
        return list(rows)
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _layered_candidates(base: list[dict[str, Any]], depth: int = _LAYER_DEPTH) -> list[dict[str, Any]]:
    """每层等距抽 `depth` 只作为候选。`picks_track` 也用它当同层基准的样本。"""
    out: list[dict[str, Any]] = []
    for layer in cap_layers.LAYERS:
        out += _stride_rows([r for r in base if r["layer"] == layer], depth)
    return out


def _closes(code: str) -> list[float]:
    """日K收盘序列（进程内 TTL 缓存，与 enrich 的候选价位共用一次取数）。"""
    return [float(b["close"]) for b in screening._safe_kline(code, KLINE_N) if b.get("close")]


def _closes_many(codes: list[str], workers: int = 8) -> dict[str, list[float]]:
    """并发拉一批日K收盘序列。

    必须并发：单只新浪日K 约 1.4 秒，120 只串行要近三分钟，三个周期就是九分钟（实测过）。
    用 `userctx.ctx_map` 而不是裸 `ex.map`：池线程要能读到当前用户（PITFALLS #19）。
    """
    if not codes:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(codes, userctx.ctx_map(ex, _closes, codes)))


def _score_rows(rows: list[dict[str, Any]], cycle: str) -> list[str]:
    """就地填 `score`：该周期因子的等权分位，方向取该层的 cohort。

    等权而不是拟合权重：本项目自己的实证与量化文献一致，过度优化的权重样本外常打不过等权。
    因子取不到值（窗口不足）就不参与这次打分，不算 0 分（缺失与「最差」是两回事）。
    方向全不可信或全缺值时给中性 50 分，把判断交回给 AI，与 `_pa_score` 同一原则。
    返回**整层都没有可信因子**的层名，让调用方记一条日志（那种层的名单等于按市值排，
    要看得见，不能悄悄发生）。
    """
    pairs = CYCLE_FACTORS[cycle]
    series = _closes_many([r["code"] for r in rows])
    for r in rows:
        closes = series.get(r["code"]) or []
        r["factors"] = factor_lab.factors_at(closes) if len(closes) >= 21 else {}
    neutral: list[str] = []
    for layer in cap_layers.LAYERS:
        dirs = factor_lab.cycle_directions(pairs, cohort=f"layer_{layer}")
        if all(dirs.get(f, {}).get("sign", 0) == 0 for f, _h in pairs):
            neutral.append(layer)
        for r in rows:
            if r["layer"] != layer:
                continue
            live = [(f, h) for f, h in pairs
                    if dirs.get(f, {}).get("sign", 0) != 0 and r["factors"].get(f) is not None]
            if not live:
                r["score"] = 50.0
                continue
            per = 100.0 / len(live)
            s = 0.0
            for f, _h in live:
                pct = screening._factor_pct(f, r["factors"][f])
                s += per * (pct if dirs[f]["sign"] > 0 else 1.0 - pct)
            r["score"] = round(s, 1)
    return neutral


def _layered_select(cycle: str, base: list[dict[str, Any]] | None = None,
                    depth: int = _LAYER_DEPTH) -> list[str]:
    """周期选股主干：分层 -> 等距抽候选 -> 周期因子打分 -> 每层名额（同申万二级 ≤2）。

    设计第三到第六节的落地：分数**真的是分数在决定名单**（旧口径下全市场路径的分数不决定谁
    入选），每层都留名额，行业上限防集中。
    """
    base = _pool_base() if base is None else base
    if not base:
        return []
    cand = _layered_candidates(base, depth)
    neutral = _score_rows(cand, cycle)
    if neutral:
        logger.warning("picks %s：%s 三层中的这些层没有可信因子，名单按层内市值序取",
                       cycle, "/".join(neutral))
    smap = universe_store.sectors_map([r["code"] for r in cand])
    for r in cand:
        r["sub"] = (smap.get(r["code"]) or ("", ""))[1]
    cand.sort(key=lambda r: r["score"], reverse=True)
    picked = cap_layers.select(cand, per_layer=cap_layers.PER_LAYER, sub_cap=cap_layers.SUB_CAP)
    logger.info("picks %s 分层选股：底池 %d（分层 %s）-> 候选 %d -> 取 %d",
                cycle, len(base), cap_layers.layer_counts(base), len(cand), len(picked))
    return picked


def _theme_codes(n: int = _THEME_N) -> list[str]:
    """最近一期复盘的题材/涨停池代码。事件驱动、没有历史可回测，只做候选补充、不进打分。"""
    env = review_store.latest() or {}
    out: list[str] = []
    for r in (env.get("raw_theme") or []):
        c = str(r.get("code") or "")
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            break
    return out


def _sector_codes(n_sectors: int = _SECTOR_N) -> list[str]:
    """板块动量前列的龙头与成员。板块日线有 200 多个交易日历史，但还没进 `factor_lab` 回测，
    所以只做候选补充；入 lab 见 `plan/BACKLOG.md`。板块数据不可用不该拖垮中线池。"""
    try:
        ranking = universe_store.sector_ranking(limit=n_sectors) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("picks: 板块动量取数失败: %s", e)
        return []
    out: list[str] = []
    for row in ranking[:n_sectors]:      # 数据层没守 limit 也不越过上限（历史上吃过这个亏）
        lead = str(row.get("leader_code") or "")
        if lead and lead not in out:
            out.append(lead)
        for c in (universe_store.codes_of(row.get("sector", "")) or [])[:2]:
            if c not in out:
                out.append(c)
    return out


def short_pool(n: int = POOL_N) -> list[str]:
    """短线：复盘题材池（事件驱动）在前，分层因子选股（5 日涨幅、20 日区间位置）在后。

    换手率与成交额相对水平需要历史分位（数据源只给当期），暂不进打分，见设计第五节。
    """
    out = _theme_codes()
    for c in _layered_select("short"):
        if c not in out:
            out.append(c)
    return out[:n]


def mid_pool(n: int = POOL_N) -> list[str]:
    """中线：板块动量前列的龙头与成员在前，分层因子选股（20 日涨幅、60 日涨幅）在后。

    主力资金 net20 仍是展示列、不做门槛：历史只够 30 天，方向验不了（设计第五节）。
    """
    out = _sector_codes()
    for c in _layered_select("mid"):
        if c not in out:
            out.append(c)
    return out[:n]


def _long_base() -> list[dict[str, Any]]:
    """长线的筛选层：估值有效（PE、PB 同为正）且营收、利润同比双正。

    财报走本地 `fundamentals_store`（覆盖率 99%，一次读全表），不逐只打网络：原先对 120 只
    串行取财报要几十秒。面板里没有的代码按「无数据即不合格」处理（与逐只取数失败同口径）。
    「估值低」这一步没有做硬门：估值只有当期快照、没有历史分位，横截面切一刀就是拍的阈值
    （PITFALLS #1）。PE / PB 作为列喂给 AI，等 `valuation_daily` 攒够 250 个交易日再改历史分位门。
    """
    base = _pool_base()
    try:
        fin = fundamentals_store.latest_map([r["code"] for r in base])
    except Exception as e:  # noqa: BLE001 面板读不出来就退化为空池，不拖垮其余周期的选股
        logger.warning("picks: 财报面板读取失败，长线本轮无候选: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for r in base:
        f = fin.get(r["code"]) or {}
        if (r.get("pe_ttm") or 0) <= 0 or (r.get("pb") or 0) <= 0:
            continue
        if (f.get("revenue_yoy") or 0) > 0 and (f.get("profit_yoy") or 0) > 0:
            out.append(r)
    logger.info("picks long 筛选：底池 %d -> 估值有效且财报双正 %d", len(base), len(out))
    return out


def long_pool(n: int = POOL_N) -> list[str]:
    """长线：先过估值与财报筛选（这层没有历史，只做筛选），再按长期动量与波动率分层选股。"""
    return _layered_select("long", base=_long_base())[:n]


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
            # 候选价位与因子打分共用同一份日K（`_safe_kline` 进程内缓存），不重复打网络
            levels[c] = picks_levels.candidate_levels(screening._safe_kline(c, KLINE_N), short=short)
        except Exception as e:  # noqa: BLE001 单只 K 线失败不拖垮整批
            logger.warning("picks: %s K线失败: %s", c, e)
            levels[c] = {"price": q.get("price"), "atr_pct": None, "levels": []}
    return rows, levels


LOCK_DIR = userctx.DATA_DIR
LOCK_STALE_SEC = 1800
FULL_AT = (16, 0)
MORNING_AT = (9, 5)


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
        pools: dict[str, list[str]] = {}
        try:
            settle("public", today)
        except Exception as e:  # noqa: BLE001 结算失败不影响选股
            logger.warning("picks: 公共结算失败: %s", e)
            errors.append(f"settle: {e}")
        for h in horizons:
            try:
                pool = {"short": short_pool, "mid": mid_pool, "long": long_pool}[h]()
                pools[h] = pool
                rows, levels = enrich(pool, short=(h == "short"))
                memory = {r["code"]: picks_store.memory_block("public", r["code"], (levels.get(r["code"]) or {}).get("price"), today) for r in rows}
                calls = llm_picks.horizon_picks(h, rows, levels, memory, market_ctx, capital)
                total += _persist("public", calls, today, run_id)
                if not calls and llm_picks.last_error():
                    errors.append(f"{h}: {llm_picks.last_error()}")
            except Exception as e:  # noqa: BLE001 单周期失败不拖累其余周期，已落库的行保留
                logger.exception("picks: 周期 %s 失败", h)
                errors.append(f"{h}: {e}")
        # 第二层验收（设计第七节）：记当日基准与名单、结算到期行。失败不影响选股结果。
        try:
            picks_track.record(pools, today)
            picks_track.settle(today)
        except Exception as e:  # noqa: BLE001
            logger.warning("picks: 前向超额追踪失败（不影响选股）: %s", e)
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
        error = f"llm: {llm_picks.last_error()}" if not calls and llm_picks.last_error() else None
        return {"run_id": run_id, "calls": _persist("watchlist", calls, today, run_id), "error": error}
    except Exception as e:  # noqa: BLE001 整轮失败账本不动
        logger.exception("picks: 自选股运行失败")
        return {"run_id": run_id, "calls": 0, "error": str(e)}
    finally:
        release_lock("watchlist", lock_path)


HORIZON_GATE_DAYS = 250   # 中长线上线所需的历史交易日数（约一年）


def visible_horizons() -> list[str]:
    """面板上允许展示的周期（用户 2026-09-17：中长线在攒够历史前先 mask，够了自动上线）。

    短线一直在线。中线要资金流历史（它的规则核心是 20 日净流入，原先只有 30 天、验不了），
    长线要估值快照加财务面板覆盖率。判据全是「数据攒了多少」而不是日期，所以到点了自动出现。
    注意：这只是**展示**门，账本照常记录各周期，数据不会因为不展示而断档。
    """
    out = ["short"]
    try:
        if moneyflow_store.days() >= HORIZON_GATE_DAYS:
            out.append("mid")
    except Exception as e:  # noqa: BLE001 数据不可用时按未就绪处理
        logger.debug("资金流历史读取失败: %s", e)
    try:
        val_days = universe_store.valuation_days()
        fin = fundamentals_store.status()
        pool = max(1, len(universe_store.codes_of() or []))
        cover = (fin.get("codes") or 0) / pool
        if val_days >= HORIZON_GATE_DAYS and cover >= 0.8:
            out.append("long")
    except Exception as e:  # noqa: BLE001
        logger.debug("长线就绪判定失败: %s", e)
    return out


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


def _last_path() -> str:
    return os.path.join(LOCK_DIR, ".picks-last.json")


def _load_last() -> dict[str, str]:
    """读「当天这个槽跑过了吗」的跨进程记录，形如 {"full": "2026-09-17"}。

    放文件而不是进程内字典：`deploy/push.sh`、手动重启、崩溃重拉都会重启 scheduler，
    16:00 之后重启会把当天 full 槽当成没跑过，再跑一轮公共三周期（3 次 DeepSeek）
    加每个账号一次自选股。文件缺失或损坏时当作没有记录（跑，与旧行为一致），
    只记日志，不因为读不到状态就跳过当天的活。
    """
    try:
        with open(_last_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("picks: 到点记录读取失败，按未跑过处理: %s", e)
        return {}
    if not isinstance(data, dict):
        logger.warning("picks: 到点记录格式不对，按未跑过处理")
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_last(last: dict[str, str]) -> None:
    """原子写（同目录临时文件加 os.replace），避免别的进程读到写了一半的 JSON。"""
    tmp = f"{_last_path()}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(last, fh, ensure_ascii=False)
        os.replace(tmp, _last_path())
    except OSError as e:
        logger.warning("picks: 到点记录写入失败（重启后可能重跑一轮）: %s", e)


def tick(uids_fn: Callable[[], list[str]],
        market_ctx_fn: Callable[[], dict[str, Any] | None] | None = None) -> str:
    """单次到点判定与执行，供 `loop_forever` 每 60 秒调用一次；返回实际跑的 slot（或 ""）。

    到点即把该槽记进 `data/.picks-last.json`（跨进程，重启不丢）——仓库「错过不补」的
    约定，桶一旦开始就算数，不因为账号内部失败而当天重跑。公共三周期失败也不能让整个
    tick 提前退出：
    `run_public` 包一层 try/except，失败只记日志，随后仍照跑每个账号的自选股。
    随后每个账号的自选股运行各自 try/except：一个账号出错（无论是 `run_watchlist`
    本身还是它调用的取数）只记日志、跳到下一个账号，不拖累其余账号那一桶。
    """
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    last = _load_last()
    slot = due_slot(now, last)
    if not slot:
        return ""
    # 先落盘再跑：桶一旦开始就算数，跑挂了也不当天重来（错过不补）
    last[slot] = _today()
    _save_last(last)
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
