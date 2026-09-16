"""三周期选股与自选股买卖点的提示词，复用 llm._chat。返回按条校验，不整批作废。"""
from __future__ import annotations

import json
import logging
from typing import Any

import llm
import picks_levels
import picks_store

logger = logging.getLogger(__name__)

HORIZON_CN = {"short": "短线（1 到 5 个交易日，交易活跃、低吸高抛，给具体价位）",
              "mid": "中线（2 到 8 周，板块轮动、趋势刚起，价位区间加趋势破坏即离场）",
              "long": "长线（3 到 12 个月，基本面与估值，分批建仓区间）"}

_RULES = """改口规则（必须遵守）：对已有观点的股票，decision 只能是 keep（维持）/ revise（修改）/ withdraw（撤销）；
revise 和 withdraw 必须给 trigger，且只能是 stop_hit（止损触发）/ target_hit（目标达成）/ expired（周期到期）/
thesis_broken（新信息推翻），并在 trigger_note 写明依据；没有触发事件就只能 keep。没有历史观点的股票 decision=new。
价位只能从候选价位里选（可取两个候选之间构成区间），不要编造。"""

_SCHEMA = """严格返回 JSON：
{"calls":[{"code":"","name":"","horizon":"short|mid|long","stance":"buy|watch|avoid|sell",
  "entry":[低,高],"exit":[低,高],"stop":价,
  "decision":"new|keep|revise|withdraw","trigger":"none|stop_hit|target_hit|expired|thesis_broken",
  "thesis":"理由(60字内)","trigger_note":"什么情况会改口(30字内)",
  "basis":{"signals":["区间位置"],"rules":["R12"]}}]}"""


def _rows_table(rows: list[dict[str, Any]]) -> str:
    head = "代码 名称 一级 二级 现价 PE PB 年化波动% 20日涨% 区间位置% 主力20日亿 换手% 1手成本"
    lines = [head]
    for r in rows:
        lines.append(" ".join(str(r.get(k, "")) for k in ("code", "name", "primary", "sub", "price", "pe_ttm", "pb",
                                                          "vol", "cum20", "range_pos", "net20", "turnover", "lot_cost")))
    return "\n".join(lines)


def _levels_block(rows: list[dict[str, Any]], levels: dict[str, dict], memory: dict[str, str]) -> str:
    parts = []
    for r in rows:
        c = r["code"]
        parts.append(f"[{c} {r.get('name','')}] {picks_levels.fmt_levels(levels.get(c) or {})}\n{memory.get(c) or '（该股尚无历史观点）'}")
    return "\n".join(parts)


def validate_calls(parsed: dict[str, Any], levels: dict[str, dict], allowed: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in (parsed or {}).get("calls") or []:
        code = str(c.get("code", "")).strip()
        if code not in allowed:
            logger.info("picks: 丢弃不在候选内的 %s", code)
            continue
        lv = (levels.get(code) or {}).get("levels") or []
        adjusted = False

        def _s(v: Any) -> float | None:
            nonlocal adjusted
            try:
                px = float(v)
            except (TypeError, ValueError):
                return None
            px2, adj = picks_levels.snap(px, lv)
            adjusted = adjusted or adj
            return px2

        entry = c.get("entry") or [None, None]
        exit_ = c.get("exit") or [None, None]
        basis = c.get("basis") if isinstance(c.get("basis"), dict) else {}
        row = {
            "code": code, "name": c.get("name", ""),
            "horizon": c.get("horizon") if c.get("horizon") in picks_store.HORIZON_DAYS else "short",
            "stance": c.get("stance") if c.get("stance") in ("buy", "watch", "avoid", "sell") else "watch",
            "entry_lo": _s(entry[0]), "entry_hi": _s(entry[1] if len(entry) > 1 else entry[0]),
            "exit_lo": _s(exit_[0]), "exit_hi": _s(exit_[1] if len(exit_) > 1 else exit_[0]),
            "stop": _s(c.get("stop")),
            "decision": c.get("decision") if c.get("decision") in picks_store.DECISIONS else "new",
            "trigger": c.get("trigger") if c.get("trigger") in picks_store.TRIGGERS else "none",
            "thesis": str(c.get("thesis", ""))[:200], "trigger_note": str(c.get("trigger_note", ""))[:100],
            "px_at_call": (levels.get(code) or {}).get("price"),
        }
        basis["adjusted"] = adjusted
        row["basis_json"] = json.dumps(basis, ensure_ascii=False)
        out.append(row)
    return out


def _ask(prompt: str, max_tokens: int) -> dict[str, Any]:
    try:
        text = llm._chat([{"role": "system", "content": llm._system_prompt()[0]},
                          {"role": "user", "content": prompt}], max_tokens=max_tokens)
        return llm._parse_json(text)
    except (llm.LLMError, ValueError) as e:
        logger.warning("picks: 模型调用或解析失败: %s", e)
        return {}


def horizon_picks(horizon: str, candidates: list[dict[str, Any]], levels: dict[str, dict],
                  memory: dict[str, str], market_ctx: dict[str, Any] | None, capital: float) -> list[dict[str, Any]]:
    prompt = f"""请在下面的候选里为【{HORIZON_CN[horizon]}】选出最值得关注的 5 只，并给每只买点区间、卖点区间、止损。
{llm._market_ctx_block(market_ctx)}
可用资金约 {int(capital)} 元。
【候选】
{_rows_table(candidates)}
【每只的候选价位与历史观点】
{_levels_block(candidates, levels, memory)}
{_RULES}
{_SCHEMA}
calls 恰好 5 条，horizon 填 {horizon}。"""
    parsed = _ask(prompt, 9000)
    out = validate_calls(parsed, levels, {r["code"] for r in candidates})
    for r in out:
        r["horizon"] = horizon
    return out[:5]


def watchlist_points(rows: list[dict[str, Any]], levels: dict[str, dict], memory: dict[str, str],
                     market_ctx: dict[str, Any] | None, capital: float) -> list[dict[str, Any]]:
    prompt = f"""下面是我的自选股。对每一只：判断它现在最适合哪个周期（short/mid/long），给出该周期下的买点区间、卖点区间、止损与立场；
有历史观点的先核对历史再决定 keep / revise / withdraw。
{llm._market_ctx_block(market_ctx)}
可用资金约 {int(capital)} 元。周期定义：短线 1 到 5 个交易日；中线 2 到 8 周；长线 3 到 12 个月。
【自选股】
{_rows_table(rows)}
【每只的候选价位与历史观点】
{_levels_block(rows, levels, memory)}
{_RULES}
{_SCHEMA}
calls 覆盖全部自选股，每只一条。"""
    parsed = _ask(prompt, 9000)
    return validate_calls(parsed, levels, {r["code"] for r in rows})
