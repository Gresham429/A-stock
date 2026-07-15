"""模拟委托交易（SQLite）：多本金存档 + 按真实行情与 A股规则撮合。

规则：整手(买入100股整数倍)、涨跌停内成交、T+1(当日买入次日才可卖)、
手续费(佣金万2.5最低5元 + 卖出印花税0.05% + 过户费0.001%)。仅当日/即时成交，不挂单排队。
data/paper.db（gitignore）。行情由调用方(app)传入 quote，本模块只管账户与规则。
"""
from __future__ import annotations

import logging
import os
import sqlite3

import fees as fee_model
import profile_store
import threading
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "paper.db")
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT,
  init_capital REAL, cash REAL);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, code TEXT, name TEXT,
  shares INTEGER, sellable INTEGER, avg_cost REAL, lock_date TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, ts TEXT, code TEXT, name TEXT,
  side TEXT, otype TEXT, price REAL, shares INTEGER, amount REAL, fee REAL,
  status TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS idx_pos_acct ON positions(account_id);
CREATE INDEX IF NOT EXISTS idx_ord_acct ON orders(account_id);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── 存档 ──────────────────────────────────────────────────────────────────
def create_account(name: str, capital: float) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        cur = c.execute("INSERT INTO accounts(name,created_at,init_capital,cash) VALUES(?,?,?,?)",
                        (name or "模拟账户", now, capital, capital))
        return cur.lastrowid


def delete_account(aid: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM positions WHERE account_id=?", (aid,))
        c.execute("DELETE FROM orders WHERE account_id=?", (aid,))
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))


def list_accounts() -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY id").fetchall()]


def get_account(aid: int) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


# ── 持仓（含 T+1 结算） ────────────────────────────────────────────────────
def _settle(c: sqlite3.Connection, aid: int) -> None:
    """T+1：把锁定日 < 今天 的持仓全部转为可卖。"""
    today = date.today().isoformat()
    c.execute("UPDATE positions SET sellable=shares, lock_date=NULL "
              "WHERE account_id=? AND lock_date IS NOT NULL AND lock_date < ?", (aid, today))


def positions_of(aid: int) -> list[dict[str, Any]]:
    with _LOCK, _conn() as c:
        _settle(c, aid)
        return [dict(r) for r in
                c.execute("SELECT * FROM positions WHERE account_id=? AND shares>0 ORDER BY code",
                          (aid,)).fetchall()]


def orders_of(aid: int, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?",
                          (aid, limit)).fetchall()]


# ── 手续费 ────────────────────────────────────────────────────────────────
def fees(side: str, amount: float, sched: "fee_model.FeeSchedule | None" = None) -> float:
    """单笔交易成本（元）。费率取 sched；不给则取 **active 投资画像**的费率表。

    sched 显式传入用于 **agent 多账户并行**——每个 agent 账户绑自己的画像(各有费率)，
    不能都读全局 active，否则并行跑不同档位时成本模型全错。

    费率因券商/账户而异（同为国信，议价前万9、议价后万2.5，差 3.6 倍），
    写死会让模拟盘系统性低估成本、给出假的正收益。见 `fees.py`。
    """
    if sched is None:
        try:
            sched = profile_store.fee_schedule()
        except (sqlite3.Error, OSError) as e:
            logger.warning("读取画像费率失败，退回默认档: %s", e)
            sched = fee_model.FeeSchedule()
    return fee_model.total(side, amount, sched)


def _log_order(c, aid, code, name, side, otype, price, shares, amount, fee, status, note):
    c.execute("INSERT INTO orders(account_id,ts,code,name,side,otype,price,shares,amount,fee,status,note)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (aid, datetime.now().isoformat(timespec="seconds"), code, name, side, otype,
               price, shares, amount, fee, status, note))


def order(aid: int, code: str, name: str, side: str, otype: str, req_price: float,
          shares: int, quote: dict[str, Any], market_open: bool,
          sched: "fee_model.FeeSchedule | None" = None) -> dict[str, Any]:
    """下单撮合（即时成交）。quote 含 price/limit_up/limit_down。返回 {ok,msg,...}。

    sched：该账户的费率表（agent 多账户并行时各账户不同）；不给则用 active 画像。
    """
    price_now = float(quote.get("price") or 0)
    lu = float(quote.get("limit_up") or 0)
    ld = float(quote.get("limit_down") or 0)

    def reject(msg: str) -> dict[str, Any]:
        with _LOCK, _conn() as c:
            _log_order(c, aid, code, name, side, otype, req_price or price_now, shares,
                       0, 0, "rejected", msg)
        return {"ok": False, "msg": msg}

    if not market_open:
        return reject("非交易时段：A股 9:30-11:30 / 13:00-15:00 才可成交")
    if price_now <= 0:
        return reject("行情异常，无法成交")
    if side == "buy" and shares % 100 != 0:
        return reject("买入须为 100 股整数倍")
    if shares <= 0:
        return reject("股数须大于 0")

    # 成交价：市价=现价；限价=需即时可成交（买 limit≥现价 / 卖 limit≤现价），否则拒绝
    if otype == "market":
        fill = price_now
    else:
        if side == "buy" and req_price >= price_now:
            fill = price_now
        elif side == "sell" and req_price <= price_now:
            fill = price_now
        else:
            return reject("限价未即时触及（本模拟不挂单排队，请用市价或调整限价）")
    # 涨跌停：封板不可成交
    if side == "buy" and lu and price_now >= lu:
        return reject("涨停封板，买不到")
    if side == "sell" and ld and price_now <= ld:
        return reject("跌停封板，卖不出")

    amount = round(fill * shares, 2)
    fee = fees(side, amount, sched)
    today = date.today().isoformat()
    with _LOCK, _conn() as c:
        _settle(c, aid)
        acct = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if not acct:
            return {"ok": False, "msg": "存档不存在"}
        pos = c.execute("SELECT * FROM positions WHERE account_id=? AND code=?", (aid, code)).fetchone()
        if side == "buy":
            cost = amount + fee
            if acct["cash"] < cost:
                msg = f"资金不足：需 {cost:.2f} 元，现金 {acct['cash']:.2f} 元"
                _log_order(c, aid, code, name, side, otype, fill, shares, amount, fee, "rejected", msg)
                return {"ok": False, "msg": msg}
            c.execute("UPDATE accounts SET cash=cash-? WHERE id=?", (cost, aid))
            if pos:
                new_shares = pos["shares"] + shares
                new_cost = (pos["shares"] * pos["avg_cost"] + amount + fee) / new_shares
                c.execute("UPDATE positions SET shares=?, avg_cost=?, lock_date=? WHERE id=?",
                          (new_shares, round(new_cost, 4), today, pos["id"]))
            else:
                c.execute("INSERT INTO positions(account_id,code,name,shares,sellable,avg_cost,lock_date)"
                          " VALUES(?,?,?,?,0,?,?)",
                          (aid, code, name, shares, round((amount + fee) / shares, 4), today))
            _log_order(c, aid, code, name, side, otype, fill, shares, amount, fee, "filled", "买入成交")
        else:  # sell
            if not pos or pos["sellable"] < shares:
                have = pos["sellable"] if pos else 0
                msg = f"可卖不足：可卖 {have} 股（T+1，当日买入次日才可卖）"
                _log_order(c, aid, code, name, side, otype, fill, shares, amount, fee, "rejected", msg)
                return {"ok": False, "msg": msg}
            proceeds = amount - fee
            c.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (proceeds, aid))
            left = pos["shares"] - shares
            if left > 0:
                c.execute("UPDATE positions SET shares=?, sellable=sellable-? WHERE id=?",
                          (left, shares, pos["id"]))
            else:
                c.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
            _log_order(c, aid, code, name, side, otype, fill, shares, amount, fee, "filled", "卖出成交")
    return {"ok": True, "fill": fill, "amount": amount, "fee": fee, "side": side}
