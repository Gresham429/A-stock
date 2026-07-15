"""持仓记录与盈亏计算：读写 portfolio.json，**按投资画像隔离 + 多笔(lot)模型**。

每只股票存多笔买入 lot：`{code, lots:[{shares, cost, date}], note}`。
- 同代码再 `add()` = **追加一笔**（不覆盖旧笔）；总股数=Σ、成本=**加权平均**。
- **当日盈亏逐笔算**：当天买入的 lot 基准=**该笔买入价**（昨收那段涨跌与你无关），
  之前就持有的 lot 才用**昨收**基准。混合（昨持+今日加仓）也准。
存储：`{"by_profile": {profile_id(str): [holding...]}}`；旧格式（裸列表/{holdings:[...]}/扁平单笔）自动迁移。
盈亏用实时价现算，不落库（避免陈旧）。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import fees
import profile_store

logger = logging.getLogger(__name__)

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"


def _pid() -> str:
    """当前 active 画像 id（字符串键）。无画像时用 '0' 兜底。"""
    prof = profile_store.get_active()
    return str(prof["id"]) if prof else "0"


def _adjust_cash(delta: float) -> None:
    """按 delta 调整 active 画像的可用现金（买入为负、卖出为正），下限 0。

    画像的 cash 语义是**可用现金**：买入扣、卖出加。此前买入不扣，导致
    `_total_assets()` 把买股票的钱算两次（既在持仓市值、又还在现金里），
    总资产虚增、分级可能错档、AI 拿到的「可用资金」也是假的。
    """
    prof = profile_store.get_active()
    if not prof:
        return
    new_cash = max(float(prof.get("cash") or 0) + delta, 0.0)
    profile_store.update(int(prof["id"]), cash=round(new_cash, 2))
    logger.info("画像 %s 现金 %.2f → %.2f (%+.2f)",
                prof.get("name"), prof.get("cash") or 0, new_cash, delta)


def cash_reconcile(pid: int | None = None) -> dict[str, Any]:
    """对账：按「现金应 = 当前现金 − 未扣减的持仓成本」给出差额，**不落盘**。

    历史持仓是在「买入不扣现金」的旧逻辑下记的，现金偏高。此函数只算差额供确认，
    实际修正由调用方显式执行（涉及用户资金，不做静默改写）。
    """
    prof = profile_store.get_active() if pid is None else profile_store.get(pid)
    if not prof:
        return {"ok": False}
    hs = _read_raw().get(str(prof["id"]), [])
    sched = profile_store.fee_schedule(int(prof["id"]))
    spent = 0.0
    for h in hs:
        for lot in h.get("lots", []) or []:
            amt = float(lot.get("shares") or 0) * float(lot.get("cost") or 0)
            spent += amt + fees.total("buy", amt, sched)
    cash = float(prof.get("cash") or 0)
    return {"ok": True, "profile": prof.get("name"), "pid": int(prof["id"]),
            "cash_now": round(cash, 2), "holdings_cost": round(spent, 2),
            "cash_should_be": round(max(cash - spent, 0.0), 2),
            "delta": round(-min(spent, cash), 2), "holdings": len(hs)}


def _today() -> str:
    return date.today().isoformat()


def _migrate_holding(h: dict[str, Any]) -> dict[str, Any]:
    """旧扁平单笔 {code,shares,cost_price,buy_date} → lots 模型。"""
    if "lots" in h:
        return h
    return {"code": h.get("code", ""), "note": h.get("note", ""),
            "lots": [{"shares": float(h.get("shares") or 0),
                      "cost": float(h.get("cost_price") or 0),
                      "date": h.get("buy_date") or ""}]}


def _read_raw() -> dict[str, list]:
    """整份 {pid: [holding(lots 模型)]}；自动迁移旧格式。"""
    if not PORTFOLIO_PATH.exists():
        return {}
    try:
        data = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("读取 portfolio 失败: %s", e)
        return {}
    if isinstance(data, dict) and "by_profile" in data:
        by = data["by_profile"] or {}
    else:   # 旧格式（裸列表 或 {"holdings":[...]}）→ 归到当前画像
        old = (data.get("holdings", []) if isinstance(data, dict)
               else (data if isinstance(data, list) else []))
        by = {_pid(): old} if old else {}
        if by:
            logger.info("portfolio 旧格式迁移到画像 %s（%d 只）", _pid(), len(old))
    return {pid: [_migrate_holding(h) for h in hs] for pid, hs in by.items()}


def _write_all(by_pid: dict[str, list]) -> None:
    PORTFOLIO_PATH.write_text(
        json.dumps({"by_profile": by_pid}, ensure_ascii=False, indent=2), encoding="utf-8")


def _derive(h: dict[str, Any]) -> dict[str, Any]:
    """由 lots 派生 总股数 / 加权平均成本 / 最近买入日——上层(面板/AI)沿用旧字段名。"""
    lots = h.get("lots") or []
    shares = sum(float(lot.get("shares") or 0) for lot in lots)
    cost_val = sum(float(lot.get("shares") or 0) * float(lot.get("cost") or 0) for lot in lots)
    return {**h, "shares": shares,
            "cost_price": round(cost_val / shares, 4) if shares else 0.0,
            "buy_date": max((lot.get("date") or "") for lot in lots) if lots else ""}


def load() -> list[dict[str, Any]]:
    """当前画像的持仓（含 lots 明细 + 派生的 shares/cost_price/buy_date）。"""
    return [_derive(h) for h in _read_raw().get(_pid(), [])]


def add(code: str, shares: float, cost_price: float,
        buy_date: str = "", note: str = "") -> list[dict[str, Any]]:
    """加仓：当前画像下同代码**追加一笔 lot**（不覆盖旧笔）；新代码则建仓。

    同时按「成交额 + 买入手续费」扣减画像可用现金——现金语义是可用现金，
    不扣会让总资产把这笔钱算两次。
    """
    amount = float(shares) * float(cost_price)
    _adjust_cash(-(amount + fees.total("buy", amount, profile_store.fee_schedule())))
    allp = _read_raw()
    hs = allp.get(_pid(), [])
    lot = {"shares": float(shares), "cost": float(cost_price), "date": buy_date or _today()}
    for h in hs:
        if h.get("code") == code:
            h.setdefault("lots", []).append(lot)
            if note:
                h["note"] = note
            break
    else:
        hs.append({"code": code, "lots": [lot], "note": note})
    allp[_pid()] = hs
    _write_all(allp)
    return load()


def remove(code: str, sell_price: float | None = None) -> list[dict[str, Any]]:
    """清仓：删除当前画像下该代码的全部 lot；给了 sell_price 则把卖出净收入加回画像现金。

    sell_price 为 None 时只删持仓、不动现金（用于「记错了想删掉」而非真的卖出）。
    """
    allp = _read_raw()
    hs = allp.get(_pid(), [])
    sold = next((h for h in hs if h.get("code") == code), None)
    allp[_pid()] = [h for h in hs if h.get("code") != code]
    _write_all(allp)
    if sold is not None and sell_price:
        shares = sum(float(l.get("shares") or 0) for l in sold.get("lots", []))
        amount = shares * float(sell_price)
        _adjust_cash(+amount - fees.total("sell", amount, profile_store.fee_schedule()))
    return load()


def codes() -> list[str]:
    """当前画像持仓代码列表。"""
    return [h["code"] for h in load() if h.get("code")]


def with_pnl(quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """合并实时行情，计算市值与盈亏。

    quotes: {code: {name, price, last_close, ...}}（来自 datasources.tencent_quote）
    **当日盈亏逐笔**：今日买入的 lot 用「该笔买入价」作基准，之前持有的用「昨收」。
    """
    out = []
    today = _today()
    for h in load():
        q = quotes.get(h["code"], {})
        price = q.get("price", 0) or 0
        last_close = q.get("last_close", 0) or 0
        lots = h.get("lots") or []
        shares = h.get("shares", 0) or 0
        # 成本按 lots 精确累加（不用四舍五入后的均价，避免累计误差）
        cost_value = round(sum(float(lot.get("shares") or 0) * float(lot.get("cost") or 0)
                               for lot in lots), 2)
        market_value = round(price * shares, 2)
        pnl_amount = round(market_value - cost_value, 2)
        avg_cost = (cost_value / shares) if shares else 0
        pnl_pct = round((price / avg_cost - 1) * 100, 2) if avg_cost > 0 else None
        today_pnl = 0.0
        for lot in lots:
            ls = float(lot.get("shares") or 0)
            # 今天买的 → 基准=买入价；之前持有的 → 基准=昨收
            base = float(lot.get("cost") or 0) if (lot.get("date") or "") == today else last_close
            if base:
                today_pnl += (price - base) * ls
        out.append({
            **h,
            "name": q.get("name", h["code"]),
            "price": price,
            "chg_pct": q.get("chg_pct"),
            "market_value": market_value,
            "cost_value": cost_value,
            "pnl_amount": pnl_amount,
            "pnl_pct": pnl_pct,
            "today_pnl": round(today_pnl, 2),
        })
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """组合总市值/总成本/总盈亏/当日盈亏。"""
    mv = sum(r["market_value"] for r in rows)
    cv = sum(r["cost_value"] for r in rows)
    today = sum(r.get("today_pnl", 0) for r in rows)
    return {
        "market_value": round(mv, 2),
        "cost_value": round(cv, 2),
        "pnl_amount": round(mv - cv, 2),
        "pnl_pct": round((mv / cv - 1) * 100, 2) if cv > 0 else None,
        "today_pnl": round(today, 2),
        "today_pnl_pct": round(today / (mv - today) * 100, 2) if (mv - today) > 0 else None,
        "count": len(rows),
    }
