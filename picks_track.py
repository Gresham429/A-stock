"""候选名单前向超额追踪（设计 `plan/2026-09-17-screening-factor-refactor-design.md` 第七节第二层）。

第一层验收看因子（分层 IC，`factor_lab.layer_report`），第二层看产品：**选出来的名单**在之后
5 / 10 / 20 个交易日里是否跑赢同层池子的中位数。这一层不带 AI、不做回测假设，每天自动积累。
IC 只说明因子有没有用，这一层才说明选股有没有用。

**基准怎么取**：同层池子的中位数用「层内等距候选集」（`picks_pipeline._layered_candidates`，
每层约 40 只）估计。它是该层的分层样本，中位数可作层中位数的估计；逐日拉全层两千多只日K
来算真值，成本与口径都不划算。基准每天记一次，三个周期共用同一份。

**没有纪律参数**：超额为正为负都如实记账，判读到不到线的事交给看的人。表按日累积，写入路径
自动清理（保留两年）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

import screening
import userctx

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "picks_track.db")
KEEP_DAYS = 730          # 保留两年：够看一年半的前向超额
SETTLE_H = (5, 10, 20)   # 结算地平线（交易日）
MAX_LOOKBACK_DAYS = 120  # 超过这个日历天数的未结算行直接放弃（日K窗口滚走了，补不回来）

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track(
  date TEXT NOT NULL, cycle TEXT NOT NULL, code TEXT NOT NULL,
  layer TEXT DEFAULT '', selected INTEGER DEFAULT 0, entry REAL,
  fwd5 REAL, fwd10 REAL, fwd20 REAL,
  bench5 REAL, bench10 REAL, bench20 REAL,
  excess5 REAL, excess10 REAL, excess20 REAL,
  recorded_at TEXT DEFAULT '', settled_at TEXT DEFAULT '',
  PRIMARY KEY (date, cycle, code)
);
CREATE INDEX IF NOT EXISTS idx_track_date ON track(date);
CREATE INDEX IF NOT EXISTS idx_track_settle ON track(settled_at);
CREATE INDEX IF NOT EXISTS idx_track_cycle ON track(cycle, date);
"""

BENCH_CYCLE = "bench"    # cycle 列的保留值：同层基准（候选深度集）


def _conn() -> sqlite3.Connection:
    return userctx.open_db(DB_PATH, timeout=20)


def init() -> None:
    os.makedirs(_DIR, exist_ok=True)
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def _ensure() -> None:
    init()


def _bars_many(codes: list[str], workers: int = 8) -> dict[str, list[dict]]:
    """并发拉一批日K（进程内 TTL 缓存与选股共用同一份）。

    必须并发：单只新浪日K 约 1.4 秒，基准 120 只加名单串行要三分钟（三周期选股实测过同一坑）。
    用 `userctx.ctx_map` 而不是裸 `ex.map`：池线程要能读到当前用户（PITFALLS #19）。
    """
    import picks_pipeline as pp      # 函数内 import：picks_pipeline 模块级 import 本模块，避循环

    if not codes:
        return {}
    uniq = list(dict.fromkeys(codes))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(uniq, userctx.ctx_map(ex, lambda c: screening._safe_kline(c, pp.KLINE_N), uniq)))


def _last_close(bars: list[dict]) -> float | None:
    """序列里最后一根的有效收盘（盘中即最新价）。取不到返回 None，该只不进追踪。"""
    for b in reversed(bars or []):
        try:
            v = float(b.get("close") or 0)
        except (TypeError, ValueError):
            return None
        if v > 0:
            return v
    return None


def dt_today() -> str:
    """上海时区的今天（服务器可能跑在别的时区，用本地日期会把夜里的运行记到前一天）。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def record(selected: dict[str, list[str]] | None = None, today: str = "") -> dict[str, Any]:
    """记下当日基准（同层候选集）与各周期入选名单。同一天重复调用只补不覆盖。

    `selected`：{周期: [代码]}，由 `picks_pipeline.run_public` 传入当日真正喂给 AI 的名单。
    取不到收盘价的代码跳过（停牌/退市），不写 0 冒充数据。
    """
    import picks_pipeline as pp      # 函数内 import：picks_pipeline 模块级 import 本模块，避循环

    _ensure()
    day = today or dt_today()
    base = pp._pool_base()
    bench_rows = pp._layered_candidates(base) if base else []
    layer_of = {r["code"]: r["layer"] for r in base}
    want = [r["code"] for r in bench_rows] + [c for codes in (selected or {}).values() for c in codes]
    bars = _bars_many(want)
    now = datetime.now().isoformat(timespec="seconds")
    rows: list[tuple] = []
    for r in bench_rows:
        close = _last_close(bars.get(r["code"]) or [])
        if close:
            rows.append((day, BENCH_CYCLE, r["code"], r["layer"], 0, close, now))
    for cycle, codes in (selected or {}).items():
        for code in codes:
            close = _last_close(bars.get(code) or [])
            if close:
                rows.append((day, cycle, code, layer_of.get(code, ""), 1, close, now))
    if not rows:
        return {"ok": False, "msg": "无可用收盘价", "date": day}
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO track(date,cycle,code,layer,selected,entry,recorded_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(date,cycle,code) DO UPDATE SET layer=excluded.layer, "
            "selected=excluded.selected, entry=excluded.entry, recorded_at=excluded.recorded_at",
            rows)
    bench_n = sum(1 for r in rows if r[1] == BENCH_CYCLE)
    cyc_n = len(rows) - bench_n
    logger.info("追踪记录 %s：基准 %d 只、名单 %d 只", day, bench_n, cyc_n)
    purge()
    return {"ok": True, "date": day, "bench": bench_n, "selected": cyc_n}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def settle(today: str = "", cycle: str = "") -> dict[str, Any]:
    """结算到期行：填各周期前向收益、同层基准中位数与超额。不带 AI、纯计算。

    只结算「录满 20 个交易日以上」的行，一次把 5 / 10 / 20 三个地平线填完（同一份日K）。
    结算不动 `selected` 与 `entry`，可以反复重跑（幂等：已结算的行不再处理）。
    """
    _ensure()
    day = today or dt_today()
    cutoff = (date_cls.fromisoformat(day) - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
    with _conn() as c:
        sql = ("SELECT date, cycle, code, entry FROM track WHERE settled_at='' "
               "AND date < ? AND date >= ?")
        args: list[Any] = [day, cutoff]
        if cycle:
            sql += " AND cycle=?"
            args.append(cycle)
        todo = [dict(r) for r in c.execute(sql, args)]
    if not todo:
        return {"ok": True, "settled": 0, "excess_filled": 0}
    by_code: dict[str, list[dict]] = {}
    for r in todo:
        by_code.setdefault(r["code"], []).append(r)
    series = _bars_many(list(by_code))
    filled: dict[tuple[str, str], dict[str, float]] = {}
    for code, items in by_code.items():
        bars = series.get(code) or []
        dates = [str(b.get("date"))[:10] for b in bars]
        closes = [float(b.get("close") or 0) for b in bars]
        for it in items:
            d = it["date"]
            if d not in dates:
                continue          # 窗口里没有那天（停牌或太老）：留给下次或最终放弃
            i = dates.index(d)
            entry = it["entry"] or closes[i]
            if not entry:
                continue
            vals: dict[str, float] = {}
            for h in SETTLE_H:
                if i + h < len(closes) and closes[i + h] > 0:
                    vals[f"fwd{h}"] = round((closes[i + h] / entry - 1) * 100, 4)
            if vals:
                filled[(d, code)] = vals
    now = datetime.now().isoformat(timespec="seconds")
    n_rows = 0
    with _LOCK, _conn() as c:
        for (d, code), vals in filled.items():
            # 同一只代码当天可能既有基准行又有名单行（同日同码），一次更新两行
            n_rows += c.execute("UPDATE track SET fwd5=?, fwd10=?, fwd20=?, settled_at=? "
                                "WHERE date=? AND code=? AND settled_at=''",
                                (vals.get("fwd5"), vals.get("fwd10"), vals.get("fwd20"), now,
                                 d, code)).rowcount
        # 放弃太老又补不回来的行（日K窗口滚走了），否则每天都会重试同一批
        stale = c.execute("UPDATE track SET settled_at=? WHERE settled_at='' AND date < ?",
                          (now, (date_cls.fromisoformat(day) - timedelta(days=MAX_LOOKBACK_DAYS)
                                 ).isoformat())).rowcount
        # 基准中位数（同日同层）写进该日所有行，再算超额
        n_ex = 0
        for (d, layer) in c.execute(
                "SELECT DISTINCT date, layer FROM track WHERE settled_at=? AND cycle=?",
                (now, BENCH_CYCLE)).fetchall():
            cols = [f"fwd{h}" for h in SETTLE_H]
            vals_sql = ", ".join(cols)
            base_rows = [dict(r) for r in c.execute(
                f"SELECT {vals_sql} FROM track WHERE date=? AND layer=? AND cycle=?",
                (d, layer, BENCH_CYCLE))]
            med = {}
            for col in cols:
                med[col] = _median([float(r[col]) for r in base_rows if r[col] is not None])
            set_sql = ", ".join(f"bench{h}=?, excess{h}=?" for h in SETTLE_H)
            for r in c.execute("SELECT rowid, * FROM track WHERE date=? AND layer=? AND cycle!=?",
                               (d, layer, BENCH_CYCLE)).fetchall():
                params: list[Any] = []
                for h in SETTLE_H:
                    params.append(med.get(f"fwd{h}"))
                    fv = r[f"fwd{h}"]
                    params.append(round(fv - med[f"fwd{h}"], 4)
                                  if fv is not None and med.get(f"fwd{h}") is not None else None)
                c.execute(f"UPDATE track SET {set_sql} WHERE rowid=?", (*params, r["rowid"]))
                n_ex += 1
    logger.info("追踪结算 %s：填收益 %d 行、算超额 %d 行、放弃过期 %d 行",
                day, n_rows, n_ex, stale)
    return {"ok": True, "settled": n_rows, "filled_codes": len(filled),
            "excess_filled": n_ex, "dropped": stale}


def summary(cycle: str = "", days: int = 0) -> list[dict[str, Any]]:
    """按周期（可选按层）汇总超额：均值、中位数、胜率、样本数。看「选股有没有用」。"""
    _ensure()
    sql = ("SELECT cycle, layer, excess5, excess10, excess20 FROM track "
           "WHERE selected=1 AND cycle!=?")
    args: list[Any] = [BENCH_CYCLE]
    if cycle:
        sql += " AND cycle=?"
        args.append(cycle)
    if days:
        sql += " AND date >= ?"
        args.append((date_cls.today() - timedelta(days=days)).isoformat())
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        buckets.setdefault((r["cycle"], r["layer"]), []).append(r)
    out = []
    for (cyc, layer), rs in sorted(buckets.items()):
        item: dict[str, Any] = {"cycle": cyc, "layer": layer, "n": len(rs)}
        for h in SETTLE_H:
            xs = [r[f"excess{h}"] for r in rs if r[f"excess{h}"] is not None]
            if xs:
                item[f"excess{h}_mean"] = round(sum(xs) / len(xs), 3)
                item[f"excess{h}_median"] = round(_median(xs) or 0.0, 3)
                item[f"excess{h}_win"] = round(100 * sum(1 for x in xs if x > 0) / len(xs), 1)
                item[f"n{h}"] = len(xs)
        out.append(item)
    return out


def purge(days: int = KEEP_DAYS) -> int:
    """按日累积的表一律要有清理；写入路径自动调用。"""
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM track WHERE date < ?", (cutoff,)).rowcount


def status() -> dict[str, Any]:
    _ensure()
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM track").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) n FROM track WHERE settled_at=''").fetchone()["n"]
        first = c.execute("SELECT MIN(date) d FROM track").fetchone()["d"]
        last = c.execute("SELECT MAX(date) d FROM track").fetchone()["d"]
    return {"rows": n, "pending": pend, "first_date": first, "last_date": last,
            "summary": summary(), "note": "超额 = 名单前向收益减同层候选集（分层样本）中位数"}


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "record":
        print(record())
    elif cmd == "settle":
        print(settle())
    elif cmd == "status":
        print(status())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
