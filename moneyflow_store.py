"""资金流历史（`data/moneyflow.db`）：把东财 `push2his` 的每日主力净流入落库，攒出可回测的资金因子里程。

**为什么**：新浪 MoneyFlow 只给最近 30 天，所以 `_pa_score` 的 net20 分量一直无法回测方向、
只能展示（PITFALLS #1 的「能验必验」在这里一直欠着）。东财 `push2his` 的 `fflow/daykline`
能给约 120 天的每日主力净流入，把它每天落一份，历史就会往上长。

**节流**：所有请求走 `ds.em_get`（跨进程最小间隔 + 抖动）。这个端点对突发敏感，实测服务器 IP
连续探测后会被重置连接，住宅 IP 稳定，所以默认单线程（em_get 本身已全局串行，多线程没有收益），
并在连续失败时退避后跳过，不把整轮卡死。

用法：
    python3 moneyflow_store.py sync [--limit N]   # 分批抓，可重复跑（断点续传）
    python3 moneyflow_store.py status
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from datetime import date as date_cls
from typing import Any

import datasources as ds
import universe_store
import userctx

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_DB_NAME = "moneyflow.db"
DB_PATH = os.path.join(_DIR, _DB_NAME)   # 测试钩子：设成绝对路径即全局覆盖

_LOCK = threading.Lock()
_inited = False
KEEP_DAYS = 730            # 保留 2 年（与估值快照同口径）
HEARTBEAT_STALE = 600      # 心跳超过 10 分钟视为上一轮已死，可重新开始
FAIL_BACKOFF = 20          # 连续失败到这个次数，退避一会儿再继续，避免把端点打到封 IP

_FFLOW = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
          "lmt=0&klt=101&secid={secid}&fields1=f1,f2,f3,f7&"
          "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")

# 全市场当日快照：clist 一次给一页（实测 pz 被钉死在 100），全 A 约 56 页。
# 这条路每天只要几十次请求，是「持续积累」的正路；上面的 daykline 是低频历史种子。
_CLIST = ("{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
          "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f62,f184")
# clist 的备用主机。`push2` 在部分网络（2026-09-17 实测：阿里云服务器 IP）上整条连不上，
# TCP 层就被拒（curl 000 / Empty reply），而它的延迟镜像 `push2delay` 返回 200。
# 收盘后取的是定盘数据，延迟几分钟对「日频快照」没有影响，所以主站不通就换镜像。
_CLIST_HOSTS = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
CLIST_MAX_PAGES = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moneyflow_daily(
  date TEXT NOT NULL, code TEXT NOT NULL,
  main_net REAL, main_pct REAL, big_net REAL, super_net REAL,
  PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_mf_code ON moneyflow_daily(code, date);
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


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _secid(code: str) -> str:
    """东财 secid：1 开头是沪市，其余（深市/创业板/科创板/北交所）走 0。"""
    return f"{'1' if ds.market_prefix(code) == 'sh' else '0'}.{code}"


def fetch_snapshot() -> list[dict[str, Any]]:
    """抓全市场**当天**的资金流快照（clist 分页）。中途失败返回已拿到的部分。

    主机按 `_CLIST_HOSTS` 顺序试，第一台能出数据就用它翻完所有页。这一点在服务器上是必需的：
    `push2` 从阿里云 IP 连不通，只有镜像 `push2delay` 能用。
    """
    out: list[dict[str, Any]] = []
    for host in _CLIST_HOSTS:
        out = []
        total: int | None = None
        for pn in range(1, CLIST_MAX_PAGES + 1):
            try:
                data = (json.loads(ds.em_get(_CLIST.format(host=host, pn=pn))).get("data") or {})
            except Exception as e:  # noqa: BLE001 单页失败保留已拿到的部分
                logger.warning("资金流快照 %s 第 %d 页失败: %s", host, pn, e)
                break
            if total is None:
                total = data.get("total") or 0
            diff = data.get("diff") or []
            if not diff:
                break
            for r in diff:
                code = str(r.get("f12") or "")
                if len(code) != 6:
                    continue
                out.append({"code": code, "main_net": _num(r.get("f62")),
                            "main_pct": _num(r.get("f184"))})
            if total and len(out) >= total:
                break
        if out:
            if host != _CLIST_HOSTS[0]:
                logger.info("资金流快照走备用主机 %s（主站不通）", host)
            return out
    logger.warning("资金流快照所有主机都没拿到数据")
    return out


def snapshot(date: str = "") -> int:
    """把当天全市场资金流快照落库（每只一行）。返回写入只数。"""
    d = date or date_cls.today().isoformat()
    rows = fetch_snapshot()
    if not rows:
        logger.warning("资金流快照为空，跳过本次")
        return 0
    _ensure()
    payload = [(d, r["code"], r["main_net"], r["main_pct"], None, None) for r in rows]
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO moneyflow_daily(date,code,main_net,main_pct,big_net,super_net) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
            "main_net=excluded.main_net, main_pct=excluded.main_pct", payload)
    purge()
    logger.info("资金流快照 %s：%d 只", d, len(payload))
    return len(payload)


def has(date: str) -> bool:
    """某天是否已经落过快照（供每日心跳幂等）。"""
    _ensure()
    with _conn() as c:
        return c.execute("SELECT 1 FROM moneyflow_daily WHERE date=? LIMIT 1", (date,)).fetchone() is not None


def fetch_history(code: str, attempts: int = 3) -> list[dict[str, Any]]:
    """拉某只股票最近约 120 天的每日资金流。失败/被限流返回空列表，不抛。

    这个端点**间歇性重置连接**（实测：同一个 UA、相隔几秒，成功与失败交替），所以单只重试几次，
    仍失败就按空处理，交给外层统计与退避。
    """
    for i in range(max(1, attempts)):
        try:
            txt = ds.em_get(_FFLOW.format(secid=_secid(code)))
            d = json.loads(txt)
            klines = ((d.get("data") or {}).get("klines")) or []
            if not klines:
                raise ValueError("返回空 klines")
            out: list[dict[str, Any]] = []
            for line in klines:
                f = str(line).split(",")
                if len(f) < 7 or len(f[0]) != 10:
                    continue
                out.append({"date": f[0], "main_net": _num(f[1]), "main_pct": _num(f[6]),
                            "big_net": _num(f[4]), "super_net": _num(f[5])})
            if out:
                return out
        except Exception as e:  # noqa: BLE001 单只失败按空处理，由调用方统计
            if i == attempts - 1:
                logger.warning("资金流历史抓取失败 %s（试了 %d 次）: %s", code, attempts, e)
            else:
                time.sleep(1.5)
    return []


def save_many(code: str, rows: list[dict[str, Any]]) -> int:
    """按 `(date, code)` upsert，重复抓同一天不会产生重复行。"""
    if not rows:
        return 0
    _ensure()
    payload = [(r.get("date"), code, r.get("main_net"), r.get("main_pct"),
                r.get("big_net"), r.get("super_net")) for r in rows if r.get("date")]
    if not payload:
        return 0
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO moneyflow_daily(date,code,main_net,main_pct,big_net,super_net) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
            "main_net=excluded.main_net, main_pct=excluded.main_pct, "
            "big_net=excluded.big_net, super_net=excluded.super_net", payload)
    return len(payload)


def of(code: str, days: int = 0) -> list[dict[str, Any]]:
    """某只股票的资金流序列（按日期升序）。days>0 只取最近这么多自然日。"""
    _ensure()
    sql = "SELECT date, main_net, main_pct, big_net, super_net FROM moneyflow_daily WHERE code=?"
    args: list[Any] = [code]
    if days > 0:
        args.append((date_cls.today() - timedelta(days=days)).isoformat())
        sql += " AND date>=?"
    sql += " ORDER BY date"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def days() -> int:
    """已积累的交易日数（周期上线门槛用它）。"""
    _ensure()
    with _conn() as c:
        row = c.execute("SELECT COUNT(DISTINCT date) n FROM moneyflow_daily").fetchone()
    return (row["n"] if row else 0) or 0


def status() -> dict[str, Any]:
    _ensure()
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT code) codes, "
                      "COUNT(DISTINCT date) days, MIN(date) first, MAX(date) latest "
                      "FROM moneyflow_daily").fetchone()
    return {"rows": r["rows"] or 0, "codes": r["codes"] or 0, "days": r["days"] or 0,
            "first": r["first"] or "", "latest": r["latest"] or "",
            "synced_at": _get_meta("synced_at"), "cursor": _get_meta("cursor")}


def purge(days: int = KEEP_DAYS) -> int:
    """删除超出保留窗口的行（按日累积的表一律配清理）。"""
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM moneyflow_daily WHERE date < ?", (cutoff,))
        return c.total_changes - before


def sync(limit: int = 0, codes: list[str] | None = None, progress_every: int = 50) -> dict[str, Any]:
    """分批抓资金流历史并落库。limit=0 跑完游标之后剩下的，断点续传；心跳挡并发。

    连续失败 `FAIL_BACKOFF` 次就退避 60 秒再继续，避免把端点打到封 IP（实测服务器 IP 突发会被重置）。
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
    full_round = limit <= 0
    batch = names[start:] if full_round else (names[start:] + names[:start])[:limit]
    _set_meta("sync_hb", str(time.time()))
    saved = done = fails = streak = 0
    try:
        for code in batch:
            rows = fetch_history(code)
            done += 1
            if rows:
                saved += save_many(code, rows)
                streak = 0
            else:
                fails += 1
                streak += 1
                if streak >= FAIL_BACKOFF:
                    logger.warning("资金流连续 %d 只失败，退避 60 秒（疑似被限流）", streak)
                    time.sleep(60)
                    streak = 0
            if progress_every and done % progress_every == 0:
                _set_meta("sync_hb", str(time.time()))
                logger.info("资金流同步：%d/%d，已写 %d 行，失败 %d 只", done, len(batch), saved, fails)
    finally:
        _set_meta("sync_hb", "")
    _set_meta("cursor", str((start + done) % len(names)))
    purge()
    if full_round:
        _set_meta("synced_at", datetime.now().isoformat(timespec="seconds"))
    logger.info("资金流同步完成：本轮 %d 只（失败 %d）/ 写 %d 行", done, fails, saved)
    return {"ok": True, "done": done, "saved": saved, "failed": fails, "full_round": full_round}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="资金流历史（data/moneyflow.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="抓资金流历史落库（可重复跑，断点续传）")
    p.add_argument("--limit", type=int, default=0, help="本轮最多抓多少只，0 表示跑完剩下的")
    sub.add_parser("status", help="看积累进度")
    args = ap.parse_args()
    if args.cmd == "sync":
        universe_store.init()
        r = sync(limit=args.limit)
        print(r)
        return 0 if r.get("ok") else 1
    print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
