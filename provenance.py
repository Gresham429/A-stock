"""AI 建议的数据溯源(provenance) + 可验证依据(verified attribution)。

两层，主次分明：
- A 数据溯源 `build_provenance`：后端拼 prompt 时已掌握全部输入，确定性生成
  「本次分析用了哪些源 + 新鲜度 + 条数」。不经过 AI、100% 稳定。
- B 结论依据 `verify_basis`：校验 AI 结构化 `basis` 里引用的信号名 / 规则 ID。
  信号名只能来自闭集 `SIGNAL_DEFS`（AI 只引用名字，值由后端权威填充，AI 不可能编）；
  规则 ID 必须在本次注入集里。`ok` = 可核对，`bad` = AI 引用了不存在的信号/规则。

`SIGNAL_DEFS` 是单一事实源：既生成注入 prompt 的「可引用信号」清单，
又用于后端填值/校验，避免 prompt 与校验漂移。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _fin0(financials: list[dict] | None, field: str) -> Any:
    return financials[0].get(field) if financials else None


# (信号名, 分组, 取值函数(ctx)->原始值, 单位后缀)
# ctx = {"quote","metrics","vol_hist","financials","market_ctx"}
SIGNAL_DEFS: list[tuple[str, str, Callable[[dict], Any], str]] = [
    ("现价",         "行情", lambda c: (c.get("quote") or {}).get("price"), ""),
    ("PE",           "行情", lambda c: (c.get("quote") or {}).get("pe_ttm"), ""),
    ("PB",           "行情", lambda c: (c.get("quote") or {}).get("pb"), ""),
    ("换手率",       "行情", lambda c: (c.get("quote") or {}).get("turnover"), "%"),
    ("涨跌幅",       "行情", lambda c: (c.get("quote") or {}).get("chg_pct"), "%"),
    ("年化波动",     "波动", lambda c: (c.get("metrics") or {}).get("vol"), "%"),
    ("20日涨幅",     "波动", lambda c: (c.get("metrics") or {}).get("cum20"), "%"),
    ("区间位置",     "波动", lambda c: (c.get("metrics") or {}).get("range_pos"), "%"),
    ("主力20日净流入", "资金", lambda c: (c.get("metrics") or {}).get("net20"), "亿"),
    ("60日高",       "波动", lambda c: (c.get("vol_hist") or {}).get("hi"), ""),
    ("60日低",       "波动", lambda c: (c.get("vol_hist") or {}).get("lo"), ""),
    ("20日振幅",     "波动", lambda c: (c.get("vol_hist") or {}).get("atr_pct"), "%"),
    ("营收同比",     "财报", lambda c: _fin0(c.get("financials"), "revenue_yoy"), "%"),
    ("净利同比",     "财报", lambda c: _fin0(c.get("financials"), "profit_yoy"), "%"),
    ("大盘研判",     "大盘", lambda c: (c.get("market_ctx") or {}).get("regime"), ""),
]

_SIGNAL_MAP: dict[str, tuple] = {d[0]: d for d in SIGNAL_DEFS}
_MARKET_ONLY = {"大盘研判"}   # 仅 entry 有大盘上下文


def signal_vocab(has_market: bool = False) -> str:
    """注入 prompt 的「可引用信号」清单（AI 只能从这里选）。position 无大盘研判。"""
    return " / ".join(d[0] for d in SIGNAL_DEFS if has_market or d[0] not in _MARKET_ONLY)


def _fmt(value: Any, suffix: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        value = round(value, 2)
    return f"{value}{suffix}"


def _parse_rid(ref: Any) -> int | None:
    s = str(ref).strip().upper().lstrip("R").rstrip("。.")
    try:
        return int(s)
    except ValueError:
        return None


def verify_basis(ai_basis: Any, ctx: dict,
                 rule_titles: dict[int, str]) -> list[dict]:
    """校验 AI 的 basis，富化为可展示依据。

    ai_basis: [{"claim": str, "signals": [名...], "rules": ["R12"...]}]
    ctx: 取值上下文（见 SIGNAL_DEFS）。
    rule_titles: {id: title} 本次实际注入的规则集。
    返回: [{"claim": str, "refs": [{kind, name, value?, title?, status}]}]
          status: ok=可核对 / bad=AI 引用了闭集外信号或未注入规则。
    """
    if not isinstance(ai_basis, list):
        return []
    out: list[dict] = []
    for item in ai_basis:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        refs: list[dict] = []
        for sig in (item.get("signals") or []):
            name = str(sig).strip()
            definition = _SIGNAL_MAP.get(name)
            if definition:
                value = _fmt(definition[2](ctx), definition[3])
                # 名在闭集即 ok；值恰好取不到（该源缺数据）标 na，不算 AI 编造
                status = "ok" if value is not None else "na"
                refs.append({"kind": "signal", "name": name,
                             "value": value, "status": status})
            else:
                refs.append({"kind": "signal", "name": name, "status": "bad"})
        for rule in (item.get("rules") or []):
            rid = _parse_rid(rule)
            if rid is not None and rid in rule_titles:
                refs.append({"kind": "rule", "name": f"R{rid}",
                             "title": rule_titles[rid], "status": "ok"})
            else:
                refs.append({"kind": "rule", "name": str(rule).strip(), "status": "bad"})
        out.append({"claim": claim, "refs": refs})
    return out


def build_provenance(quote: dict | None, metrics: dict | None,
                     vol_hist: dict | None, financials: list[dict] | None,
                     news: list[dict] | None, rules_meta: dict | None,
                     market_ctx: dict | None = None, web_on: bool = False,
                     updated: str = "") -> dict:
    """A 层：确定性溯源对象。rules_meta = {count, scenario}。"""
    src: list[dict] = []
    q = quote or {}
    src.append({"key": "quote", "label": "实时行情", "fresh": updated,
                "detail": {"现价": q.get("price"), "PE": q.get("pe_ttm"),
                           "PB": q.get("pb"), "换手率": q.get("turnover")}})
    m = metrics or {}
    src.append({"key": "metrics", "label": "波动/资金",
                "detail": {"年化波动": m.get("vol"), "20日涨幅": m.get("cum20"),
                           "区间位置": m.get("range_pos"), "主力20日净流入": m.get("net20")}})
    if vol_hist:
        src.append({"key": "vol_hist", "label": "60日波动史",
                    "detail": {"60日高": vol_hist.get("hi"), "60日低": vol_hist.get("lo"),
                               "20日振幅": vol_hist.get("atr_pct")}})
    if financials:
        f0 = financials[0]
        src.append({"key": "financials", "label": "财报", "fresh": f0.get("period", ""),
                    "detail": {"营收同比": f0.get("revenue_yoy"),
                               "净利同比": f0.get("profit_yoy")}})
    if news:
        src.append({"key": "news", "label": "新闻", "count": len(news),
                    "fresh": (news[0].get("date", "")[:10] if news else ""),
                    "titles": [n.get("title", "") for n in news[:8]]})
    if rules_meta:
        src.append({"key": "rules", "label": "交易规则",
                    "count": rules_meta.get("count", 0),
                    "scenario": rules_meta.get("scenario", "")})
    if market_ctx:
        src.append({"key": "market", "label": "大盘研判",
                    "detail": market_ctx.get("one_liner") or market_ctx.get("regime")})
    src.append({"key": "web", "label": "联网", "enabled": bool(web_on)})
    return {"sources": src}
