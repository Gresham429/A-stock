"""Agent 每日交易循环：职能流水线 + 可插拔决策 + 确定性失败归因。

设计见 `plan/2026-07-16-agent-evolution-design.md`。

    盘前 ① 研判(指数点位+K线结构+涨停跌停+板块强弱) → 大盘块 + 关注板块
    盘中 ② 选股(复用 app._screen_rows + _pa_score) → 候选
         ③ 决策(**可插拔**: SingleDecider / DebateDecider) → 买卖意向
         ④ 风控(确定性检查，可否决) → 放行的单
         ⑤ 下单(paper_store 真实撮合: 整手/涨跌停/T+1/**该账户的真实费率**)
    盘后 ⑥ 复盘(**确定性检测器**扫失败 → 教训库)

**教训由确定性检测器产出，不靠 LLM 判断。** `range_pos=92 → 追高` 是核对事实，
LLM 说「我觉得有点追高」不是。客观才可统计、才可反哺。LLM 只在决策步用。

**决策步可插拔**：`decide(ctx) -> list[Intent]` 是稳定契约，上下游不关心内部实现。
第一版 SingleDecider（一次 v4-pro）；DebateDecider（多空并行→裁判）已备好但默认不启用——
先跑 Single 拿基线，用数据证明需要辩论再换，两者归因数据落同一张表可直接 A/B。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import agent_store
import datasources as ds
import factor_lab
import fees
import llm
import paper_store
import profile_store
import outcome
import structure
import universe_store

logger = logging.getLogger(__name__)

# 确定性风控/归因阈值（不是 LLM 判断，是硬规则）
CHASE_HIGH_POS = 85.0      # 20 日区间位置 > 此值即视为追高
MAX_POS_PCT = 30.0         # 单标的仓位上限（占总资产%）
MIN_CASH_PCT = 10.0        # 现金下限
MAX_VOL = 120.0            # 年化波动率上限
STALE_DAYS = 20            # 持有超过 N 个交易日无动作 = 僵持
LOSS_CUT_PCT = -12.0       # 浮亏超过此值仍未止损 = 止损迟滞
# 买入即挂的止损线（**纪律参数**，非预测）。-10% 由 108,139 个历史建仓点回测选定
# (factor_lab.backtest_stops)：不止损平均收益最高(+1.94%)但 5% 最差达 -17.66%；
# -10% 花 0.27pp 平均收益把尾部压到 -10%，且触发率 33.6% 显著低于 -8% 的 43.1%；
# -5% 太紧(60% 被扫出)；-15%/-20% 被支配(少赚更多、尾部还不比自然分布好)。
STOP_LOSS_PCT = -10.0

# 交易时段分桶（北京时间）。**错过的桶不补**——补跑必然引入未来函数：
# 要在 14:00 重建 10:00 的决策，得重建整个信息环境（全市场快照/板块统计/资金流），
# 而 `ds.sina_all_stocks()` 只有当前快照、无 as-of 参数。任何一处泄漏，AI 就是拿
# 14:00 的答案做 10:00 的题，结论系统性偏乐观且无法自查。真实交易者也补不了。
# （对比：条件单能用日K补判，是因为「low 跌破止损价」是既成事实，不需要重建上下文。）
SLOTS: tuple[tuple[str, int, int], ...] = (
    ("早盘", 9 * 60 + 30, 11 * 60 + 30),
    ("尾盘", 13 * 60, 15 * 60),
)


def current_slot() -> str:
    """当前所处的交易时段桶；非交易时段返回空串。用北京时间（同 app._market_open）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    t = now.hour * 60 + now.minute
    for name, lo, hi in SLOTS:
        if lo <= t <= hi:
            return name
    return ""


@dataclass(frozen=True)
class Intent:
    """决策意向。决策步的输出契约——上下游只认这个，不关心内部是单 agent 还是辩论。"""

    code: str
    name: str
    side: str          # buy | sell
    shares: int
    reason: str
    ref_price: float = 0.0     # 决策时的现价（供风控算金额）
    limit_price: float = 0.0   # **限价**：挂单价，不到不成交


# ── ③ 决策：可插拔 ─────────────────────────────────────────────────────────
def _single_decider(ctx: dict[str, Any]) -> tuple[list[Intent], str]:
    """一次 v4-pro：给候选 + 持仓 + 现金 + 教训，让它输出买卖意向。"""
    prompt = _decide_prompt(ctx)
    # DeepSeek 是**推理模型**：max_tokens 同时覆盖「思考+正文」，太小会思考耗尽、
    # 正文返回空(finish_reason=length)。决策提示词含 规则库+教训+档位+成本块 ≈8000 字。
    raw = llm._chat([{"role": "system", "content": llm._system_prompt()[0]},
                     {"role": "user", "content": prompt}], max_tokens=8000)
    data = llm._parse_json(raw)
    return _intents_from(data, ctx), raw


def _debate_decider(ctx: dict[str, Any]) -> tuple[list[Intent], str]:
    """多头/空头**并行**论证 → 裁判定夺。两轮 ≈120s，不是 3×串行。

    默认不启用：先用 Single 拿基线，用数据证明需要辩论再切（归因数据落同一张表可 A/B）。
    """
    from concurrent.futures import ThreadPoolExecutor
    base = _decide_prompt(ctx)

    def side(role: str) -> str:
        stance = ("你是**多头**分析师。只论证「为什么现在该买入/加仓」，给出最强理由。"
                  if role == "bull" else
                  "你是**空头**分析师。只论证「为什么现在不该买、或该卖出」，主动挑刺，"
                  "默认怀疑；宁可错过不可做错。")
        # **8000 不是拍的，是量出来的**（2026-07-16，补入K线结构后提示词 8.9k→11.6k 字符）：
        #   多头 max_tokens=4000  → completion 3578（reasoning 3480）= 预算的 89%
        #   多头 max_tokens=16000 → completion 3823（reasoning 3700）← 模型自己收敛在 ~3800
        # 4000 踩在 89~96% 线上 = **掷硬币**：实测同一提示词一次 3578 通过、一次返回空正文
        # (finish_reason=length) 直接让整个辩论档 ok=False。
        #
        # **空头市里多头最费思考**：跌势中论证「为什么该买」难，reasoning 3480；
        # 空头论证「别买」轻松，reasoning 2080。故预算要按**多头在逆境下**的用量定，
        # 不能按空头的看着够用就行。
        #
        # max_tokens 是**上限不是配额**（按实际 completion 计费），故 4000→8000
        # 在正常情况下不多花钱，只是把掷硬币换成 2.1 倍余量。
        return llm._chat([{"role": "system", "content": llm._system_prompt()[0] + stance},
                          {"role": "user", "content": base + "\n\n只输出你的论点，不下结论。"}],
                         json_mode=False, max_tokens=8000)

    # **单边失败必须炸掉整个辩论，不许降级成一面之词。**
    # `llm._chat` 空正文即抛，`list(ex.map())` 让异常向外传播 → run_day 记 ok=False。
    # 别「好心」改成 try/except 把失败的一边吞成 ""：那样裁判只听另一边，仍会产出一个
    # 看着合理的结论，而**没人会发现多头根本没上场**——token 耗尽被伪装成市场判断，
    # 且在跌势里系统性偏空（多头恰恰是逆境下最容易耗尽的那个）。属 PITFALLS 第一类
    # 「不报错、只让你相信一个错结论」。宁可这一轮不交易。
    with ThreadPoolExecutor(max_workers=2) as ex:
        bull, bear = list(ex.map(side, ["bull", "bear"]))
    judge = llm._chat(
        [{"role": "system", "content": llm._system_prompt()[0]},
         {"role": "user", "content": base + f"\n\n【多头论点】\n{bull}\n\n【空头论点】\n{bear}\n\n"
                                            "你是裁判。权衡双方，输出最终买卖意向。"}],
        # 裁判的上下文最长（决策提示词 + 多空双方论点），故给得最多。
        # 曾设 5000 → finish_reason=length（思考耗尽、正文空）。
        max_tokens=12000)
    data = llm._parse_json(judge)
    return _intents_from(data, ctx), json.dumps(
        {"bull": bull, "bear": bear, "judge": judge}, ensure_ascii=False)


DECIDERS: dict[str, Callable[[dict], tuple[list[Intent], str]]] = {
    "single": _single_decider,
    "debate": _debate_decider,
}


_MKT_TTL = 300       # 大盘快照进程内缓存秒数：12 个 agent 看的是**同一个**大盘
_mkt_cache: tuple[float, str] = (0.0, "")
_MKT_LOCK = threading.Lock()

# 指数前缀**必须硬编码**：market_prefix 对 000xxx 一律判 sz，而上证在 sh（见 ds.index_quotes 注释）。
# 只取两条：上证=权重/大盘状态，创业板=成长股情绪。全取 5 条是无谓的 token。
_IDX_KLINE = {"上证指数": "sh000001", "创业板指": "sz399006"}

HORIZONS = outcome.HORIZONS       # (5,10,20)，跟 factor_lab 走
JUDGE_H = max(HORIZONS)           # 判罪看最长档(20日)：5日与20日实测 33.1% 判反
# 判罪线：超额收益落在历史分布**底部十分位**才记教训。
# **这是纪律参数，不是数据结论**——分布是事实(161,994 样本)，「取底部 10%」是
# 我方的**选择性**取舍，需用户认可，不假装有数据支持（PITFALLS#1 的要求）。
# 为什么不用「超额<0」这个看着最自然的线：实测 p50(20日) = **-0.90%**，
# 个股跑输上证是常态(指数市值加权) → 「超额<0」会把 **53%** 的建仓点判成失败，
# AI 会学到「你几乎总是失败」。分布本身否掉了那条线。
LESSON_PCT = 10

_BENCH_SYM = factor_lab.BENCH_SYM  # 超额基准=上证。**引用**不复制：判罪线(factor_lab
#                                   的超额分布)与被判的数必须同口径，否则不报错地错。


def _market_block() -> str:
    """【今日大盘】块：指数点位+结构 / 涨停跌停 / 板块强弱。

    **这里原先传的是一个板块名**（`sector_ranking()[0]["sector"]`），AI 看到的
    「大盘」字面上就是「传媒」两个字，故 2026-07-16 早盘 8/12 个 agent 报
    「无法判断市场状态」而拒绝出手——规则库里「市场状态识别」有 8 条规则要读它。

    容错：任一子项失败都降级为缺项，绝不抛异常——大盘取不到不该让整个决策崩掉。

    锁跨网络 I/O：正常 ~4s，最坏 ~65s（各源 timeout 15–20s 之和）。宁可让并行的
    另外 2 个 worker 等这一次，也好过 3 个 agent 各打一遍同一份大盘数据。
    """
    global _mkt_cache
    with _MKT_LOCK:
        ts, cached = _mkt_cache
        if cached and time.time() - ts < _MKT_TTL:
            return cached
        try:
            iq = ds.index_quotes()
        except Exception as e:  # noqa: BLE001
            logger.warning("指数行情失败: %s", e)
            iq = {"indices": [], "amount_liang_yi": None}
        struct: dict[str, dict | None] = {}
        for name, sym in _IDX_KLINE.items():
            try:
                struct[name] = structure.digest(ds.index_kline(sym))
            except Exception as e:  # noqa: BLE001
                logger.warning("指数K线失败 %s: %s", sym, e)
        try:
            # 注：涨跌家数子项已因东财 clist 封 IP 恒为 None，仅涨停/跌停(push2ex)可用。
            mb = ds.market_breadth()
        except Exception as e:  # noqa: BLE001
            logger.warning("市场情绪失败: %s", e)
            mb = {}
        try:
            sectors = universe_store.sector_ranking(kind="sw1", limit=5)
        except Exception as e:  # noqa: BLE001
            logger.warning("板块排行失败: %s", e)
            sectors = []
        out = structure.fmt_market(iq.get("indices") or [], iq.get("amount_liang_yi"),
                                   struct, mb.get("limit_up"), mb.get("limit_down"), sectors)
        _mkt_cache = (time.time(), out)
        return out


def _decide_prompt(ctx: dict[str, Any]) -> str:
    cand = "\n".join(
        f"- {r['code']} {r['name']} 现价{r.get('price')} 形态分{r.get('pa_score')} "
        f"波动{r.get('vol')} 区间位置{r.get('range_pos')} 20日涨{r.get('cum20')}% "
        f"1手{r.get('lot_cost')}元 [{r.get('primary')}/{r.get('sub')}]\n"
        f"    {ctx.get('struct', {}).get(r['code'], '无K线数据')}"
        for r in ctx.get("candidates", [])[:20]) or "（无候选）"
    pos = "\n".join(
        f"- {p['code']} {p['name']} {p['shares']}股 可卖{p['sellable']} 成本{p['avg_cost']} 现价{p.get('price')}"
        f" 浮盈亏{p.get('pnl_pct')}%\n"
        f"    {ctx.get('struct', {}).get(p['code'], '无K线数据')}"
        for p in ctx.get("positions", [])) or "（空仓）"
    return f"""你在模拟盘上自主交易，今天必须给出买卖意向（可以是「不操作」）。

【账户】可用现金 {ctx['cash']:.2f} 元；总资产 {ctx['total']:.2f} 元

【今日大盘】
{ctx.get('market') or '（大盘数据缺失）'}

【关注板块】{ctx.get('focus') or '全市场'}

【当前持仓】
{pos}

【候选股（已按形态分排序，形态分含波动/资金/动量/区间位置四项）】
{cand}

> 每只第二行是日K结构：均线 / 近20日高低 / 最近3根K线（`MMDD:开-高-低-收`）。
> **注意日期**：日K只到**上一交易日**（今日未收盘），而「现价」是当前实时价——
> 最后一根不是今天。两者要分开看。
> 趋势、市场状态、信号棒由**你**据这些数据判断——上面不做方向结论。

{ctx.get('blocks', '')}

严格遵循上面给出的【本金玩法档】【交易成本】【交易分析框架规则】【历史教训】。
硬性约束：A股 T+1（当日买入当日不可卖）；买入必须整百股；单标的仓位不超过总资产 {MAX_POS_PCT:.0f}%；
现金不低于总资产 {MIN_CASH_PCT:.0f}%；**收益低于保本涨幅的价差不要做**。

你的委托是**限价单**：给出 `limit_price`（买入=你愿意的最高价，卖出=最低价）。
- **当日有效**：收盘未触及即作废，明天要重新决策。挂太低=大概率不成交（错过），
  挂太高=立刻成交但价格差。这个权衡由你判断。
- 可以**一次挂多笔**（不同标的、或同标的分批不同价位）；受仓位与现金上限约束。
- 不写 limit_price 则按现价挂（等同市价，立刻成交）。

只输出 JSON：
{{"intents":[{{"code":"600760","side":"buy","shares":100,"limit_price":39.50,"reason":"简短理由"}}],
  "skip_reason":"若不操作，说明原因"}}"""


def _intents_from(data: dict, ctx: dict) -> list[Intent]:
    names = {r["code"]: r.get("name", "") for r in ctx.get("candidates", [])}
    names.update({p["code"]: p.get("name", "") for p in ctx.get("positions", [])})
    prices = {r["code"]: r.get("price") or 0 for r in ctx.get("candidates", [])}
    prices.update({p["code"]: p.get("price") or 0 for p in ctx.get("positions", [])})
    out = []
    for it in (data.get("intents") or []):
        code = str(it.get("code") or "").strip()
        side = str(it.get("side") or "").strip().lower()
        if len(code) != 6 or side not in ("buy", "sell"):
            continue
        try:
            shares = int(it.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0:
            continue
        ref = float(prices.get(code) or 0)
        try:
            lp = float(it.get("limit_price") or 0)
        except (TypeError, ValueError):
            lp = 0.0
        if lp <= 0:
            lp = ref              # AI 没给限价 → 按现价挂（等价于市价单）
        out.append(Intent(code, names.get(code, code), side, shares,
                          str(it.get("reason") or "")[:200], ref, lp))
    return out


# ── ④ 风控：确定性检查，可否决 ─────────────────────────────────────────────
def risk_check(it: Intent, ctx: dict[str, Any]) -> tuple[bool, str]:
    """硬规则否决。返回 (放行?, 原因)。这里不用 LLM——规则是死的，判断必须可复现。"""
    if it.side == "sell":
        held = next((p for p in ctx["positions"] if p["code"] == it.code), None)
        if not held:
            return False, "未持有该股"
        if it.shares > (held.get("sellable") or 0):
            return False, f"可卖仅 {held.get('sellable')} 股（T+1 锁定）"
        return True, ""
    amount = it.shares * (it.limit_price or it.ref_price or 0)   # 挂单占用按限价算
    if amount <= 0:
        return False, "无有效价格"
    fee = fees.total("buy", amount, ctx["sched"])
    if amount + fee > ctx["cash"]:
        return False, f"现金不足（需 {amount + fee:.2f}，有 {ctx['cash']:.2f}）"
    if ctx["total"] and (ctx["cash"] - amount - fee) / ctx["total"] * 100 < MIN_CASH_PCT:
        return False, f"买入后现金将低于 {MIN_CASH_PCT:.0f}%"
    if ctx["total"] and amount / ctx["total"] * 100 > MAX_POS_PCT:
        return False, f"单标的仓位将超 {MAX_POS_PCT:.0f}%"
    m = ctx["metrics"].get(it.code) or {}
    if (m.get("vol") or 0) > MAX_VOL:
        return False, f"波动率 {m.get('vol')} 超上限 {MAX_VOL:.0f}"
    return True, ""


# ── ⑥ 复盘：确定性失败检测器 ───────────────────────────────────────────────
def detect_failures(agent_id: int, ctx: dict[str, Any], filled: list[dict]) -> list[str]:
    """扫**客观**失败信号 → 教训库。只记失败，不记成功（成功多为 beta）。

    每条都由确定性规则判定，不问 LLM——`range_pos=92 → 追高`是核对事实。

    **有因子数据的属性，教训必须与 `factor_lab.direction()` 一致**（见下）。
    """
    found: list[str] = []
    # 教训方向必须跟数据走，不跟拍脑袋的阈值走。
    # 踩过的坑：_pa_score 按 vol 的正向 IC(近60日 t=+6.96) 选出高波动股 → AI 买入
    # → 这里立刻记「波动失控」→ _lesson_block() 注入 5 个 AI → 教它们避开数据证明
    # 有效的行为。**系统在惩罚 AI 服从我自己的数据**，且污染用户自己用的 5 个分析。
    # 故：只有当数据说「该属性不利」(sign<0) 时才记；sign>=0（有利 或 不显著）不记——
    # 与 _pa_score「方向未知则不参与打分」同一原则：不猜。
    # 风险由 MAX_VOL 硬门管（风控），教训不重复管、更不该用比风控更严的线。
    try:
        dirs = factor_lab.directions()
    except Exception as e:  # noqa: BLE001 因子库不可用时退化为「不记有因子的那几条」
        logger.warning("因子方向读取失败，跳过因子类教训: %s", e)
        dirs = {}

    def bad(factor: str) -> bool:
        """数据是否判定该属性不利（方向为负）。不显著/无数据 → False（不记）。"""
        return (dirs.get(factor) or {}).get("sign", 0) < 0

    def note(kind: str, ev: Any, code: str = "") -> None:
        if agent_store.add_lesson(agent_id, kind, str(ev), code):
            found.append(f"{kind}({ev})")

    for f in filled:
        if f.get("side") != "buy":
            continue
        m = ctx["metrics"].get(f["code"]) or {}
        rp = m.get("range_pos")
        if rp is not None and rp > CHASE_HIGH_POS and bad("range_pos"):
            note("chase_high", rp, f["code"])
        vol = m.get("vol")
        if vol is not None and vol > MAX_VOL * 0.8 and bad("vol"):
            note("high_vol_entry", vol, f["code"])
        sec = ctx.get("sector_chg", {}).get(f["code"])
        if sec is not None and sec < -1.0:   # 板块当日涨跌无因子回测，按硬规则记
            note("against_sector", round(abs(sec), 2), f["code"])
    # 卖出赚不抵费
    for f in filled:
        if f.get("side") != "sell":
            continue
        held = ctx.get("cost_before", {}).get(f["code"])
        if not held:
            continue
        gain_pct = (f["price"] / held - 1) * 100 if held else 0
        be = fees.breakeven_pct(f["price"] * f["shares"], ctx["sched"])
        if 0 < gain_pct < be:
            note("below_breakeven", round(gain_pct, 3), f["code"])
    # 组合层
    if ctx["total"] and ctx["cash"] / ctx["total"] * 100 < MIN_CASH_PCT:
        note("cash_exhausted", round(ctx["cash"] / ctx["total"] * 100, 1))
    for p in ctx["positions"]:
        if p.get("pnl_pct") is not None and p["pnl_pct"] < LOSS_CUT_PCT:
            note("loss_cut_late", round(p["pnl_pct"], 1), p["code"])
        if (p.get("held_days") or 0) > STALE_DAYS:
            note("stale_hold", p["held_days"], p["code"])
    if ctx["total"]:
        for p in ctx["positions"]:
            pct = (p.get("market_value") or 0) / ctx["total"] * 100
            if pct > MAX_POS_PCT:
                note("oversize", round(pct, 1), p["code"])
    return found


# ── 条件单补判（解决「app 关着的区间段怎么操作」） ─────────────────────────
def sweep_conditions(agent_id: int) -> list[dict[str, Any]]:
    """用**日K回溯**判定条件单在 app 关闭期间是否曾被触发，触发则按触发价成交。

    为什么这么做：app 关着就没有实时行情，条件单无从触发。但**日K记录了真实发生过的
    最高/最低价**——「跌破 38 止损」只要那几天有 low ≤ 38 就是真触发过，用它补判
    不是作弊，是还原事实。

    **保守成交**：按 trigger_price 而非当日最优价成交。日K只有 OHLC、不知道日内路径，
    乐观假设会系统性高估策略表现（这正是回测最常见的自欺）。

    局限：同日既触发止损又触发止盈时，无法判定孰先孰后 → 按**止损优先**处理（保守）。
    """
    ag = agent_store.get_agent(agent_id)
    if not ag:
        return []
    conds = agent_store.live_conditions(agent_id)
    if not conds:
        return []
    sched = profile_store.fee_schedule(ag["profile_id"])
    fired: list[dict[str, Any]] = []
    by_code: dict[str, list[dict]] = {}
    for c in conds:
        by_code.setdefault(c["code"], []).append(c)
    for code, cs in by_code.items():
        try:
            kl = ds.sina_kline(code, num=60, scale=240)
        except Exception as e:  # noqa: BLE001 单只失败不拖垮整批
            logger.warning("条件单补判取 K 线失败 %s: %s", code, e)
            continue
        if not kl:
            continue
        # 止损优先：同日两者都触发时，先认止损（保守）
        for c in sorted(cs, key=lambda x: 0 if x["kind"] == "stop_loss" else 1):
            bar = next((k for k in kl if k["date"] > c["created_date"] and (
                (c["kind"] == "stop_loss" and float(k["low"]) <= c["trigger_price"]) or
                (c["kind"] == "take_profit" and float(k["high"]) >= c["trigger_price"]))), None)
            if not bar:
                continue
            pos = next((p for p in paper_store.positions_of(ag["account_id"])
                        if p["code"] == code), None)
            if not pos or (pos.get("sellable") or 0) <= 0:
                agent_store.close_condition(c["id"], "cancelled")
                continue
            shares = min(c["shares"], pos["sellable"])
            q = ds.tencent_quote([code]).get(code, {}) or {}
            r = paper_store.order(ag["account_id"], code, c["name"] or code, "sell", "limit",
                                  c["trigger_price"], shares,
                                  {**q, "price": c["trigger_price"]}, True, sched=sched)
            agent_store.close_condition(c["id"], "triggered" if r.get("ok") else "cancelled",
                                        bar["date"], c["trigger_price"])
            if r.get("ok"):
                fired.append({"code": code, "kind": c["kind"], "date": bar["date"],
                              "price": c["trigger_price"], "shares": shares})
                # 止损触发才记教训，且必须**真的亏了**——止损价高于成本时是止盈性质的
                # 保护性离场，记成「止损迟滞」是错的（会污染教训统计）。
                cost = pos.get("avg_cost") or 0
                if c["kind"] == "stop_loss" and cost > 0:
                    ret_pct = (c["trigger_price"] / cost - 1) * 100
                    if ret_pct < 0:
                        agent_store.add_lesson(agent_id, "loss_cut_late",
                                               str(round(ret_pct, 1)), code)
                agent_store.cancel_conditions(agent_id, code)
    if fired:
        agent_store.log_run(agent_id, date_cls.today().isoformat(), "条件单",
                            f"补判触发 {len(fired)} 笔", detail=fired)
    return fired


# ── 限价委托的成交判定 ─────────────────────────────────────────────────────
def _touched(code: str, side: str, limit: float, since_t: str, same_day: bool) -> tuple[bool, str, float]:
    """挂单后价格是否**真的触及过**限价。返回 (触及?, 时刻, 成交价)。

    **当日**走分时（精确到分钟，只看 since_t 之后的点）；**隔夜**走日K 的 low/high。
    两者都是**既成事实**——跟条件单日K补判同一个道理，不需要重建当时的信息环境，
    故零未来函数。

    **成交价一律用 limit_price，不用当时的更优价**：分时只有每分钟收盘、无分钟内高低，
    乐观假设会系统性高估表现。宁可少成交、宁可成交价差，也不能自欺。
    """
    try:
        if same_day:
            pts = [p for p in ds.tencent_minute(code) if p.get("t", "") >= since_t]
            if not pts:
                return False, "", 0.0
            if side == "buy":
                hit = next((p for p in pts if float(p["price"]) <= limit), None)
            else:
                hit = next((p for p in pts if float(p["price"]) >= limit), None)
            return (True, hit["t"], limit) if hit else (False, "", 0.0)
        kl = ds.sina_kline(code, num=3, scale=240)
        if not kl:
            return False, "", 0.0
        bar = kl[-1]
        ok = (float(bar["low"]) <= limit) if side == "buy" else (float(bar["high"]) >= limit)
        return (True, "收盘前", limit) if ok else (False, "", 0.0)
    except Exception as e:  # noqa: BLE001 单只失败不拖垮整批
        logger.warning("挂单成交判定失败 %s: %s", code, e)
        return False, "", 0.0


def _sector_bars(sector: str, entry_date: str) -> list[dict[str, Any]]:
    """把 `sector_daily` 的每日均涨跌**复利**成一条伪 K 线（只有 close 有意义）。

    板块没有现成指数可用（东财板块指数走 clist，已封 IP —— PITFALLS#14）。
    ⚠️ 两个已知限制，故板块超额只作**附加**、缺了不影响主标签：
      · `sector_daily` **只有 2 天历史**（2026-07-15 起才有），无法回溯；
      · 它靠「每交易日开 app」写入，漏一天即断档 —— 断档会让复利跨过缺失日，
        算出的收益偏差不可控。故**发现断档就整个放弃**，不给半真的数。
    """
    if not sector:
        return []
    rows = universe_store.sector_history(sector, days=max(HORIZONS) + 40)
    rows = [r for r in rows if r["date"] >= entry_date]
    if not rows or rows[0]["date"] != entry_date:
        return []                       # 建仓日当天没有板块统计 → 没有基准起点
    bars, nav = [], 100.0
    for i, r in enumerate(rows):
        if i:                           # 第 0 根 = 建仓日基点，其涨跌属于建仓日之前
            nav *= 1 + (r["avg_chg"] or 0) / 100
        bars.append({"date": r["date"], "close": round(nav, 4)})
    return bars


def _judge_entry(e: dict[str, Any], x: float | None) -> str:
    """结清的建仓 → 该不该记教训。**判罪线只能来自数据分布，无分布则不判。**

    与旧的 T+0 判罪（`detect_failures` 的 chase_high 等）的根本区别：
    那些判的是「买入时长得像错」，这条判的是「**事后证明**跑输了历史上 90% 的建仓点」。

    文案是**个案陈述**，不宣称规律：n 太小（20–30 笔/年，统计验证要 784 笔≈31 年，
    PITFALLS#5），把单笔亏损写成「>85 视为追高」那种普适口吻，就是拿一个样本冒充规律。
    属性快照只作**当时的上下文**列出，不宣称因果。
    """
    if x is None:
        return ""                       # 超额算不出（基准缺失/停牌）→ 不判，不拿绝对收益顶替
    rank = factor_lab.rank_of(JUDGE_H, x)
    if rank is None:
        logger.info("无超额分布，跳过判罪（跑一次 factor_lab.backtest 即可）")
        return ""                       # **无分布不判罪**：退回默认阈值=偷偷拍了个数
    if rank > LESSON_PCT:
        return ""                       # 没差到底部十分位 → 不是失败，不记
    ctx = "/".join(
        f"{k}={e[k]}" for k in ("pa_score", "range_pos", "vol", "cum20")
        if e.get(k) is not None) or "属性快照缺失"
    ev = (f"{e['code']} {e['name']} 于 {e['entry_date']} 建仓价 {e['entry_price']}，"
          f"{JUDGE_H} 交易日超额收益 {x:+.2f}%（扣除上证同期涨跌后），"
          f"在 16 万个历史建仓点中处于**第 {rank} 百分位**——比 {100 - rank}% 的建仓点差。"
          f"当时属性：{ctx}。"
          f"（单笔个案，非规律；属性仅为当时上下文，不代表因果）")
    return "bad_outcome" if agent_store.add_lesson(e["agent_id"], "bad_outcome", ev,
                                                   e["code"]) else ""


def settle_entries(agent_id: int) -> list[dict[str, Any]]:
    """结算建仓留痕：5/10/20 交易日后算**超额**收益。**只算不判罪**。

    这是「学失败」链路从「买入时长得像错」转向「事后证明错」的那一步。
    为什么原来错：`chase_high` 的文案写着「此后回落」，却在成交同一循环里就记，
    那时「此后」还没发生（`detect_failures` 传的是 `settled["filled"]`）。

    **补跑合法**：走日K = 既成事实，同 `sweep_orders`/`sweep_conditions`
    （PITFALLS#3 明确允许）。不是重建决策环境，无未来函数。

    幂等：同一条会被反复扫到（r5 先到、r20 后到）；20 日档出齐才落 `settled_at`。
    """
    out = []
    for e in agent_store.open_entries(agent_id):
        try:
            bars = ds.sina_kline(e["code"], num=max(HORIZONS) + 40)
            if not bars:
                continue
            stock = outcome.forward_returns(bars, e["entry_date"], e["entry_price"])
            # 基准按**个股 +h 根落在的那一天**对齐——个股停牌时两边各数 h 根会错配窗口
            # （实测超额高估 10 个百分点且不报错）。
            ends = outcome.horizon_end_dates(bars, e["entry_date"])
            idx = outcome.bench_returns(
                ds.index_kline(_BENCH_SYM, num=max(HORIZONS) + 40), e["entry_date"], ends)
            x = outcome.excess(stock, idx)
            s = outcome.excess(stock, outcome.bench_returns(
                _sector_bars(e["sector"] or "", e["entry_date"]), e["entry_date"], ends))
            rets: dict[str, float | None] = {}
            for h in HORIZONS:
                rets[f"r{h}"], rets[f"x{h}"], rets[f"s{h}"] = stock[h], x[h], s[h]
            done = stock[JUDGE_H] is not None            # 最长档出了 = 这条结清
            agent_store.update_entry(e["id"], rets, done)
            if done:
                rec = {"code": e["code"], "entry_date": e["entry_date"],
                       **{k: v for k, v in rets.items() if v is not None}}
                rec["lesson"] = _judge_entry(e, x[JUDGE_H])
                out.append(rec)
        except Exception as ex:  # noqa: BLE001 单条结算失败不该拖垮整轮
            logger.warning("建仓结算失败 %s %s: %s", e["code"], e["entry_date"], ex)
    return out


def sweep_orders(agent_id: int) -> dict[str, list]:
    """判定该 agent 所有在挂委托：触及则成交，隔日未成交则作废（A股当日有效）。

    **必须在决策之前跑** —— 否则 AI 会基于「还没结算的持仓」重复下单。
    """
    ag = agent_store.get_agent(agent_id)
    if not ag:
        return {"filled": [], "expired": []}
    today = date_cls.today().isoformat()
    sched = profile_store.fee_schedule(ag["profile_id"])
    filled, expired = [], []
    for o in agent_store.live_pending(agent_id):
        same_day = o["placed_date"] == today
        hit, at, px = _touched(o["code"], o["side"], o["limit_price"], o["placed_t"], same_day)
        if not hit:
            if not same_day:   # A股委托当日有效，隔日未成交即作废
                agent_store.close_pending(o["id"], "expired", note="当日有效，未触及限价")
                expired.append({"code": o["code"], "side": o["side"],
                                "limit": o["limit_price"]})
            continue
        q = ds.tencent_quote([o["code"]]).get(o["code"], {}) or {}
        r = paper_store.order(ag["account_id"], o["code"], o["name"] or o["code"],
                              o["side"], "limit", px, o["shares"],
                              {**q, "price": px}, True, sched=sched)
        if r.get("ok"):
            agent_store.close_pending(o["id"], "filled", o["placed_date"], at, px)
            filled.append({"code": o["code"], "side": o["side"], "shares": o["shares"],
                           "price": px, "t": at, "date": o["placed_date"]})
            if o["side"] == "buy":   # 买入成交即挂止损（纪律）
                agent_store.add_condition(agent_id, o["code"], o["name"], "stop_loss",
                                          round(px * (1 + STOP_LOSS_PCT / 100), 3),
                                          o["shares"], note=f"建仓价 {px} 的 {STOP_LOSS_PCT}%")
                # 建仓留痕：**只记事实、不判罪**。5/10/20 交易日后由 settle_entries 结算超额。
                # 快照取**挂单时存下的**（决策那一刻 AI 看到的），不在这里重取——
                # 此处 ctx 尚未构建（sweep 跑在选股之前），且事后重取会拿到已经变了的值。
                try:
                    snap = json.loads(o.get("snap") or "{}")
                except (TypeError, ValueError):
                    snap = {}
                agent_store.add_entry(agent_id, o["code"], o["name"] or o["code"],
                                      o["placed_date"], px, o["shares"], snap)
            else:
                agent_store.cancel_conditions(agent_id, o["code"])
        else:
            agent_store.close_pending(o["id"], "cancelled", note=str(r.get("msg"))[:80])
    if filled or expired:
        agent_store.log_run(agent_id, today, "委托结算",
                            f"成交 {len(filled)} / 过期 {len(expired)}",
                            detail={"filled": filled, "expired": expired})
    return {"filled": filled, "expired": expired}


# ── 日循环 ─────────────────────────────────────────────────────────────────
def already_ran(agent_id: int, date: str = "", slot: str = "") -> bool:
    """该 agent 今日该时段是否已跑过（只读检查，供 UI/日志用）。

    **真正的幂等靠 `agent_store.claim_slot()` 原子占位**——本函数是「开头查、
    180 秒后才写」的模式，两个并发 run_all 会双双挤过去（实测发生过）。
    """
    date = date or date_cls.today().isoformat()
    slot = slot or current_slot() or "手动"
    return slot in agent_store.slots_done(agent_id, date)


def run_day(agent_id: int, focus: str = "", dry_run: bool = False,
            blocks: str = "", force: bool = False, slot: str = "") -> dict[str, Any]:
    """跑一个 agent 的一个**时段桶**。dry_run 只决策不下单；force 跳过幂等门。

    slot 留空则取当前时段桶（非交易时段为「手动」）。同一 (agent, 日, 桶) 只跑一次
    —— 靠 `claim_slot()` 原子占位，不是靠查记录。
    """
    t0 = time.time()
    ag = agent_store.get_agent(agent_id)
    if not ag:
        return {"ok": False, "msg": "agent 不存在"}
    today = date_cls.today().isoformat()
    slot = slot or current_slot() or "手动"
    claimed = False
    if not force and not dry_run:
        if not agent_store.claim_slot(agent_id, today, slot):
            return {"ok": True, "skipped": f"今日「{slot}」已跑过（幂等）",
                    "agent": ag["name"], "date": today, "slot": slot}
        claimed = True
    # 试跑的日志加前缀 —— 不污染幂等门、不与真跑记录混淆
    def _ph(p: str) -> str:
        return ("试跑-" + p) if dry_run else f"{p}@{slot}"
    acct = paper_store.get_account(ag["account_id"])
    if not acct:
        return {"ok": False, "msg": "模拟盘账户不存在"}
    sched = profile_store.fee_schedule(ag["profile_id"])

    # ① 关注板块：没指定则取当日板块日排行最强的一个（复用 universe_store 的统计）
    if not focus:
        rank = universe_store.sector_ranking(kind="sw1", limit=1)
        focus = rank[0]["sector"] if rank else ""
    swept = sweep_conditions(agent_id)          # 先补判条件单（app 关闭期间可能已触发）
    settled = sweep_orders(agent_id)            # 再结算在挂委托 —— **必须在决策之前**，
    #                                             否则 AI 会基于未结算的持仓重复下单
    graded = settle_entries(agent_id)           # 建仓留痕结算（走日K，与当前时段无关）
    if swept:
        acct = paper_store.get_account(ag["account_id"]) or acct
    market = _market_block()          # 指数点位+K线结构+涨停跌停+板块强弱（12 个 agent 共享缓存）
    agent_store.log_run(agent_id, today, _ph("研判"),
                        f"关注板块={focus or '全市场'}；大盘 {len(market)} 字"
                        + (f"；条件单补判触发 {len(swept)} 笔" if swept else "")
                        + (f"；建仓结算 {len(graded)} 笔" if graded else ""),
                        detail={"graded": graded} if graded else None)

    # ② 选股（复用既有 _screen_rows；延迟导入避免循环依赖）
    import app
    cands = app._screen_rows(float(acct["cash"]), focus)[:20]
    agent_store.log_run(agent_id, today, _ph("选股"), f"候选 {len(cands)} 只 focus={focus}")

    # 持仓 + 行情
    positions = paper_store.positions_of(ag["account_id"])
    codes = list({*(p["code"] for p in positions), *(c["code"] for c in cands)})
    quotes = ds.tencent_quote(codes) if codes else {}
    mv = 0.0
    for p in positions:
        q = quotes.get(p["code"], {}) or {}
        p["price"] = q.get("price") or 0
        p["market_value"] = p["price"] * (p.get("shares") or 0)
        p["pnl_pct"] = (round((p["price"] / p["avg_cost"] - 1) * 100, 2)
                        if p.get("avg_cost") else None)
        mv += p["market_value"]
    total = float(acct["cash"]) + mv

    # 候选股 + 持仓的日K结构（规则库 84 条里 37 条要 K线/均线/趋势，原先一条都没给）。
    # 走 app._safe_kline 的 TTL 缓存：12 个 agent 候选高度重叠，不缓存要打 240 次请求。
    struct: dict[str, str] = {}
    for code in codes:
        struct[code] = structure.fmt_stock(structure.digest(app._safe_kline(code)))

    ctx: dict[str, Any] = {
        "cash": float(acct["cash"]), "total": total, "focus": focus,
        "candidates": cands, "positions": positions, "sched": sched,
        "metrics": {c["code"]: c for c in cands},
        "cost_before": {p["code"]: p.get("avg_cost") for p in positions},
        "sector_chg": {}, "blocks": blocks,
        "struct": struct,
        "market": market,
    }
    # 各候选所属一级板块的当日涨跌（供「逆势」检测）
    day_rank = {r["sector"]: r["avg_chg"] for r in universe_store.sector_ranking(kind="sw1", limit=40)}
    for c in cands:
        ctx["sector_chg"][c["code"]] = day_rank.get(c.get("primary"))

    # 每个 agent 用**自己的**档位/费率块（档位按本账户总资产，非用户真实持仓）
    if not blocks:
        ctx["blocks"] = app._agent_blocks(ag, ctx["cash"], total, len(positions))
    # ③ 决策（可插拔）
    decider = DECIDERS.get(ag.get("decider") or "single", _single_decider)
    try:
        intents, raw = decider(ctx)
    except (llm.LLMError, ValueError) as e:
        agent_store.log_run(agent_id, today, _ph("决策"), f"失败: {e}", ok=False)
        if claimed:   # 跑失败则释放占位，下次开 app 可重试
            agent_store.release_slot(agent_id, today, slot)
        return {"ok": False, "msg": f"决策失败: {e}", "slot": slot}
    agent_store.log_run(agent_id, today, _ph("决策"),
                        f"{ag.get('decider')} → {len(intents)} 条意向", detail=raw,
                        ms=int((time.time() - t0) * 1000))

    # ④ 风控 + ⑤ 下单
    placed, rejected = [], []
    market_open = app._market_open()   # 交易时段门控（app 已有实现，复用）
    for it in intents:
        ok, why = risk_check(it, ctx)
        if not ok:
            rejected.append({"code": it.code, "side": it.side, "msg": why})
            continue
        if dry_run:
            placed.append({"code": it.code, "side": it.side, "shares": it.shares,
                           "limit": it.limit_price, "dry": True})
            continue
        # **挂单，不即时成交** —— 下个 slot / 次日开 app 时用分时或日K判定是否触及
        now_t = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        # 决策时刻的属性快照跟着委托走：成交后转存 entries，5/10/20 日后结算时回头看它。
        # 只对买入留痕——判的是「建仓决策」；卖出的结果由 below_breakeven 当场核对。
        m = ctx["metrics"].get(it.code) or {}
        snap = {"range_pos": m.get("range_pos"), "vol": m.get("vol"),
                "cum20": m.get("cum20"), "pa_score": m.get("pa_score"),
                "sector": m.get("primary") or "",
                "sector_chg": ctx.get("sector_chg", {}).get(it.code)} if it.side == "buy" else {}
        agent_store.place(agent_id, it.code, it.name, it.side, it.limit_price,
                          it.shares, now_t, it.reason, snap)
        placed.append({"code": it.code, "side": it.side, "shares": it.shares,
                       "limit": it.limit_price, "t": now_t})
        # 挂单即冻结资金（按限价），避免同一轮里多笔委托超额占用
        if it.side == "buy":
            amt = it.shares * it.limit_price
            ctx["cash"] -= amt + fees.total("buy", amt, sched)
    agent_store.log_run(agent_id, today, _ph("挂单"),
                        f"挂出 {len(placed)} / 否决 {len(rejected)}"
                        + (f"；本轮结算成交 {len(settled['filled'])}" if settled["filled"] else ""),
                        detail={"placed": placed, "rejected": rejected, "settled": settled})

    # ⑥ 复盘：确定性失败检测
    # 教训基于**真正成交**的（来自本轮结算），不是刚挂出的委托——挂单未必成交
    lessons = detect_failures(agent_id, ctx, settled["filled"])
    agent_store.log_run(agent_id, today, _ph("复盘"),
                        f"教训 {len(lessons)} 条: {', '.join(lessons) or '无'}")
    acct2 = paper_store.get_account(ag["account_id"]) or acct
    agent_store.log_equity(agent_id, today, float(acct2["cash"]), mv,
                           float(acct["init_capital"] or 0))
    agent_store.purge()  # 按日累积的表一律清理
    return {"ok": True, "agent": ag["name"], "date": today, "slot": slot, "focus": focus,
            "swept": swept,
            "candidates": len(cands), "intents": len(intents),
            "placed": placed, "filled": settled["filled"], "expired": settled["expired"],
            "rejected": rejected, "lessons": lessons,
            "cash": float(acct2["cash"]), "market_value": round(mv, 2),
            "ms": int((time.time() - t0) * 1000)}


def run_all(dry_run: bool = False, blocks: str = "", force: bool = False,
            workers: int = 3, require_open: bool = False) -> list[dict[str, Any]]:
    """跑所有启用的 agent（多档位 / 同档多账户并行实验）。

    blocks 留空则每个 agent 各自构建（推荐——多档位实验必须如此）。

    **require_open=True（启动自动跑用）：非交易时段只补判条件单，不跑决策。**
    因为 `paper_store.order` 在 `market_open=False` 时一律拒单——非交易时段跑决策
    等于烧掉每 agent 一次 v4-pro 却一笔不成。手动触发(force)与试跑(dry_run)不受此限。

    **并行 workers=3**：LLM 调用是 I/O 阻塞，串行 12 个 agent 要 8 分钟。
    不设更高是因为：① 每个 agent 还会打新浪/腾讯取行情，并发太高会被限流；
    ② DeepSeek 侧的并发上限未知，宁可保守。各 agent 的 paper 账户互不相干，
    SQLite 走 WAL 之外的写入由各 store 的 _LOCK 串行化，故并行是安全的。

    **非交易日直接跳过**：周末/节假日没有行情，跑了只会拿昨收当今价、产生假决策。
    """
    import news_store
    if not force and not news_store.is_trading_day():
        logger.info("非交易日，agent 日循环跳过")
        return [{"ok": True, "skipped": "非交易日"}]
    agents = agent_store.list_agents(active_only=True)
    if not agents:
        return []
    slot = current_slot()
    if require_open and not dry_run and not force:
        import app
        if not app._market_open() or not slot:
            fired = []
            for ag in agents:      # 非交易时段仍补判条件单：补的是**历史**已发生的触发
                try:
                    fired += sweep_conditions(ag["id"])
                except Exception as e:  # noqa: BLE001
                    logger.warning("条件单补判失败 %s: %s", ag["name"], e)
            logger.info("非交易时段：跳过 %d 个 agent 的决策（补判条件单 %d 笔）",
                        len(agents), len(fired))
            return [{"ok": True, "skipped": "非交易时段（决策已跳过，条件单已补判）",
                     "swept": len(fired)}]
        logger.info("当前时段桶：%s", slot)

    def one(ag: dict[str, Any]) -> dict[str, Any]:
        try:
            return run_day(ag["id"], dry_run=dry_run, blocks=blocks, force=force,
                           slot=slot or "手动")
        except Exception as e:  # noqa: BLE001 单个 agent 崩了不该拖垮整批
            logger.exception("agent %s 日循环失败", ag["name"])
            return {"ok": False, "agent": ag["name"], "msg": str(e)}

    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        out = list(ex.map(one, agents))
    logger.info("agent 日循环完成：%d 个，%.0fs（并行 %d）", len(out), time.time() - t0, workers)
    return out
