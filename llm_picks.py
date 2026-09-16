"""三周期选股与自选股买卖点的提示词，复用 llm._chat。返回按条校验，不整批作废。"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

import llm
import picks_levels
import picks_store

logger = logging.getLogger(__name__)

# 推理模型的 max_tokens 同时覆盖思考与正文；30 只候选加候选价位与记忆块的提示词较长，
# 9000 实测被思考耗尽（finish_reason=length、正文为空）。
PICKS_MAX_TOKENS = 20000

# _ask 的失败原因用线程局部存储：公共三周期与各账号自选股可能在不同线程里并发跑
# （picks_pipeline.tick 逐用户 for 循环、picks_routes._spawn 的后台线程），模块级
# 全局变量会被并发的另一路运行覆盖，读到的可能是别人那次的错误。
_tls = threading.local()


def set_last_error(msg: str) -> None:
    _tls.value = msg


def last_error() -> str:
    return getattr(_tls, "value", "")

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


def _range(v: Any) -> list[Any]:
    """把模型给的区间归一化成两元素列表：标量或单元素列表补成 [x, x]，其余非法形态置 [None, None]。"""
    if isinstance(v, (int, float)):
        return [v, v]
    if isinstance(v, list):
        if len(v) >= 2:
            return v
        if len(v) == 1:
            return [v[0], v[0]]
        return [None, None]
    return [None, None]


def validate_calls(parsed: dict[str, Any], levels: dict[str, dict], allowed: set[str],
                   names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in (parsed or {}).get("calls") or []:
        if not isinstance(c, dict):
            logger.info("picks: 丢弃非字典项 %r", c)
            continue
        code = str(c.get("code", "")).strip()
        try:
            if code not in allowed:
                logger.info("picks: 丢弃不在候选内的 %s", code)
                continue
            if code in seen:
                logger.info("picks: 一次返回里 %s 重复，只留第一条", code)
                continue
            seen.add(code)
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

            entry = _range(c.get("entry"))
            exit_ = _range(c.get("exit"))
            basis = c.get("basis") if isinstance(c.get("basis"), dict) else {}
            row = {
                "code": code, "name": (names or {}).get(code) or c.get("name", ""),
                "horizon": c.get("horizon") if c.get("horizon") in picks_store.HORIZON_DAYS else "short",
                "stance": c.get("stance") if c.get("stance") in ("buy", "watch", "avoid", "sell") else "watch",
                "entry_lo": _s(entry[0]), "entry_hi": _s(entry[1]),
                "exit_lo": _s(exit_[0]), "exit_hi": _s(exit_[1]),
                "stop": _s(c.get("stop")),
                "decision": c.get("decision") if c.get("decision") in picks_store.DECISIONS else "new",
                "trigger": c.get("trigger") if c.get("trigger") in picks_store.TRIGGERS else "none",
                "thesis": str(c.get("thesis", ""))[:200], "trigger_note": str(c.get("trigger_note", ""))[:100],
                "px_at_call": (levels.get(code) or {}).get("price"),
            }
            basis["adjusted"] = adjusted
            row["basis_json"] = json.dumps(basis, ensure_ascii=False)
            out.append(row)
        except (TypeError, KeyError, AttributeError, ValueError) as e:
            logger.warning("picks: 丢弃格式不合规的一条返回 %s: %s", code, e)
            continue
    return out


def _ask(prompt: str, max_tokens: int) -> dict[str, Any]:
    try:
        text = llm._chat([{"role": "system", "content": llm._system_prompt()[0]},
                          {"role": "user", "content": prompt}], max_tokens=max_tokens)
        parsed = llm._parse_json(text)
        set_last_error("")
        return parsed
    except (llm.LLMError, ValueError) as e:
        logger.warning("picks: 模型调用或解析失败: %s", e)
        set_last_error(str(e))
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
    parsed = _ask(prompt, PICKS_MAX_TOKENS)
    names = {r["code"]: r["name"] for r in candidates}
    out = validate_calls(parsed, levels, {r["code"] for r in candidates}, names)
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
    parsed = _ask(prompt, PICKS_MAX_TOKENS)
    names = {r["code"]: r["name"] for r in rows}
    return validate_calls(parsed, levels, {r["code"] for r in rows}, names)
