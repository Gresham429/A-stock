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

logger = logging.getLogger(__name__)

_DISCLAIMER = ("你是严谨的A股投资研究助手。你的输出是「决策参考信号」，不是保证收益的建议；"
               "必须基于给定的客观数据说话，不得编造未提供的数字。语气客观，中文回答。")


class LLMError(RuntimeError):
    """DeepSeek 调用失败。"""


def _chat(messages: list[dict[str, str]], *, json_mode: bool = True,
          temperature: float = 0.3, max_tokens: int = 8000,
          timeout: int = 150) -> str:
    """调用 DeepSeek chat completions，返回助手文本。

    deepseek-v4-pro 是推理模型：max_tokens 同时覆盖「思考 + 正文」，
    留足余量（默认 8000），否则思考耗尽预算会导致正文被截断为空。
    """
    if not config.llm_enabled():
        raise LLMError("未配置 DeepSeek API key（检查 .env）")
    payload: dict[str, Any] = {
        "model": config.DEEPSEEK_MODEL,
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


def _watchlist_table(rows: list[dict[str, Any]]) -> str:
    """把自选股指标压成紧凑文本表喂给模型。"""
    lines = ["代码 名称 现价 涨跌% PE PB 年化波动% 20日涨% 区间位置% 主力5日亿 主力20日亿"]
    for r in rows:
        lines.append(" ".join([
            r.get("code", ""), r.get("name", ""),
            _fmt(r.get("price")), _fmt(r.get("chg_pct")),
            _fmt(r.get("pe_ttm")), _fmt(r.get("pb")),
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
                         web_context: str = "") -> dict[str, Any]:
    """每日推荐：对自选股给 buy/sell/hold/watch，并结合持仓给操作提示。"""
    prompt = f"""基于以下 A股自选股实时指标和我的持仓，给出今日操作参考。

【自选股指标】
{_watchlist_table(rows)}

【我的持仓】
{_holdings_table(holdings)}
{_web_block(web_context)}
分析要点：估值(PE/PB是否偏贵)、动能(20日涨幅)、位置(区间位置越接近100越过热、越接近0越低位)、
资金(主力净流入为正是流入)、波动(年化波动越高越刺激也越危险)。本金约1万元、偏好科技股与波动。

严格返回如下 JSON：
{{
  "market_view": "一句话概括当前该组合的整体情绪/风险",
  "picks": [
    {{"code":"", "name":"", "action":"buy|add|hold|reduce|sell|watch",
      "confidence":"high|mid|low",
      "reason":"结合上面数据的具体理由(40字内)",
      "risk":"这只最大的风险(20字内)"}}
  ],
  "holdings_note": "针对持仓的一句话提醒(无持仓则填 无)"
}}
picks 覆盖全部自选股，按操作优先级排序(该买/该卖的排前面)。"""
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
现价：{quote.get('price')}  当前盈亏：{holding.get('pnl_pct')}%  PE：{quote.get('pe_ttm')}  PB：{quote.get('pb')}

【波动与位置】年化波动：{metrics.get('vol')}%  20日涨幅：{metrics.get('cum20')}%
区间位置：{metrics.get('range_pos')}%(越接近100越过热)  主力20日净流入：{metrics.get('net20')}亿
近60日最高/最低/当前：{vh.get('hi')}/{vh.get('lo')}/{vh.get('cur')}  近20日振幅均值：{vh.get('atr_pct')}%

【财报(利润表)】
{_fin_table(financials or [])}

【近期新闻(公司/题材/政策面)】
{news_txt}
{_web_block(web_context)}
要求：结合基本面(营收/利润增速)、估值、技术位置(区间/波动/支撑压力)、资金、新闻/政策面综合判断。
严格返回 JSON：
{{
  "action":"hold|add|reduce|sell",
  "sell_trigger":"具体价位/条件应卖出",
  "add_trigger":"具体价位/条件可加仓",
  "stop_loss":"止损价位或跌幅",
  "take_profit":"止盈参考价位",
  "fundamental":"基本面一句话(结合财报增速与估值)",
  "policy_news":"新闻/政策面一句话(若新闻不足则说明)",
  "reason":"综合结论(80字内)"
}}"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=5000)
    return _parse_json(content)


def market_screen(rows: list[dict[str, Any]], capital: float,
                  focus_sector: str = "", web_context: str = "") -> dict[str, Any]:
    """从科技股候选池跨板块筛选（考虑资金规模与板块，侧重科技）。

    rows: 每项含 code/name/sector/price/pe_ttm/pb/vol/cum20/range_pos/net20/lot_cost。
    capital: 可用资金(元)，用于判断 1 手是否买得起。
    """
    focus = f"用户特别侧重【{focus_sector}】板块，请多给该板块机会。\n" if focus_sector else ""
    header = ("代码 名称 板块 现价 PE PB 年化波动% 20日涨% 区间位置% 主力20日亿 1手成本元")
    lines = [header]
    for r in rows:
        lines.append(" ".join([
            r.get("code", ""), r.get("name", ""), r.get("sector", ""),
            _fmt(r.get("price")), _fmt(r.get("pe_ttm")), _fmt(r.get("pb")),
            _fmt(r.get("vol")), _fmt(r.get("cum20")), _fmt(r.get("range_pos")),
            _fmt(r.get("net20")), _fmt(r.get("lot_cost")),
        ]))
    prompt = f"""从下面这批 A股科技股候选中，为我做一次跨板块筛选选股。

我的可用资金约 {int(capital)} 元（A股按1手=100股买入，1手成本必须≤资金才买得起）。
{focus}用户是计算机从业者，偏好科技板块与有波动的标的，但要控制风险、注重板块分散。

【候选池指标】
{chr(10).join(lines)}
{_web_block(web_context)}
要求：
1) 优先给 1手成本 ≤ {int(capital)} 元、买得起的标的；
2) 兼顾不同子板块(别集中在一个板块)，科技方向可多给；
3) 结合估值(PE别过高)、动能(20日涨)、位置(区间位置越接近100越追高危险)、资金(主力净流入为正更好)、波动(要有波动但非纯投机)；
4) 给出不同资金情况的建议(如满仓1手 vs 分散多只)。

严格返回 JSON：
{{
  "overall":"当前科技板块整体情绪/风险一句话",
  "picks":[
    {{"code":"","name":"","sector":"","action":"buy|watch",
      "confidence":"high|mid|low","lot_cost":0,
      "reason":"为何入选(结合数据,40字内)","risk":"主要风险(20字内)"}}
  ],
  "budget_plan":"1万左右资金的具体配置建议(买哪几只各1手/如何分散)",
  "sector_view":"各板块简评(半导体/AI算力/软件/消费电子等哪些强哪些弱)"
}}
picks 给 6~10 只，按吸引力排序，覆盖至少3个不同板块。"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=9000)
    return _parse_json(content)
