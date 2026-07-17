"""AI 提示词注入块（从 app.py 抽出，2026-07-16 拆分）。

**为什么独立成模块**：这 9 个「块」构造函数把 本金档/交易成本/历史教训/全球宏观/
画像/规则库·新闻·笔记·联网 拼成文本，前置注入给 5 个用户面 AI 与 agent 决策。
`_agent_blocks` 原先在 app.py，agent_loop 只能 `import app` 反向依赖。抽出后
agent_loop 直接 `import ai_blocks`，循环依赖进一步消除。

**只含纯文本拼装，不含 Flask/路由。** 教训作用域两层见 `_lesson_block`（个体 vs 舰队）。
"""
from __future__ import annotations

import logging
import sqlite3

import agent_store
import ai_cache
import config
import datasources as ds
import fees
import llm
import news_store
import notes_store
import portfolio
import profile_store
import rules_store
import template_store
import universe_store
import websearch

logger = logging.getLogger(__name__)


def _total_assets() -> tuple[float, int]:
    """(总资产, 持仓只数) = active 画像现金 + 全局真实持仓市值（腾讯实时价）。"""
    prof = profile_store.get_active() or {}
    cash = float(prof.get("cash") or 0)
    holds = portfolio.load()
    val = 0.0
    if holds:
        quotes = ds.tencent_quote([h["code"] for h in holds])
        for h in holds:
            price = (quotes.get(h["code"], {}) or {}).get("price") or 0
            val += float(price) * float(h.get("shares") or 0)
    return cash + val, len(holds)

def _tier_block() -> str:
    """当前画像的【本金玩法档】注入块（据总资产落档）。无画像返回空串。"""
    prof = profile_store.get_active()
    if not prof:
        return ""
    total, hn = _total_assets()
    return profile_store.block_for_ai(prof.get("cash") or 0, total, hn,
                                      prof.get("risk_pref", "均衡"))

def _record_basis_stats(basis: list[dict] | None) -> None:
    """把引用校验结果（✓/⚠）计入当前 system prompt 版本的统计。

    这是**唯一可信的提示词优化信号**：每次调用一个样本、后端权威校验(AI 编不了)、
    即时反馈、不受市场影响。收益率则相反——样本太少且被大盘 beta 污染，故不统计。
    """
    if not basis:
        return
    ok = sum(1 for b in basis for r in (b.get("refs") or []) if r.get("status") == "ok")
    bad = sum(1 for b in basis for r in (b.get("refs") or []) if r.get("status") == "bad")
    if ok or bad:
        template_store.record("system_disclaimer", llm._system_prompt()[1],
                              basis_ok=ok, basis_bad=bad)

def _lesson_block(agent_id: int | None = None) -> str:
    """【历史教训】注入块：把模拟盘上犯过的**真实错误**喂给 AI（四期闭环）。

    喂的是**事实统计**（「追高 4 次」），不是让 AI 改写提示词——与「用 5 个样本优化提示词」
    有本质区别：这里数事实，那里拟合参数。故不受 784 笔样本量死局限制。

    `agent_id=None`（用户面 5 个 AI）→ 全体舰队汇总；给定 → 只该 agent 自己的（个体记忆）。
    """
    try:
        txt = agent_store.for_ai(agent_id=agent_id)
        return txt + "\n\n" if txt else ""
    except (sqlite3.Error, OSError) as e:
        logger.warning("教训块生成失败: %s", e)
        return ""

def _stock_house_view(code: str) -> str:
    """【本台对本股的历史看法】(P2c)：模拟盘全体 agent 在这只票上出手过几次、当时理由、结果。

    用户面深挖/持仓用——把「本台之前怎么看这只票、后来对不对」喂给 AI（共享底座 journal，
    按 code 查）。只列买入决策；journal 空则不注入。事实统计，非规律。
    """
    try:
        rows = agent_store.journal_for_code(code, limit=6)
    except (sqlite3.Error, OSError) as e:
        logger.warning("个股 house-view 生成失败: %s", e)
        return ""
    if not rows:
        return ""
    lines = [f"【本台对 {code} 的历史看法（模拟盘全体 agent 在此票上的出手与结果，事实统计、非规律）】"]
    for r in reversed(rows):
        head = f"- {r['date']}：{r.get('summary') or '（未记理由）'}"
        if r.get("settled_at") and r.get("x20") is not None:
            p = r.get("x20_pctile")
            head += f" → 20日超额 {r['x20']:+.2f}%" + (f"（第 {p} 百分位）" if p is not None else "")
        else:
            head += " → 结果未定"
        lines.append(head)
    return "\n".join(lines) + "\n\n"

def _agent_blocks(ag: dict, cash: float, total: float, n_pos: int) -> str:
    """**每个 agent 自己的**注入块：档位按它自己的账户总资产、费率按它绑的画像。

    此前 `blocks` 由 `_agent_boot` 算一次传给所有 agent —— `_tier_block()` 读的是
    **active 画像 + 用户真实持仓**，于是 50 万的中型 agent 拿到的是用户 7000 块的
    微型档玩法，`_fee_block()` 的保本涨幅也按用户现金算。**多档位实验会完全失效**。

    拆法：**档位跟这个账户的钱走**（agent 的 paper 账户总资产），
    **费率跟券商走**（agent 绑的画像——都是同一个券商，故共用合理）。
    """
    prof = profile_store.get(ag.get("profile_id")) or {}
    tier = profile_store.block_for_ai(cash, total, n_pos, prof.get("risk_pref", "均衡"))
    try:
        sched = profile_store.fee_schedule(ag.get("profile_id"))
        fee = fees.for_ai(sched, cash or 10000.0) + "\n\n"
    except (sqlite3.Error, OSError, ValueError) as e:
        logger.warning("agent 费率块失败: %s", e)
        fee = ""
    # 教训**主看自己的**（个体记忆，多-agent 对照才干净）；**另加全舰队只读层**(P3)——
    # 市场真理(如「追高在弱势里普遍跑输」)该共享，且标签区分「你本账户」vs「全体账户」，
    # 不污染各自的行为统计（策略身份在档位/自视图里，不在这层，故不会收敛趋同）。
    return (tier + fee + _lesson_block(agent_id=ag.get("id"))
            + _lesson_block() + rules_store.for_ai())

def _fee_block() -> str:
    """【交易成本】注入块：给 AI 具体费率与保本涨幅，而非「注意手续费」这种空话。

    此前 5 个 AI 提示词 0 处提及手续费——AI 在给买卖建议时并不知道交易要花钱，
    会建议博取小于保本涨幅的价差（对万9费率，往返 0.232%，即涨不到 0.232% 就是亏）。
    费率取 active 画像（因券商/账户而异，见 fees.py）。
    """
    try:
        sched = profile_store.fee_schedule()
        prof = profile_store.get_active()
        capital = float((prof or {}).get("cash") or 0) or 10000.0
        return fees.for_ai(sched, capital) + "\n\n"
    except (sqlite3.Error, OSError, ValueError) as e:
        logger.warning("交易成本块生成失败: %s", e)
        return ""

def _macro_block() -> str:
    """全球宏观/地缘 digest 注入块（据全球快讯 flash 合成「要点+板块指向」，ai_cache kind=macro 当日缓存）。"""
    if not config.llm_enabled():
        return ""
    hit = ai_cache.get("macro", {})
    if hit:
        d = hit["result"].get("digest") or {}
    else:
        news = ds.eastmoney_global_news(30) + ds.cls_telegraph(30)
        markets = ds.global_markets()      # 外围数值：油价/黄金/铜/美股三大指数
        try:
            d = llm.macro_digest(news, markets)
        except llm.LLMError:
            return ""
        ai_cache.put("macro", {}, {"digest": d}, llm.FLASH_MODEL)
    pts = "；".join(d.get("points") or [])
    smap = "；".join(d.get("sector_map") or [])
    if not (pts or smap):
        return ""
    return (f"\n【全球宏观/地缘·对A股板块指向(每日更新)】\n"
            f"要点：{pts}\n板块指向：{smap}\n外围倾向：{d.get('bias', '')}\n")

def _profile_block(prof: dict | None) -> str:
    """公司叙事拼成注入 AI 主分析的文本块。"""
    if not prof:
        return ""
    tags = "、".join(prof.get("tags") or [])
    return (f"\n【公司叙事·据公开数据】做过：{prof.get('did', '')}；在做：{prof.get('doing', '')}；"
            f"要做：{prof.get('will', '')}" + (f"；题材：{tags}" if tags else "") + "\n")

def _ai_web_context(scope: str, code: str = "", name: str = "") -> str:
    """构建喂给 AI 的「联网知识」上下文：交易规则 + L2 本地资讯库 + A 实时快讯 + B 博查(可选)。"""
    parts = []
    # 交易分析框架规则（启用中的规则库，蒸馏自 PA_Agent）——置顶，AI 按此推理
    rules_txt = rules_store.for_ai()
    if rules_txt:
        parts.append("【交易分析框架规则（价格行为体系，分析时严格按此推理）】\n" + rules_txt)
    # L4：本地新闻库（带日期，让 AI 按新鲜度加权；个股取该股近期，大盘取市场级+政策）
    if scope == "position" and code:
        local = news_store.query(code=code, days=120, limit=8)
        if len(local) < 5:  # L3：本地稀疏 → 按需深抓更久历史再取
            try:
                news_store.deepen(code)
            except Exception as e:  # noqa: BLE001 兜底，不阻断 AI
                logger.warning("news 深抓 %s 失败: %s", code, e)
            local = news_store.query(code=code, days=365, limit=10)
    else:
        local = (news_store.query(sector="市场", days=30, limit=8)
                 + news_store.query(kind="政策", days=60, limit=6))
    if local:
        seen: set[str] = set()
        lines = []
        for n in local:
            t = n.get("title", "")
            if not t or t in seen:
                continue
            seen.add(t)
            lines.append(f"- {n.get('date','')} [{n.get('kind','')}] {t}")
        if lines:
            parts.append("本地资讯库（近期新闻/政策，越近权重越高；仅供判断勿编造）：\n"
                         + "\n".join(lines[:12]))
    # L5：私域笔记（我本人的判断，带时间戳供按新鲜度加权；须与客观数据区分、勿当事实）
    if scope == "position" and code:
        p, s = universe_store.sector_of(code)
        my_notes = notes_store.for_ai(code=code, sectors=[p, s], limit=5)
    else:
        my_notes = notes_store.list_notes(limit=5)
    if my_notes:
        nlines = [f"- {n.get('created_at','')[:10]} [{n.get('kind','')}] "
                  f"{n.get('ai_summary') or (n.get('content','') or '')[:60]}" for n in my_notes]
        parts.append("【我的私域笔记（我本人的判断，仅供参考、需与客观数据区分，勿当事实）】\n"
                     + "\n".join(nlines))
    # A：实时快讯
    news = ds.market_news_digest(12)
    if news:
        parts.append("最新财经/政策快讯：\n"
                     + "\n".join(f"- {n['time']} {n['title']}" for n in news))
    if config.bocha_enabled():
        if scope == "position" and name:
            query = f"{name} {code} 最新消息 政策 业绩 利好 利空"
        else:
            query = "A股 科技板块 半导体 AI 芯片 最新政策 行业动态"
        dig = websearch.search_digest(query, count=6)
        if dig:
            parts.append("联网搜索（博查）：\n" + dig)
    return "\n\n".join(parts)
