"""因子回测与失效监控（SQLite `data/factors.db`）。

**为什么这条路走得通，而 LLM 回测走不通**：`_pa_score` 的分量是**确定性函数**
（只吃日K），可以在历史上精确重算，**没有数据泄漏**。LLM 则不同——模型训练时见过
2025 年的行情，让它重放 2025-03-15 的决策，它可能「记得」那只股后来崩了，
故 LLM agent 的历史回放不可信。两条腿的验证方法不同，不能混在一起。

**样本量对比**（这是本模块存在的理由）：
    交易结果验证策略优势  784 笔 ≈ 31 年   ← 死局
    因子 IC 横截面观测    300 只 × 600 日   ← 18 万个点，每日一个 IC 观测

**动态调整的正确形式是「监控失效」，不是「自动重拟合」**：
频繁用最近 N 天重拟合最优权重 = 追逐噪音，权重乱跳，策略跟着市场随机波动走
——那是「用小样本优化」换个地方犯。故本模块产出的是：
  ① 每个分量的 IC 均值 / t 值 / 胜率  → 决定**该留还是该删、方向对不对**
  ② 滚动 IC 曲线                      → 因子衰减时**报警**，改不改由人定
量化实证里，过度优化的权重样本外常打不过等权，故不输出「最优权重=0.237」这种东西。

**已知限制**：`sina_metrics` 的 series 被截到 30 天，故 `_pa_score` 四个分量里的
**资金（net20）无法回测**，本模块只验价格类三分量（vol / cum20 / range_pos）。
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

import datasources as ds
import universe_store

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "factors.db")
_LOCK = threading.Lock()

IC_KEEP_DAYS = 1095      # 滚动 IC 保留 3 年（要看衰减趋势，得留长）
KLINE_DAYS = 600         # 新浪日K 实测上限 ~600 根（≈2.4 年）
WARMUP = 25              # 前 N 根用于算 20 日窗口指标，不产出样本
HORIZONS = (5, 10, 20)   # 未来收益天数
# 超额分布的基准。前缀**必须硬编码**：market_prefix 对 000xxx 判 sz，而上证在 sh。
# **单一事实源**：agent_loop 结算时引用这同一个常量——两处基准若不一致，
# 判罪线与被判的那个数就不是一个口径的，且不会报错。
BENCH_SYM = "sh000001"

# 待验因子：只含**确定性、只吃日K**的分量。net20(资金) 因数据源只给 30 天，无法回测。
FACTORS = ("vol", "cum20", "range_pos")

# cohort-aware 方向：打分对象是预筛后的大盘池，其因子方向可与「小盘主导的全池」相反
# （2026-07-18 覆盖分析：h=10 range_pos 全池 −1 vs 大盘 +1）。大盘 cohort = 按流通市值前 N。
# **镜像 `screening._PRESCREEN`**：两者是同一条预筛边界，改一个要同步改另一个。
LARGE_COHORT_N = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ic_daily(
  date TEXT NOT NULL, factor TEXT NOT NULL, horizon INTEGER NOT NULL,
  ic REAL, n INTEGER,
  PRIMARY KEY (date, factor, horizon)
);
CREATE INDEX IF NOT EXISTS idx_ic_date ON ic_daily(date);
-- cohort 逐日 IC：与 ic_daily 并存、互不干扰。只存非「全池」cohort（当前只有 'large'）。
-- 打分/教训门按池子选方向读它；excess_dist/判罪线永远走全池(ic_daily)、不 cohort 化。
CREATE TABLE IF NOT EXISTS ic_cohort(
  date TEXT NOT NULL, factor TEXT NOT NULL, horizon INTEGER NOT NULL,
  cohort TEXT NOT NULL, ic REAL, n INTEGER,
  PRIMARY KEY (date, factor, horizon, cohort)
);
CREATE INDEX IF NOT EXISTS idx_iccohort_date ON ic_cohort(date);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  stocks INTEGER, days INTEGER, samples INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS direction_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, factor TEXT, horizon INTEGER,
  sign INTEGER, basis TEXT, t_stat REAL, prev_sign INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dlog_factor ON direction_log(factor, horizon);
-- 超额收益的历史分位分布：**判罪线的唯一合法来源**。
-- 「超额多负算失败」是个阈值，按 PITFALLS#1 的战绩(拍的方向被数据打脸 5 次)不能拍。
-- 这张表让「这笔在历史上有多差」成为**可查的事实**，而不是我定的数。
-- 由 backtest() 顺带产出（远期收益本就在算，收集成本≈0；原先算完就丢）。
CREATE TABLE IF NOT EXISTS excess_dist(
  horizon INTEGER NOT NULL, pct INTEGER NOT NULL,   -- pct: 1/5/10/25/50/75/90/95/99
  value REAL, n INTEGER, updated_at TEXT,
  PRIMARY KEY (horizon, pct)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# 分布记录这几个分位点：够定判罪线，也够让 AI 知道「有多差」，且不必存 16 万个原始值。
DIST_PCTS = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── 因子计算（与 ds/_pa_score 同口径，但在历史序列上逐日重算） ──────────────
def _ann_vol(closes: list[float]) -> float | None:
    """年化波动率(%)。与 ds._annualized_vol 同口径；含非正值返回 None。"""
    if len(closes) < 21 or any(c <= 0 for c in closes[-21:]):
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(252) * 100


def factors_at(closes: list[float]) -> dict[str, float | None]:
    """给定截至 t 日的收盘序列（含 t），算三个因子。**只用 t 及之前的数据**——不可窥视未来。"""
    if len(closes) < 21:
        return {f: None for f in FACTORS}
    w = closes[-20:]
    lo, hi = min(w), max(w)
    return {
        "vol": _ann_vol(closes),
        "cum20": ((closes[-1] / closes[-21] - 1) * 100 if closes[-21] > 0 else None),
        "range_pos": ((closes[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0),
    }


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """秩相关（IC 的标准算法）。样本 <8 返回 None——横截面太小算不出稳定 IC。"""
    n = len(xs)
    if n < 8:
        return None

    def rank(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:  # 并列取平均秩
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


# ── 回测 ───────────────────────────────────────────────────────────────────
def sample_codes(n: int = 300) -> list[str]:
    """从全市场 eligible 池按流通市值分层抽样——避免只取大盘股导致结论不可推广。"""
    codes = universe_store.codes_of()  # 已按流通市值降序
    if len(codes) <= n:
        return codes
    step = len(codes) / n
    return [codes[int(i * step)] for i in range(n)]


def _series_of(code: str) -> tuple[str, list[dict]]:
    try:
        return code, ds.sina_kline(code, num=KLINE_DAYS, scale=240)
    except Exception as e:  # noqa: BLE001 单只失败不拖垮整批
        logger.warning("日K 取数失败 %s: %s", code, e)
        return code, []


def _percentile(xs: list[float], p: float) -> float | None:
    """第 p 百分位（线性插值，同 numpy 默认口径）。空样本 → None。

    自己实现是因为本项目零第三方依赖（只依赖 flask）。
    """
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return round(s[0], 4)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k // 1), min(int(k // 1) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 4)


def excess_dist() -> dict[int, dict[int, float]]:
    """读超额收益分位分布 {horizon: {pct: value}}。没跑过回测则为空。"""
    out: dict[int, dict[int, float]] = {}
    try:
        with _conn() as c:
            for r in c.execute("SELECT horizon, pct, value FROM excess_dist"):
                if r["value"] is not None:
                    out.setdefault(r["horizon"], {})[r["pct"]] = r["value"]
    except sqlite3.Error as e:
        logger.warning("超额分布读取失败: %s", e)
    return out


def rank_of(horizon: int, x: float, dist: dict[int, dict[int, float]] | None = None) -> int | None:
    """这笔的超额收益 x 落在历史分布的第几百分位。**无分布返回 None，绝不猜。**

    返回 None 时调用方必须放弃判罪——退回某个默认阈值等于偷偷拍了个数（PITFALLS#1）。
    """
    d = (dist if dist is not None else excess_dist()).get(horizon) or {}
    if not d:
        return None
    pts = sorted(d.items(), key=lambda kv: kv[1])     # 按分位值升序
    if x <= pts[0][1]:
        return pts[0][0]
    if x >= pts[-1][1]:
        return pts[-1][0]
    for (p_lo, v_lo), (p_hi, v_hi) in zip(pts, pts[1:]):
        if v_lo <= x <= v_hi:
            if v_hi == v_lo:
                return p_lo
            return int(round(p_lo + (p_hi - p_lo) * (x - v_lo) / (v_hi - v_lo)))
    return None


def _daily_ic_rows(per_day: dict[str, dict[str, list]]) -> list[tuple]:
    """每日横截面 IC → [(date, factor, horizon, ic, n)]。

    纯函数、无网络无库——`backtest`(全池) 与 `backtest_large`(大盘 cohort) **共用同一口径**，
    避免两处 IC 逻辑漂移。存哪张表由调用方决定（cohort 由调用方补）。
    """
    rows: list[tuple] = []
    for date, d in per_day.items():
        for f in FACTORS:
            for h in HORIZONS:
                xs, ys = d.get(f) or [], d.get(f"fwd{h}") or []
                if len(xs) != len(ys):
                    continue
                ic = _spearman(xs, ys)
                if ic is not None:
                    rows.append((date, f, h, round(ic, 6), len(xs)))
    return rows


def backtest(n_stocks: int = 300, workers: int = 8) -> dict[str, Any]:
    """跑一次全量回测：抽样 → 拉日K → 逐日算因子与未来收益 → 每日横截面 IC → 落盘。

    无 LLM、无泄漏、纯确定性。返回汇总。
    """
    init()
    codes = sample_codes(n_stocks)
    logger.info("因子回测：抽样 %d 只，拉 %d 根日K…", len(codes), KLINE_DAYS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = dict(ex.map(_series_of, codes))
    series = {c: k for c, k in series.items() if len(k) > WARMUP + max(HORIZONS) + 21}
    logger.info("有效样本 %d 只（剔除次新/停牌/取数失败）", len(series))
    if not series:
        return {"ok": False, "msg": "无有效日K"}

    # 基准日线：用于把绝对收益换成**超额**收益（判罪线必须是超额口径，否则 beta
    # 混进线里 —— 大盘跌的月份所有建仓点都落到下尾，判罪线就成了「别在熊市买」）。
    bench: dict[str, float] = {}
    try:
        for b in ds.index_kline(BENCH_SYM, num=KLINE_DAYS):
            c_ = float(b.get("close") or 0)
            if c_ > 0:
                bench[str(b["date"])[:10]] = c_
    except Exception as e:  # noqa: BLE001 基准取不到只是不产出分布，IC 照跑
        logger.warning("基准日K取数失败，本轮不产出超额分布: %s", e)

    # 逐只算：每日因子值 + 未来收益（+ 顺带收超额分布，远期收益本就在算，成本≈0）
    per_day: dict[str, dict[str, list]] = {}   # date -> {factor: [值], "fwd{h}": [收益]}
    ex_pool: dict[int, list[float]] = {h: [] for h in HORIZONS}
    samples = 0
    for code, kl in series.items():
        closes = [float(k["close"]) for k in kl]
        dates = [str(k["date"])[:10] for k in kl]
        for t in range(WARMUP, len(closes) - max(HORIZONS)):
            if closes[t] <= 0:
                continue
            f = factors_at(closes[: t + 1])       # 只用 t 及之前 —— 严防未来函数
            if any(v is None for v in f.values()):
                continue
            d = per_day.setdefault(dates[t], {})
            for k_, v in f.items():
                d.setdefault(k_, []).append(v)
            for h in HORIZONS:
                fwd = (closes[t + h] / closes[t] - 1) * 100
                d.setdefault(f"fwd{h}", []).append(fwd)
                # 超额：基准**按日期对齐**到 dates[t] → dates[t+h]，不是自己数 h 根。
                # 个股停牌时 t+h 根会横跨比 h 个交易日更长的日历窗口，各数各的会错配
                # （实测超额高估 10 个百分点且不报错，见 outcome.bench_returns）。
                b0, b1 = bench.get(dates[t]), bench.get(dates[t + h])
                if b0 and b1:
                    ex_pool[h].append(fwd - (b1 / b0 - 1) * 100)
            samples += 1

    # 每日横截面 IC（纯 helper，backtest_large 同口径复用）
    rows = _daily_ic_rows(per_day)
    # 超额分位分布 —— 判罪线的唯一合法来源（见 excess_dist 表注释）
    now_ = datetime.now().isoformat(timespec="seconds")
    dist_rows = []
    for h, pool in ex_pool.items():
        for p in DIST_PCTS:
            v = _percentile(pool, p)
            if v is not None:
                dist_rows.append((h, p, v, len(pool), now_))
    with _LOCK, _conn() as c:
        c.executemany("INSERT INTO ic_daily(date,factor,horizon,ic,n) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(date,factor,horizon) DO UPDATE SET ic=excluded.ic, n=excluded.n",
                      rows)
        if dist_rows:
            c.executemany(
                "INSERT INTO excess_dist(horizon,pct,value,n,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(horizon,pct) DO UPDATE SET value=excluded.value, n=excluded.n, "
                "updated_at=excluded.updated_at", dist_rows)
        c.execute("INSERT INTO runs(created_at,stocks,days,samples,note) VALUES(?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), len(series),
                   len(per_day), samples, f"factors={','.join(FACTORS)}"))
    if dist_rows:
        logger.info("超额分布：%s", {h: len(p) for h, p in ex_pool.items()})
    purge()
    logger.info("因子回测完成：%d 只 × %d 日 = %d 样本，%d 条 IC", len(series), len(per_day),
                samples, len(rows))
    return {"ok": True, "stocks": len(series), "days": len(per_day),
            "samples": samples, "ic_rows": len(rows), "summary": summary()}


def backtest_large(n: int = 200, workers: int = 8) -> dict[str, Any]:
    """大盘 cohort（按流通市值前 `LARGE_COHORT_N`）的逐日 IC → `ic_cohort`（cohort='large'）。

    与 `backtest()` **隔离**：另抽密集样本(~200)、独立一次网络、**不产 excess_dist**
    （判罪线/冻结分位永远走全池）。IC 走同一个 `_daily_ic_rows` helper → 与全池同口径。
    per_day 累积段与 `backtest()` 刻意重复（~12 行）：宁可小重复，也不重构 `backtest()`
    去碰它与 excess_dist 交织的主循环。大盘池仅 ~600 只，密集抽 200 已出稳的横截面 IC。
    """
    init()
    # ⚠️ cohort 必须与 `screening._screen_rows` 的预筛**同口径**：按 **Tencent float_mcap_yi
    # 降序**取前 N。**不能用 codes_of()[:N]**——codes_of() 是按**股票代码号**排序、非市值
    # （实测其 top-600 与真·市值 top-600 仅 82/600 重合），用它会得到「小号码股」而非大盘池，
    # 方向甚至相反（PITFALLS）。故这里自己拉 quote 重排（~1.7s，与 prescreen 同法）。
    codes_all = universe_store.codes_of()
    try:
        q = ds.tencent_quote(codes_all)
    except Exception as e:  # noqa: BLE001 取行情失败则本轮不产出 cohort（打分回退全池）
        logger.warning("大盘 cohort 取行情失败: %s", e)
        return {"ok": False, "msg": "行情失败"}
    ranked = sorted([c for c in codes_all if c in q],
                    key=lambda c: (q.get(c, {}) or {}).get("float_mcap_yi", 0) or 0, reverse=True)
    codes = ranked[:LARGE_COHORT_N]
    if len(codes) < 30:
        return {"ok": False, "msg": f"大盘池不足（{len(codes)}）"}
    step = max(1, len(codes) // n)
    picks = codes[::step][:n]
    logger.info("大盘 cohort 回测：抽样 %d 只（市值前 %d 里密采），拉 %d 根日K…",
                len(picks), LARGE_COHORT_N, KLINE_DAYS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = dict(ex.map(_series_of, picks))
    series = {c: k for c, k in series.items() if len(k) > WARMUP + max(HORIZONS) + 21}
    if not series:
        return {"ok": False, "msg": "无有效日K"}
    per_day: dict[str, dict[str, list]] = {}
    for _code, kl in series.items():
        closes = [float(k["close"]) for k in kl]
        dates = [str(k["date"])[:10] for k in kl]
        for t in range(WARMUP, len(closes) - max(HORIZONS)):
            if closes[t] <= 0:
                continue
            f = factors_at(closes[: t + 1])       # 只用 t 及之前 —— 无未来函数
            if any(v is None for v in f.values()):
                continue
            d = per_day.setdefault(dates[t], {})
            for k_, v in f.items():
                d.setdefault(k_, []).append(v)
            for h in HORIZONS:
                d.setdefault(f"fwd{h}", []).append((closes[t + h] / closes[t] - 1) * 100)
    rows = _daily_ic_rows(per_day)
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO ic_cohort(date,factor,horizon,cohort,ic,n) VALUES(?,?,?,'large',?,?) "
            "ON CONFLICT(date,factor,horizon,cohort) DO UPDATE SET ic=excluded.ic, n=excluded.n",
            [(d, f, h, ic, nn) for (d, f, h, ic, nn) in rows])
    purge()
    logger.info("大盘 cohort 回测完成：%d 只 × %d 日 = %d 条 IC", len(series), len(per_day), len(rows))
    return {"ok": True, "cohort": "large", "stocks": len(series),
            "days": len(per_day), "ic_rows": len(rows)}


def summary(days: int = 0) -> list[dict[str, Any]]:
    """每个因子的 IC 均值 / t 值 / 胜率 —— 据此决定**该留还是该删**，而非精调权重。

    t 值 = IC均值 / (IC标准差/√n)。|t| > 2 视为显著。
    """
    sql = "SELECT factor, horizon, ic FROM ic_daily"
    args: list[Any] = []
    if days:
        sql += " WHERE date >= ?"
        args.append((date_cls.today() - timedelta(days=days)).isoformat())
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    buckets: dict[tuple[str, int], list[float]] = {}
    for r in rows:
        buckets.setdefault((r["factor"], r["horizon"]), []).append(r["ic"])
    out = []
    for (f, h), ics in sorted(buckets.items()):
        n = len(ics)
        if n < 20:
            continue
        mean = sum(ics) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in ics) / (n - 1)) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
        out.append({
            "factor": f, "horizon": h, "n_days": n,
            "ic_mean": round(mean, 4), "ic_std": round(sd, 4),
            "t_stat": round(t, 2), "significant": abs(t) > 2,
            "win_rate": round(100 * sum(1 for x in ics if x > 0) / n, 1),
            "direction": "正向" if mean > 0 else "反向",
        })
    return out


def rolling_ic(factor: str, horizon: int = 10, window: int = 60) -> list[dict[str, Any]]:
    """滚动 IC 曲线 —— 因子衰减时看得见。这是「动态」的正确形式：监控，不是自动重拟合。"""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT date, ic FROM ic_daily WHERE factor=? AND horizon=? ORDER BY date",
            (factor, horizon))]
    out = []
    for i in range(window - 1, len(rows)):
        w = [r["ic"] for r in rows[i - window + 1: i + 1]]
        out.append({"date": rows[i]["date"], "ic_ma": round(sum(w) / len(w), 4)})
    return out


def decay_alert(horizon: int = 10, window: int = 60, ratio: float = 0.4) -> list[dict[str, Any]]:
    """失效报警：近 window 日 IC 均值相对全样本均值衰减超过 (1-ratio)，或符号反转。

    **报警而非自动改权重** —— 改不改由人定，避免噪音驱动参数。
    """
    alerts = []
    full = {(s["factor"], s["horizon"]): s["ic_mean"] for s in summary()}
    with _conn() as c:
        for f in FACTORS:
            rows = c.execute(
                "SELECT ic FROM ic_daily WHERE factor=? AND horizon=? ORDER BY date DESC LIMIT ?",
                (f, horizon, window)).fetchall()
            if len(rows) < window // 2:
                continue
            recent = sum(r["ic"] for r in rows) / len(rows)
            base = full.get((f, horizon))
            if base is None or abs(base) < 1e-6:
                continue
            flipped = (recent * base) < 0
            decayed = abs(recent) < abs(base) * ratio
            if flipped or decayed:
                alerts.append({
                    "factor": f, "horizon": horizon,
                    "ic_full": round(base, 4), "ic_recent": round(recent, 4),
                    "reason": "符号反转" if flipped else f"衰减至 {abs(recent / base) * 100:.0f}%",
                })
    return alerts


RECENT_WINDOW = 60      # 近期窗口（个交易日）
T_THRESHOLD = 2.0       # |t| 超过此值才认方向


def _t_of(ics: list[float]) -> tuple[float, float]:
    n = len(ics)
    if n < 20:
        return 0.0, 0.0
    m = sum(ics) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in ics) / (n - 1)) if n > 1 else 0.0
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def direction(factor: str, horizon: int = 10, cohort: str = "all") -> dict[str, Any]:
    """因子当前方向 —— **动态调整的正确形式**。

    cohort='all'（默认）读全池 `ic_daily`（5 个用户面 AI + 默认打分依赖，行为不变）；
    其它 cohort（如 'large'）读 `ic_cohort` —— 打分对象是预筛后的大盘池、方向可与全池相反。


    规则（防抖 + 防噪音）：
      近 %d 日 |t| > %.1f  → 用近期方向（regime 已切换，且证据显著）
      否则全样本 |t| > %.1f → 用全样本方向（长期规律，近期只是噪音）
      两者都不显著        → **方向未知，该因子不参与打分**（不猜）

    为什么不自动重拟合权重：那会让噪音驱动参数，在 regime 之间来回甩。
    这里只让**方向**随显著证据变，权重保持简单——量化实证里过度优化的权重
    样本外常打不过等权。

    实测（2026-07-16，299 只 × 600 日）：全样本三因子皆反向(cum20 t=-8.16)，
    但近 60 日全部符号反转(vol t=+6.96, cum20 t=+2.99) —— A股 2026 年从反转
    regime 切向动量 regime。若无此机制，静态权重会持续押错方向。
    """
    with _conn() as c:
        if cohort == "all":
            rows = [r["ic"] for r in c.execute(
                "SELECT ic FROM ic_daily WHERE factor=? AND horizon=? ORDER BY date",
                (factor, horizon))]
        else:
            rows = [r["ic"] for r in c.execute(
                "SELECT ic FROM ic_cohort WHERE factor=? AND horizon=? AND cohort=? ORDER BY date",
                (factor, horizon, cohort))]
    if len(rows) < 20:
        return {"factor": factor, "sign": 0, "basis": "数据不足", "t": 0.0, "cohort": cohort}
    m_full, t_full = _t_of(rows)
    m_rec, t_rec = _t_of(rows[-RECENT_WINDOW:])
    if abs(t_rec) > T_THRESHOLD:
        return {"factor": factor, "sign": 1 if m_rec > 0 else -1,
                "basis": f"近{RECENT_WINDOW}日", "t": round(t_rec, 2),
                "ic": round(m_rec, 4), "flipped": (m_rec * m_full) < 0}
    if abs(t_full) > T_THRESHOLD:
        return {"factor": factor, "sign": 1 if m_full > 0 else -1,
                "basis": "全样本", "t": round(t_full, 2), "ic": round(m_full, 4),
                "flipped": False}
    return {"factor": factor, "sign": 0, "basis": "均不显著→不参与打分",
            "t": round(t_rec, 2)}


def directions(horizon: int = 10, cohort: str = "all") -> dict[str, dict[str, Any]]:
    """所有因子的当前方向（供 _pa_score 调用；查不到即全 0 → 打分退回中性）。"""
    try:
        return {f: direction(f, horizon, cohort) for f in FACTORS}
    except sqlite3.Error as e:
        logger.warning("因子方向读取失败（打分退回中性）: %s", e)
        return {f: {"factor": f, "sign": 0, "basis": "库不可用", "t": 0.0, "cohort": cohort}
                for f in FACTORS}


_NO_DATA_BASES = ("数据不足", "库不可用")


def scoring_directions(cohort: str = "large", horizon: int = 10) -> dict[str, dict[str, Any]]:
    """给「打分/教训门」用的方向：优先 cohort 的方向，**该 cohort 尚无 IC 数据时回退全池**。

    区分两种「sign 0」：
      · cohort 表**无数据**（冷启动、未跑 backtest_large）→ 回退全池，避免打分全体中性、shortlist 崩。
      · cohort 有数据但**不显著** → 尊重 sign 0（数据说这只因子在此池中性、不该打分），**不回退**。
    判据：cohort 各因子的 basis 若**全部**落在「数据不足/库不可用」，视作无数据 → 回退。
    """
    if cohort == "all":
        return directions(horizon, cohort="all")
    d = directions(horizon, cohort=cohort)
    has_data = any(v.get("basis") not in _NO_DATA_BASES for v in d.values())
    return d if has_data else directions(horizon, cohort="all")


def purge(days: int = IC_KEEP_DAYS) -> int:
    """滚动清理 IC（按日累积的表一律要有）。"""
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM ic_daily WHERE date < ?", (cutoff,))
        c.execute("DELETE FROM ic_cohort WHERE date < ?", (cutoff,))
        return c.total_changes - before


def status() -> dict[str, Any]:
    try:
        with _conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM ic_daily").fetchone()["n"]
            last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error as e:
        logger.warning("factor status 失败: %s", e)
        return {"ready": False}
    try:
        db_mb = round(os.path.getsize(DB_PATH) / 1048576, 2)
    except OSError:
        db_mb = 0.0
    return {"ready": n > 0, "ic_rows": n, "db_mb": db_mb, "keep_days": IC_KEEP_DAYS,
            "last_run": dict(last) if last else None,
            "note": "net20(资金)因数据源只给30天，未纳入回测"}


# ── 止损参数回测（纪律参数也能验，只是问的问题不同） ───────────────────────
STOP_GRID = (-5.0, -8.0, -10.0, -15.0, -20.0, None)   # None = 不止损


def backtest_stops(n_stocks: int = 200, hold_days: int = 20, workers: int = 8) -> dict[str, Any]:
    """止损线网格回测：历史上每个可能的建仓点，测各档止损线的最终收益。

    **和因子回测问的问题不同**：因子问「什么能预测收益」，止损问「亏损时何时认输」。
    止损是**纪律参数**（控制单笔最大亏损），不是预测参数——所以判据不是「哪个赚最多」，
    而是「哪个在可接受的收益代价下，把尾部亏损压住」。只看均值会得出「别止损」的错论
    （因为 A 股反弹多），但那忽略了**最差情况**和**心理承受**。

    规则：第 t 日以收盘价建仓，之后 hold_days 内若任一日 low ≤ 止损价 → 按止损价出场；
    否则持满以收盘价出场。**按止损价成交而非当日最优**（保守，不自欺）。
    """
    init()
    codes = sample_codes(n_stocks)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = dict(ex.map(_series_of, codes))
    series = {c: k for c, k in series.items() if len(k) > WARMUP + hold_days + 5}
    if not series:
        return {"ok": False, "msg": "无有效日K"}
    res: dict[Any, list[float]] = {s: [] for s in STOP_GRID}
    for kl in series.values():
        for t in range(WARMUP, len(kl) - hold_days):
            entry = float(kl[t]["close"])
            if entry <= 0:
                continue
            window = kl[t + 1: t + 1 + hold_days]
            for stop in STOP_GRID:
                if stop is None:
                    res[stop].append((float(window[-1]["close"]) / entry - 1) * 100)
                    continue
                trig = entry * (1 + stop / 100)
                hit = next((b for b in window if float(b["low"]) <= trig), None)
                res[stop].append(stop if hit else (float(window[-1]["close"]) / entry - 1) * 100)
    out = []
    for stop, rs in res.items():
        n = len(rs)
        if not n:
            continue
        rs_sorted = sorted(rs)
        mean = sum(rs) / n
        out.append({
            "stop": stop, "n": n,
            "mean_ret": round(mean, 3),
            "win_rate": round(100 * sum(1 for x in rs if x > 0) / n, 1),
            "p05": round(rs_sorted[int(n * 0.05)], 2),      # 5% 最差情况 —— 止损真正管的是这个
            "p50": round(rs_sorted[n // 2], 2),
            "worst": round(rs_sorted[0], 2),
            "stopped_pct": (round(100 * sum(1 for x in rs if stop is not None and abs(x - stop) < 1e-9) / n, 1)
                            if stop is not None else 0.0),
        })
    return {"ok": True, "stocks": len(series), "hold_days": hold_days,
            "samples": len(res[STOP_GRID[0]]), "grid": out}


# ── 自动刷新 + 方向留痕 ────────────────────────────────────────────────────
MAX_LAG_DAYS = 5     # ic_daily 落后超过这么多**自然日**就重跑（回测仅 14s，宁可勤快）


def last_ic_date() -> str:
    try:
        with _conn() as c:
            r = c.execute("SELECT MAX(date) d FROM ic_daily").fetchone()
        return (r["d"] or "") if r else ""
    except sqlite3.Error:
        return ""


def is_stale(max_lag_days: int = MAX_LAG_DAYS) -> tuple[bool, int]:
    """IC 是否过期。返回 (过期?, 落后天数)。

    **IC 天然滞后 max(HORIZONS)=20 个交易日**：今日的因子值要等 20 天后才知道未来
    20 日收益，故最新可算的 IC 永远是 `today - 20 交易日`。判过期时必须把这段
    结构性滞后算进去，否则会误判为"永远过期"、每次启动都重跑。
    """
    last = last_ic_date()
    if not last:
        return True, 9999
    lag = (date_cls.today() - date_cls.fromisoformat(last)).days
    structural = int(max(HORIZONS) * 1.5)   # 20 交易日 ≈ 30 自然日
    return lag > structural + max_lag_days, lag


def refresh_if_stale(n_stocks: int = 300) -> dict[str, Any]:
    """惰性刷新：IC 过期才重跑（14s）。供 app 启动时后台调用。

    **不做增量**：全量重跑仅 14s，而增量要处理「哪天该补算」的边界，复杂度不值。
    """
    stale, lag = is_stale()
    if not stale:
        return {"ok": True, "skipped": f"IC 未过期（最新 {last_ic_date()}，落后 {lag} 天）"}
    logger.info("因子 IC 已过期（最新 %s，落后 %d 天），重跑回测…", last_ic_date(), lag)
    before = {f: (direction(f) or {}).get("sign", 0) for f in FACTORS}
    r = backtest(n_stocks)
    if r.get("ok"):
        log_directions(before)
        try:                                  # 顺带刷新大盘 cohort 方向（打分/教训门用）
            r["cohort_large"] = backtest_large()
        except Exception as e:  # noqa: BLE001 大盘 cohort 失败不该拖垮全池刷新（打分回退全池）
            logger.warning("大盘 cohort 回测失败（打分回退全池）: %s", e)
    return r


def log_directions(prev: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """把当前方向留痕（**只在变化时记**）。返回本次发生的变更。

    留痕的意义：方向翻转是 regime 切换的证据，也是「这个因子稳不稳」的证据——
    若某因子一个月翻 5 次，它就是噪音，不该参与打分（`flip_rate()` 可查）。
    """
    prev = prev or {}
    changed = []
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        for f in FACTORS:
            d = direction(f)
            p = prev.get(f)
            if p is not None and p == d["sign"]:
                continue          # 没变，不记 —— 表里只留拐点
            c.execute("INSERT INTO direction_log(ts,factor,horizon,sign,basis,t_stat,prev_sign) "
                      "VALUES(?,?,10,?,?,?,?)",
                      (now, f, d["sign"], d.get("basis", ""), d.get("t", 0.0), p))
            changed.append({"factor": f, "from": p, "to": d["sign"], "basis": d.get("basis")})
    if changed:
        logger.info("因子方向变更: %s", changed)
    return changed


def direction_history(factor: str = "", limit: int = 30) -> list[dict[str, Any]]:
    sql = "SELECT * FROM direction_log"
    args: list[Any] = []
    if factor:
        sql += " WHERE factor=?"
        args.append(factor)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def flip_rate(days: int = 180) -> list[dict[str, Any]]:
    """各因子近 N 日的方向翻转次数 —— **翻得越勤越不可信**。

    实测（2024-02~2026-06，505 个交易日、每日滚动）：vol/cum20 各翻 1 次、
    range_pos 翻 2 次 —— 说明 `|t|>2 + 60日窗口` 本身已足够稳，**不需要额外迟滞**
    （加迟滞是没证据的复杂度）。此函数用于持续监控这个前提是否仍成立。
    """
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT factor, COUNT(*) n FROM direction_log WHERE ts >= ? AND prev_sign IS NOT NULL "
            "GROUP BY factor", (since,))]
    return [{"factor": r["factor"], "flips": r["n"], "days": days,
             "verdict": "稳定" if r["n"] <= 3 else "⚠️ 翻转频繁，疑为噪音"} for r in rows]
