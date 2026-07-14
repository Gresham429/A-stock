"""DeepSeek 集成层：把看板真实数据喂给 deepseek-v4-pro，产出结构化投资参考。

- 直连 HTTP（OpenAI 兼容），零第三方 SDK 依赖。
- 所有输出均为「AI 参考信号」，非投资建议；提示词内已强制风险框架。
- 密钥来自 config（.env），源码不含 key。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import config
import provenance

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "你是严谨、理性、克制的A股投资研究助手。硬性要求："
    "① 只依据提示词中给定的客观数据推理，绝不编造、外推或引用未提供的数字与事实；"
    "② 数据缺失就明说「数据不足」，不要脑补；"
    "③ 只描述波动幅度与概率区间，不对涨跌方向下确定性断言；"
    "④ 结论需可被给定数据支撑，逻辑链清晰、口径一致；"
    "⑤ 所有输出均为「决策参考信号」，不是投资建议、不保证收益；"
    "⑥ 若提示词含【交易分析框架规则】，必须严格遵循，结论不得与之相悖（尤其禁止逆势/禁止追高潮/交易者方程）。"
    "语气客观冷静，中文回答，严格按要求的 JSON 结构返回。")


class LLMError(RuntimeError):
    """DeepSeek 调用失败。"""


def _chat(messages: list[dict[str, str]], *, json_mode: bool = True,
          temperature: float = 0.15, max_tokens: int = 8000,
          timeout: int = 150, model: str = "") -> str:
    """调用 DeepSeek chat completions，返回助手文本。

    deepseek-v4-pro 是推理模型：max_tokens 同时覆盖「思考 + 正文」，
    留足余量（默认 8000），否则思考耗尽预算会导致正文被截断为空。
    model 可覆盖（如笔记结构化用更快的 deepseek-v4-flash）。
    """
    if not config.llm_enabled():
        raise LLMError("未配置 DeepSeek API key（检查 .env）")
    payload: dict[str, Any] = {
        "model": model or config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        if not content.strip():
            fr = choice.get("finish_reason")
            raise LLMError(f"模型返回空正文（finish_reason={fr}）；"
                           "多为推理耗尽 max_tokens，请调大或稍后重试")
        return content
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        logger.error("DeepSeek HTTP %s: %s", e.code, body)
        raise LLMError(f"DeepSeek 返回 {e.code}：{body}") from e
    except (OSError, KeyError, ValueError) as e:
        logger.error("DeepSeek 调用失败: %s", e)
        raise LLMError(f"DeepSeek 调用失败：{e}") from e


def _parse_json(text: str) -> dict[str, Any]:
    """稳健解析 JSON（容忍模型偶尔包裹代码块）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _fmt(v: Any, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def _web_block(web_context: str) -> str:
    """近期新闻/联网搜索上下文块（供 AI 判断政策面与情绪；无则空）。"""
    if not web_context:
        return ""
    return ("\n【近期财经/政策快讯 + 联网搜索（据此判断政策面/情绪面，"
            "但仅作参考、勿编造）】\n" + web_context + "\n")


def _index_table(indices: list[dict[str, Any]]) -> str:
    """指数行情压成紧凑文本表。"""
    if not indices:
        return "（指数数据暂缺）"
    lines = ["指数 点位 涨跌% 成交额(亿)"]
    for i in indices:
        lines.append(" ".join([i.get("name", ""), _fmt(i.get("point")),
                               _fmt(i.get("chg_pct")), _fmt(i.get("amount_yi"))]))
    return "\n".join(lines)


def _breadth_line(breadth: dict[str, Any] | None) -> str:
    """市场情绪（涨跌家数/涨停跌停/行业冷热）压成一段文本。"""
    if not breadth:
        return "（市场情绪数据暂缺，请仅据指数与成交额判断）"
    parts = [
        f"涨/跌家数：{_fmt(breadth.get('advancers'))}/{_fmt(breadth.get('decliners'))}",
        f"涨停/跌停：{_fmt(breadth.get('limit_up'))}/{_fmt(breadth.get('limit_down'))}",
    ]
    top = breadth.get("top_industries") or []
    bot = breadth.get("bottom_industries") or []
    if top:
        parts.append("领涨行业：" + "、".join(
            f"{x.get('name','')}({_fmt(x.get('chg_pct'))}%)" for x in top))
    if bot:
        parts.append("领跌行业：" + "、".join(
            f"{x.get('name','')}({_fmt(x.get('chg_pct'))}%)" for x in bot))
    return "  ".join(parts)


def _market_ctx_block(market_ctx: dict[str, Any] | None) -> str:
    """把大盘研判结论压成上下文块，喂给选股 AI（无则空）。"""
    if not market_ctx:
        return ""
    return ("\n【当前大盘研判（据此决定进攻/防守强度；此为已给定结论，选股须与之一致）】\n"
            f"市场状态：{market_ctx.get('regime','—')}  "
            f"风格：{market_ctx.get('style','—')}  "
            f"赚钱效应：{market_ctx.get('sentiment','—')}\n"
            f"主要风险：{market_ctx.get('risk','—')}\n"
            f"选股指导：{market_ctx.get('guidance','—')}\n")


def market_overview(indices: list[dict[str, Any]], breadth: dict[str, Any] | None = None,
                    web_context: str = "") -> dict[str, Any]:
    """大盘局势研判：据指数 + 市场情绪判定市场状态、风格与攻防指导。"""
    prompt = f"""请对当前 A股大盘做一次严谨的局势研判。仅依据下列客观数据，不要编造。

【五大指数】
{_index_table(indices)}

【市场情绪】
{_breadth_line(breadth)}
{_web_block(web_context)}
研判要点（按此逻辑推理，口径一致）：
1) 指数强弱：几大指数涨跌是否一致、大盘(沪深300)与成长(创业板/科创50)谁强，看风格偏向；
2) 广度：涨跌家数对比反映普涨还是分化/结构行情；涨停数多、跌停数少=情绪偏暖，反之偏冷；
3) 量能：两市成交额是放量还是缩量（若无历史仅作绝对水平参考，别臆断趋势）；
4) 行业冷热：领涨/领跌行业揭示当前主线与回避方向；
5) 综合给出市场状态与「该进攻还是防守、仓位轻重」的明确指导。

严格返回如下 JSON（字段都要有，缺数据就写「数据不足」）：
{{
  "regime": "强势|中性偏多|中性|中性偏空|弱势|避险",
  "style": "大盘价值|小盘成长|均衡|防御 中择一并简述依据",
  "sentiment": "赚钱效应一句话(结合涨跌家数/涨停跌停)",
  "risk": "当前大盘最大风险点(一句话)",
  "guidance": "对选股的指导:进攻/均衡/防守 + 仓位与选股方向建议(40字内)",
  "one_liner": "一句话大盘研判(顶部条展示,含关键数字,30字内)"
}}"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=6000)
    return _parse_json(content)


def _watchlist_table(rows: list[dict[str, Any]]) -> str:
    """把自选股指标压成紧凑文本表喂给模型。"""
    lines = ["代码 名称 现价 涨跌% PE PB 换手% 年化波动% 20日涨% 区间位置% 主力5日亿 主力20日亿"]
    for r in rows:
        lines.append(" ".join([
            r.get("code", ""), r.get("name", ""),
            _fmt(r.get("price")), _fmt(r.get("chg_pct")),
            _fmt(r.get("pe_ttm")), _fmt(r.get("pb")), _fmt(r.get("turnover")),
            _fmt(r.get("vol")), _fmt(r.get("cum20")),
            _fmt(r.get("range_pos")), _fmt(r.get("net5")), _fmt(r.get("net20")),
        ]))
    return "\n".join(lines)


def _holdings_table(holdings: list[dict[str, Any]]) -> str:
    if not holdings:
        return "（当前无持仓）"
    lines = ["代码 名称 持股数 成本价 现价 盈亏%"]
    for h in holdings:
        lines.append(" ".join([
            h.get("code", ""), h.get("name", ""),
            _fmt(h.get("shares")), _fmt(h.get("cost_price")),
            _fmt(h.get("price")), _fmt(h.get("pnl_pct")),
        ]))
    return "\n".join(lines)


def daily_recommendation(rows: list[dict[str, Any]],
                         holdings: list[dict[str, Any]],
                         web_context: str = "",
                         news_map: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """每日推荐：对自选股给 buy/sell/hold/watch，并结合持仓给操作提示。"""
    nb = ""
    if news_map:
        rows_n = [f"{c}: " + " / ".join(t[:3]) for c, t in news_map.items() if t]
        if rows_n:
            nb = "\n【个股近期动态(本地新闻库,给一句话公司叙事用)】\n" + "\n".join(rows_n) + "\n"
    prompt = f"""基于以下 A股自选股实时指标和我的持仓，给出今日操作参考。

【自选股指标】
{_watchlist_table(rows)}

【我的持仓】
{_holdings_table(holdings)}
{nb}{_web_block(web_context)}
分析要点：估值(PE/PB是否偏贵)、动能(20日涨幅)、位置(区间位置越接近100越过热、越接近0越低位)、
资金(主力净流入为正是流入)、波动(年化波动越高越刺激也越危险)。本金约1万元、偏好科技股与波动。
**严格遵循上方【交易分析框架规则】**（若有）。

⚠️ 这是**组合级速览**（只喂了紧凑指标，没有财报/波动史/个股新闻/你的成本）：
- **对持仓中的股票**：只做粗筛，倾向 hold/watch，**不要仅凭浅层指标下硬 sell/reduce**；若觉得该重新评估，标 action=watch 并在 reason 里写「建议用『何时卖』深看」——持仓的买卖最终以单只深度分析(position)为准。
- 非持仓的自选股：可给 buy/watch 倾向。

严格返回如下 JSON：
{{
  "market_view": "一句话概括当前该组合的整体情绪/风险",
  "picks": [
    {{"code":"", "name":"", "action":"buy|add|hold|watch|reduce|sell",
      "held": true/false,
      "confidence":"high|mid|low",
      "reason":"结合上面数据的具体理由(40字内)；持仓需评估时写『建议用何时卖深看』",
      "risk":"这只最大的风险(20字内)",
      "narrative":"这公司近期在做什么/什么题材(一句话,20字内;本地动态无信息则留空串)"}}
  ],
  "holdings_note": "针对持仓的一句话提醒(无持仓则填 无)"
}}
picks 覆盖全部自选股，按关注优先级排序。**持仓股 action 默认 hold/watch，除非规则明确触发。**"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=8000)
    return _parse_json(content)


def _fin_table(financials: list[dict[str, Any]]) -> str:
    if not financials:
        return "（暂无财报数据）"
    lines = ["报告期 营收(亿) 营收同比% 归母净利(亿) 净利同比%"]
    for f in financials:
        lines.append(" ".join([f.get("period", ""), _fmt(f.get("revenue_yi")),
                               _fmt(f.get("revenue_yoy")), _fmt(f.get("profit_yi")),
                               _fmt(f.get("profit_yoy"))]))
    return "\n".join(lines)


def company_profile(name: str, code: str,
                    news: list[dict[str, Any]] | None = None,
                    financials: list[dict[str, Any]] | None = None,
                    announcements: list[dict[str, Any]] | None = None,
                    concepts: list[str] | None = None) -> dict[str, Any]:
    """公司叙事三段（做过/在做/要做）+ 题材标签，用 flash 快模型合成。只据给定数据、不编造。"""
    news_txt = "\n".join(f"- {n.get('date','')[:10]} {n.get('title','')}"
                         for n in (news or [])[:8]) or "（无）"
    ann_txt = "\n".join(f"- {a.get('date','')} {a.get('title','')}"
                        for a in (announcements or [])[:8]) or "（无）"
    concepts_txt = "、".join(concepts or []) or "（无）"
    prompt = f"""根据下列客观数据，为 A股【{name} {code}】写一段极简"公司叙事"。只据给定信息、不编造、不预测股价。
【所属板块/概念】{concepts_txt}
【财报(利润表,多期)】
{_fin_table(financials or [])}
【近期新闻】
{news_txt}
【近期公告】
{ann_txt}
严格返回 JSON：
{{
  "did":"做过什么：主营业务 + 历史/沉淀(一句,30字内)",
  "doing":"在做什么：近期动作/公告/新闻反映的当前重心(一句,30字内)",
  "will":"要做什么：规划/在建/题材催化(一句,30字内；无明确公开信息写『暂无明确公开规划』)",
  "tags":["方向/题材标签,3~6个,来自所属板块或新闻"]
}}"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}],
                    model=FLASH_MODEL, max_tokens=2500)
    return _parse_json(content)


def position_advice(holding: dict[str, Any], quote: dict[str, Any],
                    metrics: dict[str, Any], financials: list[dict[str, Any]] | None = None,
                    news: list[dict[str, Any]] | None = None,
                    vol_hist: dict[str, Any] | None = None,
                    web_context: str = "") -> dict[str, Any]:
    """单只持仓的卖出/买入时机建议（严格结合波动史 + 财报 + 近期新闻 + 联网搜索）。"""
    news_txt = "\n".join(f"- {n.get('date','')[:10]} {n.get('title','')}"
                         for n in (news or [])[:6]) or "（暂无近期新闻）"
    vh = vol_hist or {}
    prompt = f"""我持有下面这只股票，请**严格结合**下列客观数据给出卖出/加仓/止损时机参考。

【标的】{quote.get('name','')} {holding.get('code','')}
持股数：{holding.get('shares')}  成本价：{holding.get('cost_price')}  买入日：{holding.get('buy_date','')}
现价：{quote.get('price')}  当前盈亏：{holding.get('pnl_pct')}%  PE：{quote.get('pe_ttm')}  PB：{quote.get('pb')}  换手率：{quote.get('turnover')}%(交易活跃度)

【波动与位置】年化波动：{metrics.get('vol')}%  20日涨幅：{metrics.get('cum20')}%
区间位置：{metrics.get('range_pos')}%(越接近100越过热)  主力20日净流入：{metrics.get('net20')}亿
近60日最高/最低/当前：{vh.get('hi')}/{vh.get('lo')}/{vh.get('cur')}  近20日振幅均值：{vh.get('atr_pct')}%

【财报(利润表)】
{_fin_table(financials or [])}

【近期新闻(公司/题材/政策面)】
{news_txt}
{_web_block(web_context)}
要求：结合基本面(营收/利润增速)、估值、技术位置(区间/波动/支撑压力)、资金、新闻/政策面综合判断。
**严格遵循上方【交易分析框架规则】**（若有）——先判市场状态、只顺势、结构止损、交易者方程；结论不得与规则相悖。
这是**该持仓的权威判断**（比组合速览『自选推荐』更深、更可信）。
【可引用信号】basis 里 signals 只能从这些名字选（**不要写数值，值由系统填**）：{provenance.signal_vocab(False)}
规则已以 [R编号] 注入上方框架规则；basis 里 rules 用 R+编号(如 R12)。每条关键结论(action/sell_trigger/add_trigger/stop_loss/take_profit)都给依据，没把握就不列该条。
严格返回 JSON：
{{
  "action":"hold|add|reduce|sell",
  "sell_trigger":"具体价位/条件应卖出",
  "add_trigger":"具体价位/条件可加仓",
  "stop_loss":"止损价位或跌幅",
  "take_profit":"止盈参考价位",
  "hold_horizon":"建议持有周期(如『持有2~6周看季报兑现』/『破位即走』)，结合你的周期偏好",
  "rule_basis":"本次决策命中的框架规则(如『交易者方程/顺势/结构止损』，20字内)",
  "basis":[{{"claim":"action","signals":["区间位置"],"rules":["R12"]}},{{"claim":"stop_loss","signals":["60日低"],"rules":["R7"]}}],
  "fundamental":"基本面一句话(结合财报增速与估值)",
  "policy_news":"新闻/政策面一句话(若新闻不足则说明)",
  "reason":"综合结论(80字内)"
}}"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=6000)
    return _parse_json(content)


def entry_advice(code: str, name: str, quote: dict[str, Any], metrics: dict[str, Any],
                 financials: list[dict[str, Any]] | None = None,
                 news: list[dict[str, Any]] | None = None,
                 vol_hist: dict[str, Any] | None = None, capital: float = 0,
                 market_ctx: dict[str, Any] | None = None,
                 web_context: str = "") -> dict[str, Any]:
    """单股深度入场分析：是否入场/何时/怎么买 + 未来卖出策略预判（不必持仓）。"""
    news_txt = "\n".join(f"- {n.get('date','')[:10]} {n.get('title','')}"
                         for n in (news or [])[:6]) or "（暂无近期新闻）"
    vh = vol_hist or {}
    prompt = f"""请对下面这只股票做一次**深度入场分析**：我目前不一定持有，想判断是否值得买、何时买、怎么买，并**预判未来的卖出策略**。
{_market_ctx_block(market_ctx)}
【标的】{name} {code}   可用资金约 {int(capital)} 元（A股 1手=100股，1手成本须 ≤ 资金）
现价：{quote.get('price')}  PE：{quote.get('pe_ttm')}  PB：{quote.get('pb')}  换手率：{quote.get('turnover')}%(交易活跃度)  1手成本：{quote.get('lot_cost')}元
【波动与位置】年化波动：{metrics.get('vol')}%  20日涨：{metrics.get('cum20')}%  区间位置：{metrics.get('range_pos')}%(越接近100越过热)  主力20日净流入：{metrics.get('net20')}亿
近60日最高/最低/当前：{vh.get('hi')}/{vh.get('lo')}/{vh.get('cur')}  近20日振幅均值：{vh.get('atr_pct')}%
【财报(利润表)】
{_fin_table(financials or [])}
【近期新闻(公司/题材/政策面)】
{news_txt}
{_web_block(web_context)}
**严格遵循上方【交易分析框架规则】**：先判大盘与该股所处周期(趋势/通道/区间)，只顺势、按交易者方程决定是否值得，结构止损、二次入场优先、不追高潮。
【可引用信号】basis 里 signals 只能从这些名字选（**不要写数值，值由系统填**）：{provenance.signal_vocab(True)}
规则已以 [R编号] 注入上方框架规则；basis 里 rules 用 R+编号(如 R12)。每条关键结论(verdict/entry_when/entry_zone/entry_how/stop_loss/targets/future_sell_plan)都给依据，没把握就不列该条。
严格返回 JSON：
{{
  "verdict":"值得入场|观望等待|不建议",
  "confidence":"high|mid|low",
  "market_fit":"与当前大盘/该股周期状态是否契合(一句话)",
  "entry_when":"何时入场(具体条件,如『回踩EMA20出现H2』/『突破测试不破前高后』)",
  "entry_zone":"建议买入价位区间",
  "entry_how":"怎么买(结合资金:一次/分批/挂限价;大约能买几手)",
  "stop_loss":"止损价位或条件",
  "targets":"止盈/目标位(TP1保守 / TP2结构远目标)",
  "hold_horizon":"预计持有周期",
  "future_sell_plan":"未来卖出策略预判(什么情况分批止盈/什么情况破位止损/什么信号清仓)",
  "risks":"主要风险(20字内)",
  "rule_basis":"命中的框架规则(20字内)",
  "basis":[{{"claim":"verdict","signals":["区间位置","大盘研判"],"rules":["R12"]}},{{"claim":"stop_loss","signals":["60日低"],"rules":["R7"]}}],
  "reason":"综合结论(100字内)"
}}
若 verdict 非『值得入场』，entry_* 写触发条件而非具体价。"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=7000)
    return _parse_json(content)


def market_screen(rows: list[dict[str, Any]], capital: float,
                  focus_sector: str = "", market_ctx: dict[str, Any] | None = None,
                  web_context: str = "") -> dict[str, Any]:
    """从全市场两级候选池跨板块筛选（结合大盘 + 资金规模 + 侧重板块）。

    rows: 每项含 code/name/primary/sub/price/pe_ttm/pb/vol/cum20/range_pos/net20/lot_cost。
    capital: 可用资金(元)，用于判断 1 手是否买得起。
    focus_sector: 一级板块名 / 二级细分名 / 空(全市场)。
    market_ctx: market_overview() 的结论，用于让选股与大盘状态一致。
    """
    focus = (f"用户本次特别侧重【{focus_sector}】方向，请优先在该方向内选，并说明其相对强弱。\n"
             if focus_sector else "本次为全市场筛选，请跨一级板块均衡挑选、避免集中单一方向。\n")
    header = "代码 名称 一级板块 二级细分 现价 PE PB 年化波动% 20日涨% 区间位置% 主力20日亿 1手成本元"
    lines = [header]
    for r in rows:
        lines.append(" ".join([
            r.get("code", ""), r.get("name", ""),
            r.get("primary", ""), r.get("sub", ""),
            _fmt(r.get("price")), _fmt(r.get("pe_ttm")), _fmt(r.get("pb")),
            _fmt(r.get("vol")), _fmt(r.get("cum20")), _fmt(r.get("range_pos")),
            _fmt(r.get("net20")), _fmt(r.get("lot_cost")),
        ]))
    prompt = f"""从下面这批 A股全市场候选中，结合当前大盘做一次严谨的跨板块筛选选股。
{_market_ctx_block(market_ctx)}
我的可用资金约 {int(capital)} 元（A股按1手=100股买入，1手成本必须≤资金才买得起）。
{focus}用户是科技从业者、本金偏小、能承受波动，但要求理性控风险、注重板块分散。

【候选池指标（一份紧凑表，每列含义见下）】
{chr(10).join(lines)}
{_web_block(web_context)}
指标口径（据此打分，不要臆造表外数据）：
- PE/PB：估值高低，越高越贵、越需要业绩支撑；负 PE=亏损，谨慎。
- 年化波动%：波动弹性，高=机会与风险同时放大。
- 20日涨%：近月动能；过高警惕追高。
- 区间位置%：当前价在近段高低区间的位置，越接近100越接近高位/过热，越接近0越低位。
- 主力20日亿：近20日主力资金净流入(亿)，正=净流入。

筛选规则（按顺序权衡）：
1) 硬约束：只选 1手成本 ≤ {int(capital)} 元、买得起的标的；
2) 与大盘一致：{('大盘偏弱/防守时优先低估值、低位、资金净流入、波动适中；'
   '大盘偏强/进攻时可适度提高波动与动能权重。') if market_ctx else '（无大盘结论时按均衡口径处理。）'}
3) 分散：覆盖多个一级板块与二级细分，别扎堆单一方向；
4) 质量：综合估值(别过高)、动能、位置(避免高位追接)、资金(净流入更优)、波动(要有弹性但非纯投机)；
5) 给出不同资金分配方式(满仓单只 vs 分散多只)的取舍。

严格返回 JSON（缺数据的字段写「数据不足」，不要编造）：
{{
  "overall":"结合大盘的整体研判一句话(点明当前该进攻还是防守)",
  "market_regime":"回显你采用的大盘状态(无则填 未提供)",
  "picks":[
    {{"code":"","name":"","primary":"一级板块","sub":"二级细分",
      "action":"buy|watch","confidence":"high|mid|low","lot_cost":0,
      "reason":"为何入选(结合上表数据与大盘,40字内)","risk":"主要风险(20字内)"}}
  ],
  "budget_plan":"{int(capital)}元的具体配置建议(买哪几只各1手/如何分散/留多少现金)",
  "sector_view":"按二级细分点评哪些方向强/弱、当前更该配哪类(结合大盘风格)"
}}
picks 给 6~10 只，按吸引力排序，覆盖至少 3 个不同一级板块。"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=9000)
    return _parse_json(content)


FLASH_MODEL = "deepseek-v4-flash"  # 轻量抽取任务用快模型（笔记结构化），别占用慢的 v4-pro


def structure_note(content: str) -> dict[str, Any]:
    """L5：把一段自由笔记结构化（快模型），供检索与喂给 AI。返回 summary/codes/sectors/tags/kind。"""
    prompt = f"""把下面这段我的投资笔记结构化，便于以后检索与喂给 AI 参考。只输出 JSON、不编造原文没有的信息。

【笔记原文】
{content}

要求：
- summary：一句话摘要（30 字内）
- codes：涉及的股票代码数组（6 位数字；没有则空数组）
- sectors：涉及的板块/行业名数组（没有则空数组）
- tags：2~4 个标签（如 观点/仓位/风险/催化/买点/卖点/复盘 等）
- kind：观点|事实|操作|研究 中择一

严格返回 JSON：
{{"summary":"","codes":[],"sectors":[],"tags":[],"kind":""}}"""
    out = _chat([{"role": "system", "content": "你是把投资笔记结构化的助手，只按要求输出 JSON。"},
                 {"role": "user", "content": prompt}],
                model=FLASH_MODEL, temperature=0.1, max_tokens=1200, timeout=60)
    return _parse_json(out)
