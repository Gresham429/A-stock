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
                         holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """每日推荐：对自选股给 buy/sell/hold/watch，并结合持仓给操作提示。"""
    prompt = f"""基于以下 A股自选股实时指标和我的持仓，给出今日操作参考。

【自选股指标】
{_watchlist_table(rows)}

【我的持仓】
{_holdings_table(holdings)}

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


def position_advice(holding: dict[str, Any], quote: dict[str, Any],
                    metrics: dict[str, Any]) -> dict[str, Any]:
    """单只持仓的卖出/买入时机建议。"""
    prompt = f"""我持有下面这只股票，请给出明确的卖出/加仓/止损时机参考。

股票：{quote.get('name','')} {holding.get('code','')}
持股数：{holding.get('shares')}  成本价：{holding.get('cost_price')}  买入日：{holding.get('buy_date','')}
现价：{quote.get('price')}  当前盈亏：{holding.get('pnl_pct')}%
PE：{quote.get('pe_ttm')}  PB：{quote.get('pb')}
年化波动：{metrics.get('vol')}%  20日涨幅：{metrics.get('cum20')}%
区间位置：{metrics.get('range_pos')}%(越接近100越过热)  主力20日净流入：{metrics.get('net20')}亿

严格返回 JSON：
{{
  "action":"hold|add|reduce|sell",
  "sell_trigger":"什么条件/价位应卖出(具体)",
  "add_trigger":"什么条件/价位可加仓(具体)",
  "stop_loss":"建议止损价位或跌幅",
  "take_profit":"建议止盈参考",
  "reason":"综合判断理由(60字内)"
}}"""
    content = _chat([{"role": "system", "content": _DISCLAIMER},
                     {"role": "user", "content": prompt}], max_tokens=4000)
    return _parse_json(content)
