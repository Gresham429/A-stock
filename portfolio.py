"""持仓记录与盈亏计算：读写 portfolio.json，**按投资画像(profile)隔离**（方案 B）。

每个画像各记各的持仓，互不干扰。存储格式：{"by_profile": {profile_id(str): [holdings]}}。
切换 active 画像后，load()/add()/remove() 自动作用于该画像的持仓。
每条持仓: {code, shares, cost_price, buy_date, note}；盈亏用实时价现算，不落库（避免陈旧）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import profile_store

logger = logging.getLogger(__name__)

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"


def _pid() -> str:
    """当前 active 画像 id（字符串键）。无画像时用 '0' 兜底。"""
    prof = profile_store.get_active()
    return str(prof["id"]) if prof else "0"


def _read_all() -> dict[str, list]:
    """读整份 {pid: [holdings]}；自动迁移旧格式（裸列表 / {holdings:[...]}）到当前画像。"""
    if not PORTFOLIO_PATH.exists():
        return {}
    try:
        data = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("读取 portfolio 失败: %s", e)
        return {}
    if isinstance(data, dict) and "by_profile" in data:
        return data["by_profile"] or {}
    # 旧格式（裸列表 或 {"holdings":[...]}）→ 整份历史持仓归到当前 active 画像，并落成新格式。
    old = data.get("holdings", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    migrated = {_pid(): old} if old else {}
    if migrated:
        _write_all(migrated)
        logger.info("portfolio 旧格式迁移到画像 %s（%d 只）", _pid(), len(old))
    return migrated


def _write_all(by_pid: dict[str, list]) -> None:
    PORTFOLIO_PATH.write_text(
        json.dumps({"by_profile": by_pid}, ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> list[dict[str, Any]]:
    """当前画像的全部持仓记录。"""
    return _read_all().get(_pid(), [])


def _save(holdings: list[dict[str, Any]]) -> None:
    allp = _read_all()
    allp[_pid()] = holdings
    _write_all(allp)


def add(code: str, shares: float, cost_price: float,
        buy_date: str = "", note: str = "") -> list[dict[str, Any]]:
    """当前画像新增/覆盖一只持仓（同代码则更新）。"""
    holdings = [h for h in load() if h.get("code") != code]
    holdings.append({
        "code": code,
        "shares": float(shares),
        "cost_price": float(cost_price),
        "buy_date": buy_date,
        "note": note,
    })
    _save(holdings)
    return holdings


def remove(code: str) -> list[dict[str, Any]]:
    """当前画像卖出/删除一只持仓。"""
    holdings = [h for h in load() if h.get("code") != code]
    _save(holdings)
    return holdings


def codes() -> list[str]:
    """当前画像持仓代码列表。"""
    return [h["code"] for h in load() if h.get("code")]


def with_pnl(quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """合并实时行情，计算市值与盈亏。

    quotes: {code: {name, price, ...}}（来自 datasources.tencent_quote）
    每条附加: name, price, market_value, cost_value, pnl_amount, pnl_pct。
    """
    out = []
    for h in load():
        q = quotes.get(h["code"], {})
        price = q.get("price", 0) or 0
        last_close = q.get("last_close", 0) or 0
        shares = h.get("shares", 0) or 0
        cost = h.get("cost_price", 0) or 0
        market_value = round(price * shares, 2)
        cost_value = round(cost * shares, 2)
        pnl_amount = round(market_value - cost_value, 2)
        pnl_pct = round((price / cost - 1) * 100, 2) if cost > 0 else None
        # 当日盈亏 = 股数 ×（现价 - 昨收）
        today_pnl = round((price - last_close) * shares, 2) if last_close else 0.0
        out.append({
            **h,
            "name": q.get("name", h["code"]),
            "price": price,
            "chg_pct": q.get("chg_pct"),
            "market_value": market_value,
            "cost_value": cost_value,
            "pnl_amount": pnl_amount,
            "pnl_pct": pnl_pct,
            "today_pnl": today_pnl,
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
