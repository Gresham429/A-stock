"""复盘往下走一层：个股线索（用户子项目 A）。

板块层面的复盘回答「今天盘面怎么样」，这里回答「具体哪几只值得看一眼、它们各自是什么状态」。
设计见 `plan/2026-09-17-review-stocks-design.md`，三句话：

1. **挑股透明不打分**。三路来源各带理由标签（板块资金流入第 N / N 连板 / 龙虎榜净买 X 万），
   去重取前 `STOCK_N`。个股层面没有历史验证，按 PITFALLS #1 不装成有依据的分数。
2. **数据全部复用现有面**。行情腾讯、财务本地面板、舆情本地新闻库、资金新浪、板块归属本地库，
   没有新数据源，没有新密钥。
3. **不给买卖点**。输出按白名单重建，价位类字段一律丢弃（买卖点归子项目 B，越界会让两处结论打架）。

公开块只含全市场标的、进复盘 envelope；自选股那一层按人隔离，落个人目录，公开复盘里不出现。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Optional

import config
import datasources as ds
import fundamentals_store
import llm
import news_store
import screening
import universe_store
import userctx

logger = logging.getLogger(__name__)

STOCK_N = 8            # 公开块取几只
WATCH_N = 6            # 自选股那一层最多几只
SECTOR_TOP = 3         # 取资金流前几个板块
LHB_TOP = 2            # 龙虎榜取净买额前几只
NEWS_PER_STOCK = 3     # 每只带几条本地新闻标题
SCHEMA_VERSION = 1

_FLASH = getattr(llm, "FLASH_MODEL", "deepseek-v4-flash")

# 白名单：只收这些键。价位类一律不在表里（不给买卖点）。
_FIELDS = ("code", "name", "sector", "tagline", "fundamentals", "financials",
           "news", "driver", "risk", "watch")

_BOUNDARY = (
    "只据给定数据说话，缺数据就写「当日无显著消息」或「数据缺失」，不编造。"
    "**不给买卖点、不给目标价、不建议仓位**（价位与买卖时机由另一套流程负责）。"
)


# ── 挑股（纯函数，可离线测）──────────────────────────────────────────────
def pick(zt: list[dict], lhb: Optional[list[dict]] = None,
         sector_flow: Optional[list[dict]] = None,
         limit: int = STOCK_N) -> list[dict]:
    """三路来源挑个股线索。返回 [{code,name,reason,source,pct,limit_days,...}]，顺序即优先级。

    涨停池的 `industry` 是东财行业板块名，与概念板块资金流的名字不一定对得上；
    对不上时用 `universe_store.codes_of(板块名)` 取该板块市值前 2 只兜底（行业龙头通常是资金主线）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(row: dict, reason: str, source: str) -> None:
        """加入并就地写上理由。**只在真的新增时写**：被去重的那次调用不能去改已有那行，
        否则后一路来源会把前一路的理由覆盖掉（2026-09-17 踩过：连板把板块理由冲掉）。"""
        code = str(row.get("code") or "")
        if not code or code in seen:
            return
        seen.add(code)
        r = dict(row)
        r["reason"] = reason
        r["source"] = source
        out.append(r)

    # 1) 板块资金流前列的成员
    for i, sec in enumerate((sector_flow or [])[:SECTOR_TOP], 1):
        name = str(sec.get("name") or "")
        if not name:
            continue
        why = f"板块资金流入第 {i}（{name} {sec.get('main_net_yi')} 亿）"
        hits = [z for z in zt if str(z.get("industry") or "") == name]
        if hits:
            hits.sort(key=lambda x: (x.get("limit_days") or 0, x.get("seal_fund") or 0),
                      reverse=True)
            for z in hits[:2]:
                add(z, why, "sector")
            continue
        try:
            codes = (universe_store.codes_of(name) or [])[:2]
        except Exception as e:  # noqa: BLE001 板块名对不上不拖垮挑股
            logger.debug("个股线索：板块 %s 取成员失败: %s", name, e)
            codes = []
        for c in codes:
            z = next((x for x in zt if x.get("code") == c), None)
            add(z or {"code": c, "name": "", "pct": None, "limit_days": 0}, why, "sector")

    # 2) 连板梯队
    for z in sorted(zt, key=lambda x: (x.get("limit_days") or 0, x.get("seal_fund") or 0),
                    reverse=True)[:3]:
        add(z, f"{z.get('limit_days') or 0} 连板", "ladder")

    # 3) 龙虎榜净买额前列（只收当日上涨的）
    taken = 0
    for r in sorted(lhb or [], key=lambda x: x.get("net_buy_wan") or 0, reverse=True):
        if (r.get("change_pct") or 0) <= 0:
            continue
        before = len(out)
        add(r, f"龙虎榜净买 {r.get('net_buy_wan')} 万（{r.get('reason') or ''}）", "lhb")
        taken += 1 if len(out) > before else 0
        if taken >= LHB_TOP:
            break

    return out[:limit]


# ── 逐只补数据（本地为主，行情与资金走网络）──────────────────────────────
def profile(codes: list[str], lhb: Optional[list[dict]] = None) -> dict[str, dict]:
    """给一批代码补行情/估值、财务、舆情、资金、板块归属、龙虎榜命中。"""
    codes = [str(c) for c in codes if c]
    if not codes:
        return {}
    quotes = ds.tencent_quote(codes)
    fins = fundamentals_store.latest_map(codes)
    smap = universe_store.sectors_map(codes)
    metrics = screening._metrics_of(codes)
    lhb_map = {str(r.get("code")): r for r in (lhb or [])}
    out: dict[str, dict] = {}
    for c in codes:
        q = quotes.get(c) or {}
        f = fins.get(c) or {}
        m = metrics.get(c) or {}
        primary, sub = smap.get(c) or ("", "")
        try:
            news = [str(x.get("title") or "") for x in news_store.query(code=c, limit=NEWS_PER_STOCK + 2)]
        except Exception as e:  # noqa: BLE001 本地库读不到就当没消息
            logger.debug("个股线索：%s 本地新闻失败: %s", c, e)
            news = []
        out[c] = {
            "code": c, "name": q.get("name") or c,
            "price": q.get("price"), "pct": q.get("chg_pct"),
            "turnover": q.get("turnover"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
            "mcap_yi": q.get("mcap_yi"), "lot_cost": q.get("lot_cost"),
            "primary": primary, "sub": sub,
            "fin_period": f.get("period"), "revenue_yoy": f.get("revenue_yoy"),
            "profit_yoy": f.get("profit_yoy"),
            "net5": m.get("net5"), "net20": m.get("net20"),
            "news": [n for n in news if n][:NEWS_PER_STOCK],
            "lhb": (lhb_map.get(c) or {}).get("net_buy_wan"),
            "lhb_reason": (lhb_map.get(c) or {}).get("reason") or "",
        }
    return out


# ── 一次 LLM 调用出档案 ──────────────────────────────────────────────────
def _fmt_row(r: dict[str, Any]) -> str:
    bits = [f"{r.get('name')}({r.get('code')})",
            f"现价 {r.get('price')} 涨跌 {r.get('pct')}% 换手 {r.get('turnover')}%",
            f"板块 {r.get('primary')}/{r.get('sub')}",
            f"PE {r.get('pe_ttm')} PB {r.get('pb')} 市值 {r.get('mcap_yi')} 亿"]
    if r.get("reason"):
        bits.append(f"入选理由 {r['reason']}")
    if r.get("fin_period"):
        bits.append(f"财务 {r['fin_period']} 营收同比 {r.get('revenue_yoy')}% "
                    f"利润同比 {r.get('profit_yoy')}%")
    else:
        bits.append("财务 数据缺失")
    if r.get("net20") is not None:
        bits.append(f"主力净流入 5 日 {r.get('net5')} / 20 日 {r.get('net20')}")
    if r.get("lhb") is not None:
        bits.append(f"龙虎榜净买 {r['lhb']} 万（{r.get('lhb_reason')}）")
    if r.get("news"):
        bits.append("近期消息：" + "；".join(r["news"]))
    else:
        bits.append("近期消息：无")
    return " | ".join(str(b) for b in bits)


def analyze(date: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """一次调用出全部档案。未配 key 或失败返回 []（上层记 warning，不影响复盘本体）。"""
    if not rows or not config.llm_enabled():
        return []
    data = "\n".join(f"{i+1}. {_fmt_row(r)}" for i, r in enumerate(rows))
    prompt = (
        f"{_BOUNDARY}\n\n"
        f"你是复盘里的『个股线索』分析师。{date} 收盘后，从板块资金流与涨幅榜挑出下面这几只，"
        "给每只写一份短档案（每项一句话，不重复数据、不说套话）：\n"
        "tagline 今天为什么被看到；fundamentals 主营与行业地位；financials 财务变化（只据给定数字）；"
        "news 消息面（没有就写「当日无显著消息」）；driver 赌的是什么逻辑；"
        "risk 这条逻辑最可能怎么失败；watch 后续盯哪个信号能验证或推翻。\n"
        "**不要给买卖点、目标价、仓位建议。**\n"
        f"只返回 JSON 对象 {{\"items\": [{{\"code\": \"...\", \"name\": \"...\", \"sector\": \"...\", "
        "\"tagline\": \"...\", \"fundamentals\": \"...\", \"financials\": \"...\", \"news\": \"...\", "
        "\"driver\": \"...\", \"risk\": \"...\", \"watch\": \"...\"}}]}}，"
        "items 的顺序与上面一致，每只一条，不要多也不要少。\n\n" + data)
    try:
        raw = llm._chat([{"role": "user", "content": prompt}], json_mode=True,
                        temperature=0.15, max_tokens=6000, model=_FLASH)
        obj = llm._parse_json(raw)
    except (llm.LLMError, ValueError) as e:
        logger.warning("个股线索 AI 失败（复盘本体不受影响）: %s", e)
        return []
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        logger.warning("个股线索返回结构不对（无 items 数组）")
        return []
    by_code = {str(r["code"]): r for r in rows}
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "")
        base = by_code.get(code)
        if not base:
            # 模型回了没被挑中的代码：只保留白名单字段里能对上原始数据的那几个，其余丢弃。
            # 宁可少一条，也不让模型凭空多出标的（用户看到的名字必须能在数据里查到）。
            continue
        row = {k: it.get(k) for k in _FIELDS}
        row["code"] = code
        row["name"] = base.get("name") or it.get("name") or code
        row["sector"] = it.get("sector") or f"{base.get('primary')}/{base.get('sub')}".strip("/")
        row.update({k: base.get(k) for k in ("price", "pct", "turnover", "pe_ttm", "pb",
                                             "mcap_yi", "reason", "source", "net5", "net20",
                                             "fin_period", "revenue_yoy", "profit_yoy")})
        out.append(row)
    return out


# ── 组装与落盘 ───────────────────────────────────────────────────────────
def build_public(date: str, zt: list[dict], lhb: Optional[list[dict]],
                 sector_flow: Optional[list[dict]],
                 limit: int = STOCK_N) -> dict[str, Any]:
    """公开块的完整流程：挑股 → 补数据 → AI。返回可直接放进 envelope 的 dict。"""
    picked = pick(zt, lhb, sector_flow, limit)
    if not picked:
        return {"items": [], "degraded": False, "note": "当日没有可挑的标的"}
    prof = profile([p["code"] for p in picked], lhb)
    rows = []
    for p in picked:
        base = prof.get(p["code"])
        if not base:
            continue
        base["reason"] = p.get("reason") or ""
        base["source"] = p.get("source") or ""
        rows.append(base)
    items = analyze(date, rows)
    return {"items": items, "degraded": not items, "picked": len(rows),
            "note": "" if items else "AI 未生成个股档案（复盘本体不受影响）"}


def _user_path() -> str:
    return userctx.user_path("review_stocks.json")


def load_user() -> Optional[dict]:
    try:
        with open(_user_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("自选股复盘档案读取失败: %s", e)
        return None


def save_user(block: dict) -> bool:
    path = _user_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(block, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning("自选股复盘档案写入失败: %s", e)
        return False


def build_watchlist(codes: list[str], date: str, force: bool = False) -> dict[str, Any]:
    """自选股那一层。按交易日缓存：当天已生成且非 force 就直接返回缓存。

    自选股是个人的，所以这块**不进公开复盘**，落 `data/users/<uid>/review_stocks.json`。
    """
    today = datetime.date.today().isoformat()
    if not force:
        cached = load_user()
        if cached and cached.get("date") == today and cached.get("items"):
            return cached
    codes = [str(c) for c in codes if c][:WATCH_N]
    if not codes:
        return {"date": today, "items": [], "note": "自选股为空", "generated_at": _now()}
    prof = profile(codes)
    rows = [prof[c] for c in codes if c in prof]
    for r in rows:
        r["reason"] = "自选股"
        r["source"] = "watchlist"
    items = analyze(date, rows)
    block = {"schema_version": SCHEMA_VERSION, "date": today, "target_date": date,
             "generated_at": _now(), "items": items, "picked": len(rows),
             "degraded": not items,
             "note": "" if items else "AI 未生成（未配 key 或调用失败）"}
    # 即使 items 为空也落盘：当天算跑过，否则调度每个心跳都会重试、白烧 AI 额度。
    # 想重跑走页面的「重新生成」（force=1）。
    save_user(block)
    return block


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
