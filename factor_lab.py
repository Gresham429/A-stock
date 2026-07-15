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

# 待验因子：只含**确定性、只吃日K**的分量。net20(资金) 因数据源只给 30 天，无法回测。
FACTORS = ("vol", "cum20", "range_pos")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ic_daily(
  date TEXT NOT NULL, factor TEXT NOT NULL, horizon INTEGER NOT NULL,
  ic REAL, n INTEGER,
  PRIMARY KEY (date, factor, horizon)
);
CREATE INDEX IF NOT EXISTS idx_ic_date ON ic_daily(date);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  stocks INTEGER, days INTEGER, samples INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


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

    # 逐只算：每日因子值 + 未来收益
    per_day: dict[str, dict[str, list]] = {}   # date -> {factor: [值], "fwd{h}": [收益]}
    samples = 0
    for code, kl in series.items():
        closes = [float(k["close"]) for k in kl]
        dates = [k["date"] for k in kl]
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
                d.setdefault(f"fwd{h}", []).append((closes[t + h] / closes[t] - 1) * 100)
            samples += 1

    # 每日横截面 IC
    rows = []
    for date, d in per_day.items():
        for f in FACTORS:
            for h in HORIZONS:
                xs, ys = d.get(f) or [], d.get(f"fwd{h}") or []
                if len(xs) != len(ys):
                    continue
                ic = _spearman(xs, ys)
                if ic is not None:
                    rows.append((date, f, h, round(ic, 6), len(xs)))
    with _LOCK, _conn() as c:
        c.executemany("INSERT INTO ic_daily(date,factor,horizon,ic,n) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(date,factor,horizon) DO UPDATE SET ic=excluded.ic, n=excluded.n",
                      rows)
        c.execute("INSERT INTO runs(created_at,stocks,days,samples,note) VALUES(?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), len(series),
                   len(per_day), samples, f"factors={','.join(FACTORS)}"))
    purge()
    logger.info("因子回测完成：%d 只 × %d 日 = %d 样本，%d 条 IC", len(series), len(per_day),
                samples, len(rows))
    return {"ok": True, "stocks": len(series), "days": len(per_day),
            "samples": samples, "ic_rows": len(rows), "summary": summary()}


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


def direction(factor: str, horizon: int = 10) -> dict[str, Any]:
    """因子当前方向 —— **动态调整的正确形式**。

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
        rows = [r["ic"] for r in c.execute(
            "SELECT ic FROM ic_daily WHERE factor=? AND horizon=? ORDER BY date",
            (factor, horizon))]
    if len(rows) < 20:
        return {"factor": factor, "sign": 0, "basis": "数据不足", "t": 0.0}
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


def directions(horizon: int = 10) -> dict[str, dict[str, Any]]:
    """所有因子的当前方向（供 _pa_score 调用；查不到即全 0 → 打分退回中性）。"""
    try:
        return {f: direction(f, horizon) for f in FACTORS}
    except sqlite3.Error as e:
        logger.warning("因子方向读取失败（打分退回中性）: %s", e)
        return {f: {"factor": f, "sign": 0, "basis": "库不可用", "t": 0.0} for f in FACTORS}


def purge(days: int = IC_KEEP_DAYS) -> int:
    """滚动清理 IC（按日累积的表一律要有）。"""
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM ic_daily WHERE date < ?", (cutoff,))
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
