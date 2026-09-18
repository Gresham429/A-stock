"""到点提醒：价格摸到买点 / 卖点 / 止损就推手机（用户子项目 C）。

数据来源是**观点账本**（AI 的三周期与自选股买卖点）加用户的手动覆盖，不另算一套点位：

1. 有效周期只看 `picks_pipeline.visible_horizons()`。中长线在面板上还是 mask 状态时也不提醒，
   免得「面板上不展示、手机却推过来」这种自相矛盾（用户 2026-09-17 明确先 mask）。
2. 每个标的的有效点位 = 手动覆盖（`alerts_store.points`）优先，没有则用账本里 AI 给的
   `entry_lo/hi`、`exit_lo/hi`、`stop`。AI 每天更新不会覆盖用户改过的点位。
3. 「接近」怎么算：进入区间，或距离区间边界 `ALERT_APPROACH_PCT`（默认 1%）以内。
   这个百分比是**纪律参数**（用户偏好，不是收益预测），所以放在环境变量里可调，
   并在提醒正文里写清是「已进入」还是「接近」。
4. 同一标的同一类触发每天最多一次（`sent` 表去重）；去过重了也不重复推。

交易时段之外不发（用 `agent_loop._market_open()`，与撮合同一个事实源）；手动 `/check`
可以 `force=True` 绕过时段，方便当场验证配置。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date as date_cls
from typing import Any, Callable

import alerts_store
import datasources as ds
import news_store
import notify
import picks_pipeline
import picks_store
import userctx

logger = logging.getLogger(__name__)

APPROACH_PCT = float(os.environ.get("ASTOCK_ALERT_APPROACH_PCT", "1.0"))  # 纪律参数
TICK_SEC = int(os.environ.get("ASTOCK_ALERT_TICK_SEC", "300"))
MAX_BODY = 120          # 依据只取一行，太长在手机上没用


def _market_open() -> bool:
    """复用撮合的交易时段判定，别在提醒里再写一份时间窗（两处会漂移）。"""
    import agent_loop
    return agent_loop._market_open()


def _ledger_rows(scope: str, visible: list[str]) -> list[dict[str, Any]]:
    """账本里在有效期内的行，按可见周期过滤。"""
    try:
        picks_store.init(scope)
        rows = picks_store.current(scope) if scope == "watchlist" else picks_store.latest_run(scope)
    except Exception as e:  # noqa: BLE001 账本读不出来就不提醒，不影响其余
        logger.warning("提醒：读 %s 账本失败: %s", scope, e)
        return []
    today = date_cls.today().isoformat()
    out = []
    for r in rows:
        if r.get("horizon") and r["horizon"] not in visible:
            continue
        if (r.get("valid_until") or "") and r["valid_until"] < today:
            continue
        # 生成当天不提醒：AI 的价位是贴着生成时的现价给的（`picks_levels` 从当前结构算候选），
        # 当天推等于把用户 16:00 刚在面板上看过的结论再念一遍。从第二个交易日起，
        # 价格再摸到才算新信息。手设点位不受这条约束（那是用户当场的意思）。
        if str(r.get("created_at") or "")[:10] >= today:
            continue
        out.append(r)
    return out


def effective_levels(visible: list[str] | None = None) -> list[dict[str, Any]]:
    """当前所有有效点位：手动覆盖在前，AI 点位在后（同一代码以手动为准）。"""
    visible = visible if visible is not None else picks_pipeline.visible_horizons()
    manual = {p["code"]: p for p in alerts_store.points(only_enabled=True)}
    out: dict[str, dict[str, Any]] = {}
    for scope in ("public", "watchlist"):
        for r in _ledger_rows(scope, visible):
            code = r.get("code") or ""
            if not code or code in out:
                continue                      # 自选股覆盖公共：同一只按自选股那份（更贴近用户）
            out[code] = {"code": code, "name": r.get("name") or code, "horizon": r.get("horizon") or "",
                         "scope": scope, "source": "ai", "thesis": _one_line(r.get("thesis")),
                         "buy_lo": r.get("entry_lo"), "buy_hi": r.get("entry_hi"),
                         "sell_lo": r.get("exit_lo"), "sell_hi": r.get("exit_hi"),
                         "stop": r.get("stop")}
    for code, p in manual.items():
        base = out.get(code) or {"code": code, "name": code, "horizon": p.get("horizon") or "",
                                 "scope": "manual", "thesis": p.get("note") or ""}
        merged = dict(base)
        merged.update({"source": "manual", "manual": p, "note": p.get("note") or ""})
        for k in ("buy_lo", "buy_hi", "sell_lo", "sell_hi", "stop"):
            if p.get(k) is not None:
                merged[k] = p[k]
        out[code] = merged
    return list(out.values())


def _one_line(text: Any) -> str:
    s = " ".join(str(text or "").split())
    return s[:MAX_BODY]


def _hit(row: dict[str, Any], price: float) -> tuple[str, str] | None:
    """返回 (kind, 措辞) 或 None。止损优先：同时摸到买点与止损时说的是止损。"""
    tol = APPROACH_PCT / 100.0
    stop = row.get("stop")
    if stop and price <= float(stop) * (1 + tol):
        inside = price <= float(stop)
        return "stop", ("已跌破止损 %.2f" % float(stop)) if inside else ("逼近止损 %.2f" % float(stop))
    lo, hi = row.get("buy_lo"), row.get("buy_hi")
    if lo is not None and hi is not None:
        lo, hi = float(lo), float(hi)
        if lo <= price <= hi:
            return "buy", "已进入买点区间 %.2f 到 %.2f" % (lo, hi)
        if hi < price <= hi * (1 + tol):
            return "buy", "接近买点区间 %.2f 到 %.2f" % (lo, hi)
        if lo * (1 - tol) <= price < lo:
            return "buy", "接近买点区间 %.2f 到 %.2f" % (lo, hi)
    lo, hi = row.get("sell_lo"), row.get("sell_hi")
    if lo is not None and hi is not None:
        lo, hi = float(lo), float(hi)
        if lo <= price <= hi:
            return "sell", "已进入卖点区间 %.2f 到 %.2f" % (lo, hi)
        if hi < price <= hi * (1 + tol):
            return "sell", "接近卖点区间 %.2f 到 %.2f" % (lo, hi)
        if lo * (1 - tol) <= price < lo:
            return "sell", "接近卖点区间 %.2f 到 %.2f" % (lo, hi)
    return None


def _compose(row: dict[str, Any], kind: str, wording: str, price: float) -> tuple[str, str]:
    kind_cn = {"buy": "买点", "sell": "卖点", "stop": "止损"}[kind]
    code = row["code"]
    name = str(row.get("name") or "")
    who = f"{name}({code})" if name and name != code else code   # 手设点位可能没名字，别写成 600519(600519)
    title = f"{kind_cn} {who}"
    bits = [f"现价 {price:.2f}，{wording}"]
    if row.get("horizon"):
        bits.append(f"周期 {'短线' if row['horizon'] == 'short' else '中线' if row['horizon'] == 'mid' else '长线'}")
    bits.append("点位来自手动设置" if row.get("source") == "manual" else "点位来自 AI 分析")
    if row.get("thesis"):
        bits.append(f"依据 {row['thesis']}")
    bits.append("参考信号，不构成投资建议")
    return title, "；".join(bits)


def check(uid: str = "", force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """扫一遍并推送。`force` 跳过交易时段判定（手动验证用），`dry_run` 只返回打算发什么。"""
    uid = uid or userctx.get_uid() or ""
    if not force and not _market_open():
        return {"ok": True, "skipped": "非交易时段", "sent": 0, "hits": []}
    tg = alerts_store.targets(only_enabled=True)
    if not dry_run and not tg:
        return {"ok": True, "skipped": "还没有配置提醒目标", "sent": 0, "hits": []}
    rows = effective_levels()
    if not rows:
        return {"ok": True, "skipped": "没有有效期内的点位", "sent": 0, "hits": []}
    codes = [r["code"] for r in rows]
    try:
        quotes = ds.tencent_quote(codes)
    except Exception as e:  # noqa: BLE001 取不到行情就这轮不发，下一轮再来
        logger.warning("提醒：取行情失败: %s", e)
        return {"ok": False, "error": f"取行情失败: {e}", "sent": 0, "hits": []}
    already = alerts_store.sent_today()
    hits: list[dict[str, Any]] = []
    for row in rows:
        price = (quotes.get(row["code"]) or {}).get("price")
        if not price:
            continue
        hit = _hit(row, float(price))
        if not hit:
            continue
        kind, wording = hit
        if (row["code"], kind) in already:
            continue
        title, body = _compose(row, kind, wording, float(price))
        rec = {"code": row["code"], "name": row["name"], "kind": kind, "price": float(price),
               "title": title, "body": body, "source": row.get("source", "ai")}
        if dry_run:
            hits.append(rec)
            continue
        res = notify.send_all(tg, title, body)
        alerts_store.mark_sent(row["code"], kind, float(price), res["sent"], res["total"])
        rec["pushed"] = res["sent"]
        hits.append(rec)
        if res["failed"]:
            logger.warning("提醒：%s %s 有 %d 台没发出去", row["code"], kind, res["failed"])
    if not dry_run:
        alerts_store.purge()
    logger.info("提醒检查：点位 %d 个，触发 %d 条，目标 %d 台", len(rows), len(hits), len(tg))
    return {"ok": True, "checked": len(rows), "sent": len(hits), "hits": hits,
            "targets": len(tg)}


def loop_forever(uids_fn: Callable[[], list[str]]) -> None:
    """每 TICK_SEC 秒给每个配了目标的账号扫一遍。交易所之外空转，成本接近零。"""
    while True:
        try:
            for uid in uids_fn():
                with userctx.as_user(uid):
                    if not alerts_store.targets(only_enabled=True):
                        continue
                    check(uid)
        except Exception as e:  # noqa: BLE001 提醒循环绝不能因单次异常停摆
            logger.warning("提醒循环异常（下次继续）：%s", e)
        time.sleep(TICK_SEC)


def notify_test(user: str = "") -> dict[str, Any]:
    """给所有目标发一条测试，用来确认手机收得到（不写去重表）。"""
    tg = alerts_store.targets(only_enabled=True)
    if not tg:
        return {"ok": False, "error": "还没有配置提醒目标"}
    title = "提醒测试"
    body = f"{date_cls.today().isoformat()} 这是一条测试消息；收到说明通道配好了。"
    if news_store.is_trading_day():
        body += " 当前是交易日。"
    res = notify.send_all(tg, title, body)
    out: dict[str, Any] = {"ok": res["sent"] > 0, **res}
    fails = [d for d in res["detail"] if not d["ok"]]
    if fails and not res["sent"]:
        # 除了逐台的 detail，再给一个汇总的 error：任何客户端只读 error 也能知道为什么失败
        out["error"] = "；".join(f"{d.get('label') or d.get('channel') or ''} {d.get('msg') or ''}".strip()
                                for d in fails)
    return out
