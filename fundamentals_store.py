"""财务面板（`data/fundamentals.db`）：把新浪财报的多期数据落库，攒可回测的基本面历史。

**为什么单独一个模块**：`ds.financial_summary` 一次能给 4 期（营收、归母净利及其同比），
但现在是用完就丢，所以长线「营收利润双正」这条规则从来没有历史可回测（BACKLOG 子项目 E）。
落库以后，一年后就能问「去年某天全市场里增速最快的那批股票，后来半年涨得怎么样」。

**为什么不需要 purge**：主键是 `(code, period)`，行数只随股票数与报告期数增长
（约 5000 只 × 4 期/年），不会随运行次数膨胀，与按日累积的表不同。

用法：
    python3 fundamentals_store.py sync [--limit N] [--workers 6]   # 分批抓，可重复跑（断点续传）
    python3 fundamentals_store.py status
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import datasources as ds
import universe_store
import userctx

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_DB_NAME = "fundamentals.db"
# 测试钩子：设成某个绝对路径即全局覆盖。
DB_PATH = os.path.join(_DIR, _DB_NAME)

_LOCK = threading.Lock()
_inited = False
HEARTBEAT_STALE = 600     # 心跳超过 10 分钟视为上一轮同步已死，可重新开始
SYNC_INTERVAL_DAYS = 7    # 全量一轮的目标间隔

_SCHEMA = """
CREATE TABLE IF NOT EXISTS financials(
  code TEXT NOT NULL, period TEXT NOT NULL,
  revenue_yi REAL, revenue_yoy REAL, profit_yi REAL, profit_yoy REAL,
  fetched_at TEXT,
  PRIMARY KEY (code, period)
);
CREATE INDEX IF NOT EXISTS idx_fin_period ON financials(period);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _conn() -> sqlite3.Connection:
    return userctx.open_db(DB_PATH, timeout=20)


def init() -> None:
    global _inited
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)
    _inited = True


def _ensure() -> None:
    if not _inited:
        init()


def _get_meta(k: str) -> str:
    with _conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return (row["v"] if row else "") or ""


def _set_meta(k: str, v: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def save_many(code: str, rows: list[dict[str, Any]]) -> int:
    """落一只股票的多期财报（按 `(code, period)` upsert，重复抓同一期不会产生重复行）。"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    payload = [(code, str(r.get("period") or ""), r.get("revenue_yi"), r.get("revenue_yoy"),
                r.get("profit_yi"), r.get("profit_yoy"), now)
               for r in rows if r.get("period")]
    if not payload:
        return 0
    _ensure()
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO financials(code,period,revenue_yi,revenue_yoy,profit_yi,profit_yoy,fetched_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code,period) DO UPDATE SET "
            "revenue_yi=excluded.revenue_yi, revenue_yoy=excluded.revenue_yoy, "
            "profit_yi=excluded.profit_yi, profit_yoy=excluded.profit_yoy, "
            "fetched_at=excluded.fetched_at", payload)
    return len(payload)


def of(code: str, limit: int = 8) -> list[dict[str, Any]]:
    """某只股票的财报序列（报告期新到旧）。"""
    _ensure()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT period, revenue_yi, revenue_yoy, profit_yi, profit_yoy, fetched_at "
            "FROM financials WHERE code=? ORDER BY period DESC LIMIT ?", (code, limit))]


def status() -> dict[str, Any]:
    """面板进度：行数、覆盖股票数、最新报告期、上次跑完一轮的时间。"""
    _ensure()
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT code) codes, "
                      "MAX(period) latest FROM financials").fetchone()
    return {"rows": r["rows"] or 0, "codes": r["codes"] or 0,
            "latest_period": r["latest"] or "",
            "synced_at": _get_meta("synced_at"), "cursor": _get_meta("cursor")}


def _fetch(code: str) -> list[dict[str, Any]]:
    """单只抓取，失败按空处理（不拖垮整批）。"""
    try:
        return ds.financial_summary(code) or []
    except Exception as e:  # noqa: BLE001 单只失败不影响整批
        logger.warning("财报抓取失败 %s: %s", code, e)
        return []


def sync(limit: int = 0, workers: int = 6, codes: list[str] | None = None,
         progress_every: int = 200) -> dict[str, Any]:
    """分批抓财报并落库。limit=0 表示跑完剩下的一整轮（断点续传，可重复跑）。

    心跳（`sync_hb`）挡并发：上一轮还在跑时本次直接跳过，避免两个进程同时打新浪。
    """
    names = list(codes) if codes is not None else universe_store.codes_of()
    if not names:
        return {"ok": False, "msg": "股票池为空（universe 未就绪？）"}
    _ensure()
    hb = _get_meta("sync_hb")
    if hb:
        try:
            if time.time() - float(hb) < HEARTBEAT_STALE:
                return {"ok": False, "msg": "上一轮同步还在跑（心跳未过期），本次跳过"}
        except ValueError:
            pass
    start = int(_get_meta("cursor") or 0) % len(names)
    # limit>0：从游标起最多抓 N 只（不够就绕回表头继续）；limit=0：把游标之后剩下的抓完。
    # 这样中途被杀（或分了多次小批）都能接着跑，只有真正跑完一轮才写 synced_at。
    full_round = limit <= 0
    if full_round:
        batch = names[start:]
    else:
        batch = (names[start:] + names[:start])[:limit]
    _set_meta("sync_hb", str(time.time()))
    saved = done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for code, rows in zip(batch, ex.map(_fetch, batch)):
                done += 1
                saved += save_many(code, rows)
                if progress_every and done % progress_every == 0:
                    _set_meta("sync_hb", str(time.time()))
                    logger.info("财务面板同步：%d/%d，已写 %d 行", done, len(batch), saved)
    finally:
        _set_meta("sync_hb", "")
    _set_meta("cursor", str((start + done) % len(names)))
    if full_round:
        _set_meta("synced_at", datetime.now().isoformat(timespec="seconds"))
    logger.info("财务面板同步完成：本轮 %d 只 / 写 %d 行（累计见 status）", done, saved)
    return {"ok": True, "done": done, "saved": saved, "full_round": full_round}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="财务面板（data/fundamentals.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="抓财报落库（可重复跑，断点续传）")
    p_sync.add_argument("--limit", type=int, default=0, help="本轮最多抓多少只，0 表示跑完一轮")
    p_sync.add_argument("--workers", type=int, default=6)
    sub.add_parser("status", help="看面板进度")
    args = ap.parse_args()
    if args.cmd == "sync":
        universe_store.init()
        r = sync(limit=args.limit, workers=args.workers)
        print(r)
        return 0 if r.get("ok") else 1
    print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
