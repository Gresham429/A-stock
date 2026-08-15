"""复盘 AI 层：DeepSeek 复盘裁判（结构化研判）+ 可发布文稿。

复用 A-stock 现成 llm._chat（DeepSeek v4-pro / v4-flash，urllib，OpenAI 兼容）。
- 硬边界焊进 prompt：只到板块层面、不荐个股、不给买卖点/时机、不预测涨跌。
- 全程可降级：未配 key 或调用失败 → 返回 None，pipeline 保留硬指标照常出。
- 裁判走 pro（推理、结构化），文稿走 pro（长文）；温度 0.15 求严谨。

（多分析师 5 角色 fan-out 是二期增强；MVP 由裁判直接读全部硬指标收敛。）
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import config
import llm

logger = logging.getLogger(__name__)

_PHASES = ("冰点", "修复", "发酵", "亢奋", "退潮")

_BOUNDARY = (
    "严格边界：这是市场层面的『复盘』（把今天发生的事实整理清楚），不是荐股。"
    "只做到板块层面，不推荐任何个股、不给买卖点位、不给参与度、不预测涨跌方向。"
    "个股只作客观陈述。只依据给定数据，不编造、不引入外部信息。"
)


def _pct(v: Any) -> str:
    return "—" if v is None else f"{v}%"


def _metrics_text(m: dict, counts: dict, date: str) -> str:
    """把硬指标压成紧凑文本喂 AI。"""
    b = m.get("breadth", {})
    ld = m.get("ladder", {})
    me = m.get("money_effect", {})
    pr = m.get("promotion", {})
    cp = m.get("consec_premium", {})
    le = m.get("loss_effect", {})
    sq = m.get("seal_quality", {})
    tt = m.get("theme_tree", {})
    cy = m.get("cycle_position", {})
    lines = [
        f"复盘日：{date}（已收盘定稿）",
        f"宽度/温度：涨停 {b.get('zt_count')} · 炸板 {b.get('zb_count')}"
        f"（炸板率 {_pct(b.get('break_rate'))}）· 跌停 {b.get('dt_count')}"
        f" · 最高 {b.get('max_height')} 连板 · 温度[{b.get('temp_tag')}]",
        f"连板梯队：{ld.get('tiers')} · 最高 {ld.get('highest')} 板"
        + (f" · ⚠断层缺档 {ld.get('gaps')}（最高标悬空、断板后无下一梯队承接）"
           if ld.get('gaps') else " · 梯队连续"),
        f"赚钱效应（昨涨停股今日 {me.get('n')} 只）：中位数 {_pct(me.get('median'))}"
        f" · 均值 {_pct(me.get('avg'))} · 翻红率 {_pct(me.get('red_rate'))}"
        f" · 再涨停率 {_pct(me.get('again_rate'))}（看中位数，均值易被大涨拉偏）",
        f"晋级率：1进2 {_pct(pr.get('one_to_two', {}).get('rate'))}"
        f"（{pr.get('one_to_two', {}).get('promoted')}/{pr.get('one_to_two', {}).get('base')}）"
        f" · 2进3 {_pct(pr.get('two_to_three', {}).get('rate'))}"
        f" · 3板+ {_pct(pr.get('three_plus', {}).get('rate'))}"
        f" · 总体 {_pct(pr.get('overall_rate'))}（1进2 最敏感）",
        f"连板溢价（昨≥2板今 {cp.get('n')} 只）：中位数 {_pct(cp.get('median'))}"
        f" · 翻红率 {_pct(cp.get('red_rate'))}",
        f"亏钱效应：昨涨停今跌超5% {le.get('deep5')} 只（{_pct(le.get('deep5_rate'))}）"
        f" · 跌停 {le.get('limit_down')} 只 · 最惨 {_pct(le.get('worst'))}"
        f" · 全市场跌停 {le.get('market_limit_down')} 只",
        f"封板质量：从未开板率 {_pct(sq.get('never_broken_rate'))}"
        f" · 早盘封板 {sq.get('opening')} 只 · 尾盘封板 {sq.get('late')} 只"
        f" · 平均炸板 {sq.get('avg_broken_times')} 次",
    ]
    top = tt.get("top") or []
    if top:
        lines.append("题材热点（按涨停家数）："
                     + "、".join(f"{d['theme']}({d['count']})" for d in top[:8]))
    if cy.get("available"):
        lines.append(f"情绪周期：本轮低点 {cy.get('trough_date')} 起、当前第 {cy.get('day_n')} 天"
                     f"、{'回升中' if cy.get('rising') else '仍在走弱'}")
    else:
        lines.append("情绪周期：历史样本不足，暂无法定位（需累积交易日）")
    lhb = counts.get("lhb", 0)
    if lhb:
        lines.append(f"全市场龙虎榜：{lhb} 只上榜")
    return "\n".join(lines)


def judge(metrics: dict, counts: dict, date: str) -> Optional[dict]:
    """复盘裁判：读全部硬指标 → 结构化『明日关注点』。失败/未配 key 返回 None。"""
    if not config.llm_enabled():
        logger.info("未配 DeepSeek key，跳过 AI 研判")
        return None
    prompt = (
        f"{_BOUNDARY}\n\n以下是某交易日 A 股短线打板情绪的硬指标（纯计算、数据源直出）：\n\n"
        f"{_metrics_text(metrics, counts, date)}\n\n"
        "请据此收敛成一份『明日关注点』，只输出 JSON，字段：\n"
        f'- emotion_phase: 从 {list(_PHASES)} 选一个情绪档位\n'
        "- market_oneliner: 一句话概括当前盘面（≤40字）\n"
        "- focus_directions: 2~5 个关注方向，每个 {direction(方向/板块), logic(依据哪些读数), risk(风险)}\n"
        "- risk_alerts: 需警惕的信号（字符串数组）\n"
        "- verification_items: 2~5 条明日可验证条件（带今日基准值与阈值，次日能对账；字符串数组）\n"
    )
    try:
        raw = llm._chat([{"role": "user", "content": prompt}],
                        json_mode=True, temperature=0.15, max_tokens=6000)
        data = llm._parse_json(raw)
    except (llm.LLMError, ValueError) as e:
        logger.warning("复盘裁判失败，降级为无 AI 研判: %s", e)
        return None
    phase = data.get("emotion_phase", "")
    if phase not in _PHASES:  # 归一化容错
        phase = next((p for p in _PHASES if p in str(phase)), "")
    return {
        "emotion_phase": phase,
        "market_oneliner": str(data.get("market_oneliner", ""))[:80],
        "focus_directions": data.get("focus_directions") or [],
        "risk_alerts": data.get("risk_alerts") or [],
        "verification_items": data.get("verification_items") or [],
    }


def article(metrics: dict, counts: dict, date: str, focus: Optional[dict]) -> Optional[str]:
    """据硬指标 + 裁判研判，生成一篇可直接发布的复盘长文（markdown）。失败返回 None。"""
    if not config.llm_enabled():
        return None
    focus_txt = ""
    if focus:
        focus_txt = (f"\n\n【裁判研判】情绪档位：{focus.get('emotion_phase')}；"
                     f"{focus.get('market_oneliner')}；关注方向："
                     + "；".join(d.get("direction", "") for d in focus.get("focus_directions", [])))
    prompt = (
        f"{_BOUNDARY}\n\n你是短线复盘编辑。据下列硬指标写一篇**可直接发布**的当日 A 股情绪复盘长文"
        "（markdown，600~900字，收盘后口吻，客观陈述、有逻辑有节奏，"
        "结尾一句风险提示 + 『仅供参考，不构成投资建议』）。不要编造数据、不荐个股、不给买卖点。\n\n"
        f"{_metrics_text(metrics, counts, date)}{focus_txt}\n\n直接输出文章正文，不要额外解释。"
    )
    try:
        return llm._chat([{"role": "user", "content": prompt}],
                         json_mode=False, temperature=0.2, max_tokens=6000)
    except llm.LLMError as e:
        logger.warning("复盘文稿生成失败，降级为无文稿: %s", e)
        return None
