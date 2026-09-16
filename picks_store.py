"""观点账本：每条推荐/买卖点一行，修改不覆盖旧行而是链上新行；改口规则在这里强制。

两个 scope：public（全站三周期，data/picks_public.db）、watchlist（个人自选股，
data/users/<uid>/picks.db）。表结构相同。
记忆块 memory_block()：近 60 天最多 12 条全文；更早只放确定性汇总加至多 3 条相关记录。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Any

import news_store
import userctx

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"short": 5, "mid": 40, "long": 250}
SCOPES = ("public", "watchlist")
DECISIONS = ("new", "keep", "revise", "withdraw")
TRIGGERS = ("none", "stop_hit", "target_hit", "expired", "thesis_broken")
WINDOW_DAYS = 60
WINDOW_MAX = 12
PICKUP_MAX = 3
PICKUP_TOL = 0.05

DB_PATHS: dict[str, str | None] = {"public": None, "watchlist": None}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, created_at TEXT, scope TEXT,
  code TEXT, name TEXT, horizon TEXT, stance TEXT,
  entry_lo REAL, entry_hi REAL, exit_lo REAL, exit_hi REAL, stop REAL,
  valid_from TEXT, valid_until TEXT,
  thesis TEXT, trigger_note TEXT, basis_json TEXT,
  decision TEXT, trigger TEXT, parent_id INTEGER, status TEXT,
  px_at_call REAL, max_up REAL, max_dn REAL, ret_5d REAL, ret_20d REAL, touched TEXT DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_calls_code ON calls(scope, code, created_at);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(scope, status);
"""


def _path(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"未知 scope: {scope}")
    if DB_PATHS.get(scope):
        return DB_PATHS[scope]  # type: ignore[return-value]
    return userctx.shared_path("picks_public.db") if scope == "public" else userctx.user_path("picks.db")


def _conn(scope: str) -> sqlite3.Connection:
    return userctx.open_db(_path(scope))


def init(scope: str) -> None:
    with _conn(scope) as c:
        c.executescript(_SCHEMA)


def valid_until(horizon: str, start: str) -> str:
    """从 start 起数 HORIZON_DAYS 个交易日。"""
    n = HORIZON_DAYS.get(horizon, 5)
    d = dt.date.fromisoformat(start)
    cnt = 0
    while cnt < n:
        d += dt.timedelta(days=1)
        if news_store.is_trading_day(d):
            cnt += 1
    return d.isoformat()


def current(scope: str, code: str = "") -> list[dict[str, Any]]:
    with _conn(scope) as c:
        sql = "SELECT * FROM calls WHERE scope=? AND status='open'"
        args: list[Any] = [scope]
        if code:
            sql += " AND code=?"
            args.append(code)
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at DESC", args)]


def chain(scope: str, code: str, limit: int = 50) -> list[dict[str, Any]]:
    with _conn(scope) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (scope, code, limit))]


def _enforce(prev: dict[str, Any] | None, p: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """改口规则。返回 (decision, trigger, 价位来源行)。"""
    decision = p.get("decision") if p.get("decision") in DECISIONS else "new"
    trigger = p.get("trigger") if p.get("trigger") in TRIGGERS else "none"
    if prev is None:
        return "new", "none", p
    if decision == "new":
        decision = "keep"
    if decision in ("revise", "withdraw") and trigger == "none":
        logger.warning("picks: %s %s 无触发却要 %s，按 keep 处理", p.get("code"), p.get("horizon"), decision)
        return "keep", "none", prev
    if decision == "revise" and trigger == "stop_hit" and prev.get("touched") != "stop":
        logger.warning("picks: %s 称止损触发但结果未记录到，按 keep", p.get("code"))
        return "keep", "none", prev
    if decision == "revise" and trigger == "target_hit" and prev.get("touched") != "exit":
        logger.warning("picks: %s 称目标达成但结果未记录到，按 keep", p.get("code"))
        return "keep", "none", prev
    if decision == "keep":
        return "keep", "none", prev
    return decision, trigger, p


def apply(scope: str, proposed: dict[str, Any], today: str, run_id: str) -> dict[str, Any]:
    """按改口规则落库，返回落库行。"""
    code, horizon = proposed["code"], proposed.get("horizon", "short")
    prev_rows = [r for r in current(scope, code) if r["horizon"] == horizon]
    prev = prev_rows[0] if prev_rows else None
    decision, trigger, src = _enforce(prev, proposed)
    status = "withdrawn" if decision == "withdraw" else "open"
    vf = today if decision in ("new", "revise") else (prev or {}).get("valid_from", today)
    vu = valid_until(horizon, today) if decision in ("new", "revise") else (prev or {}).get("valid_until", valid_until(horizon, today))
    row = {
        "run_id": run_id, "created_at": today, "scope": scope, "code": code,
        "name": proposed.get("name") or (prev or {}).get("name", ""), "horizon": horizon,
        "stance": src.get("stance", "watch"),
        "entry_lo": src.get("entry_lo"), "entry_hi": src.get("entry_hi"),
        "exit_lo": src.get("exit_lo"), "exit_hi": src.get("exit_hi"), "stop": src.get("stop"),
        "valid_from": vf, "valid_until": vu,
        "thesis": proposed.get("thesis", "") if decision != "keep" else (prev or {}).get("thesis", ""),
        "trigger_note": src.get("trigger_note", ""),
        "basis_json": src.get("basis_json", "{}") if isinstance(src.get("basis_json"), str) else json.dumps(src.get("basis_json") or {}, ensure_ascii=False),
        "decision": decision, "trigger": trigger,
        "parent_id": (prev or {}).get("id"), "status": status,
        "px_at_call": proposed.get("px_at_call"),
        "max_up": None, "max_dn": None, "ret_5d": None, "ret_20d": None, "touched": "none",
    }
    cols = ",".join(row)
    with _conn(scope) as c:
        if prev:
            c.execute("UPDATE calls SET status='superseded' WHERE id=?", (prev["id"],))
        cur = c.execute(f"INSERT INTO calls({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
        row["id"] = cur.lastrowid
    return row


def expire(scope: str, today: str) -> int:
    with _conn(scope) as c:
        return c.execute("UPDATE calls SET status='expired' WHERE scope=? AND status='open' AND valid_until<?",
                         (scope, today)).rowcount


def staple(scope: str, code: str, bars: list[dict[str, Any]], today: str) -> int:
    """把给出后的走势贴回近 60 天内的行：最高/最低相对给出价、5 日/20 日收益、碰到哪个区间。"""
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    n = 0
    with _conn(scope) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at>=? AND status IN ('open','superseded')",
            (scope, code, since))]
        for r in rows:
            after = [b for b in bars if str(b.get("date", ""))[:10] > r["created_at"]]
            px0 = r.get("px_at_call")
            if not after or not px0:
                continue
            hi = max(float(b["high"]) for b in after)
            lo = min(float(b["low"]) for b in after)
            closes = [float(b["close"]) for b in after]
            ret5 = round((closes[min(4, len(closes) - 1)] / px0 - 1) * 100, 2) if len(closes) >= 5 else None
            ret20 = round((closes[19] / px0 - 1) * 100, 2) if len(closes) >= 20 else None
            touched = "none"
            if r.get("stop") and lo <= r["stop"]:
                touched = "stop"
            elif r.get("exit_lo") and hi >= r["exit_lo"]:
                touched = "exit"
            elif r.get("entry_hi") and lo <= r["entry_hi"]:
                touched = "entry"
            c.execute("UPDATE calls SET max_up=?, max_dn=?, ret_5d=?, ret_20d=?, touched=? WHERE id=?",
                      (round((hi / px0 - 1) * 100, 2), round((lo / px0 - 1) * 100, 2), ret5, ret20, touched, r["id"]))
            n += 1
    return n


def rollup(scope: str, code: str, before: str) -> dict[str, Any]:
    """before 之前的确定性汇总：次数、买点被碰比例、20 日收益均值与最差、最近立场。"""
    with _conn(scope) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at<? AND decision!='keep' ORDER BY created_at",
            (scope, code, before))]
    if not rows:
        return {"n": 0}
    hit = sum(1 for r in rows if r.get("touched") in ("entry", "exit", "stop"))
    r20 = [r["ret_20d"] for r in rows if r.get("ret_20d") is not None]
    return {"n": len(rows), "entry_hit_rate": round(hit / len(rows), 2),
            "avg_ret20": round(sum(r20) / len(r20), 2) if r20 else None,
            "worst_ret20": min(r20) if r20 else None, "last_stance": rows[-1].get("stance")}


def _fmt_row(r: dict[str, Any]) -> str:
    res = ""
    if r.get("max_up") is not None:
        res = f" 结果:最高{r['max_up']:+}% 最低{r['max_dn']:+}%"
        if r.get("ret_5d") is not None:
            res += f" 5日{r['ret_5d']:+}%"
        if r.get("touched") and r["touched"] != "none":
            res += f" 碰到{r['touched']}"
    return (f"{r['created_at']} {r['horizon']} {r['stance']} 买{r.get('entry_lo')}-{r.get('entry_hi')} "
            f"卖{r.get('exit_lo')}-{r.get('exit_hi')} 止{r.get('stop')} {r['decision']}/{r['trigger']}{res}")


def memory_block(scope: str, code: str, price: float | None, today: str) -> str:
    """给模型的两层记忆：近窗全文 + 远期汇总与相关挑选。"""
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    with _conn(scope) as c:
        recent = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (scope, code, since, WINDOW_MAX))]
        old = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at<? AND decision!='keep' ORDER BY created_at DESC",
            (scope, code, since))]
    lines = []
    if recent:
        lines.append(f"【{recent[0]['name']} 近 60 天观点（新在前）】")
        lines += [_fmt_row(r) for r in recent]
    if old:
        rl = rollup(scope, code, since)
        lines.append(f"【远期汇总】共 {rl['n']} 次，碰到比例 {rl.get('entry_hit_rate')}，20日收益均值 {rl.get('avg_ret20')}，最差 {rl.get('worst_ret20')}，最近立场 {rl.get('last_stance')}")
        picked = []
        for r in old:
            near = price and any(v and abs(v - price) <= price * PICKUP_TOL
                                 for v in (r.get("entry_lo"), r.get("entry_hi"), r.get("exit_lo"), r.get("exit_hi")))
            hit = r.get("touched") in ("stop", "exit")
            if near or hit:
                picked.append(r)
            if len(picked) >= PICKUP_MAX:
                break
        if picked:
            lines.append("【远期相关记录】")
            lines += [_fmt_row(r) for r in picked]
    return "\n".join(lines) if lines else "（该股尚无历史观点）"
