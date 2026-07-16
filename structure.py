"""K 线结构摘要 —— 给 AI 的**原料**，不是结论。

**为什么存在**：规则库注入的是价格行为体系（84 条启用规则里 37 条要 K线/均线/
趋势/信号棒），而决策提示词原先只给静态标量（形态分/波动/区间位置/20日涨）。
2026-07-16 早盘 12 个 agent 有 11 个产出 0 条意向，理由清一色「数据不足：缺少
K线、趋势、均线」——AI 是在正确地服从 R6「看不懂就等」。本模块补上那份数据。

**红线：只给原料，不下判断。** 这里不算「趋势=上涨」「信号棒=看涨吞没」这类带
阈值的结论——趋势由 AI 读均线关系与 K 线自己判。理由见 PITFALLS#1：本项目拍脑袋
定的方向已被数据打脸 5 次（`_pa_score` 两个分量方向全反、止损线、教训阈值、
「方向会抖动」的判断）。能验的才敢定方向，验不了的就把原料交给 AI，别猜。

纯函数、零网络、零 IO —— 取数在 `datasources`，缓存在 `app`。
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

MA_WINDOWS = (5, 20, 60)     # 均线窗口：A股最常用的三条（规则库按这三条写）
HL_WINDOW = 20               # 近 N 日高低：与 range_pos 因子同窗口，口径一致
LAST_BARS = 3                # 给 AI 看的最近 K 线根数（token 预算：3 根 ≈ 30 字符）
_MIN_BARS = max(MA_WINDOWS[1], HL_WINDOW)   # 少于 MA20/高低窗口则整只不出摘要


def _clean(bars: list[dict[str, Any]]) -> list[dict[str, float]]:
    """滤掉脏 K 线（0 收盘 / None / 非数值）。

    全市场池必踩：退市残留、停牌补 0、空字段。见 PITFALLS#11 —— `_annualized_vol`
    就曾因 0 收盘价 `math.log` 抛 domain error 穿透 executor.map 打崩整个选股。
    """
    out = []
    for b in bars:
        try:
            o, h, l, c = (float(b["open"]), float(b["high"]),
                          float(b["low"]), float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        out.append({"date": str(b.get("date") or ""),
                    "open": o, "high": h, "low": l, "close": c})
    return out


def _ma(closes: list[float], n: int) -> float | None:
    """N 日简单均线；不足 N 根返回 None（不拿现有的凑，那是编数据）。"""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


def digest(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """日K -> 结构摘要。数据不足返回 None。

    Args:
        bars: `ds.sina_kline()` 的返回，**时间正序** [{date,open,high,low,close,volume}]。

    Returns:
        {ma5, ma20, ma60, hi20, lo20, last:[最近3根 OHLC]}；不足 20 根返回 None。
        ma60 单独可为 None（不足 60 根），不连累 ma5/ma20。
    """
    rows = _clean(bars or [])
    if len(rows) < _MIN_BARS:
        return None
    closes = [r["close"] for r in rows]
    window = rows[-HL_WINDOW:]
    return {
        "ma5": _ma(closes, 5),
        "ma20": _ma(closes, 20),
        "ma60": _ma(closes, 60),
        "hi20": round(max(r["high"] for r in window), 2),
        "lo20": round(min(r["low"] for r in window), 2),
        "last": [{k: (v if k == "date" else round(v, 2)) for k, v in r.items()}
                 for r in rows[-LAST_BARS:]],
    }


def _md(date: str | None) -> str:
    """'2026-07-15' -> '0715:'。省掉年份（K线跨度只有 3 根，不会跨年歧义）。"""
    if not date or len(date) < 10:
        return ""
    return date[5:7] + date[8:10] + ":"


def fmt_stock(d: dict[str, Any] | None) -> str:
    """结构摘要 -> 紧凑单行（进候选股列表）。

    None 时明说「无K线」——让 AI 知道**缺的是什么**，而不是以为没这回事。
    """
    if not d:
        return "无K线数据"
    # `=` 不能省：MA5=8.01 写成 MA58.01 与「MA58」无法区分，AI 会读错每一条均线。
    ma = " ".join(f"{k.upper()}={d[k]}" for k in ("ma5", "ma20", "ma60") if d.get(k))
    # 「开-高-低-收」的字段说明由调用方在表头写**一次**，不在每行重复（×20 只纯浪费）。
    # **日期不能省**：盘中日K的最后一根是**昨天**（新浪当日 bar 收盘后才有），而候选行的
    # 「现价」是今天的实时价。不标日期，AI 会把昨天的 bar 当今天的信号棒读——这种错
    # 不报错，只是让它每一次判断都基于错位的那根。日期断档还能顺带暴露停牌股。
    bars = " ".join(f"{_md(b.get('date'))}{b['open']}-{b['high']}-{b['low']}-{b['close']}"
                    for b in d.get("last", []))
    return f"{ma} 20日高{d['hi20']}低{d['lo20']} 近{len(d.get('last', []))}根 {bars}"


def fmt_market(indices: list[dict[str, Any]], amount_yi: float | None,
               idx_struct: dict[str, dict | None], limit_up: int | None,
               limit_down: int | None, sectors: list[dict[str, Any]]) -> str:
    """大盘研判块 -> 多行文本（进决策提示词的【今日大盘】）。

    原先这里传的是**一个板块名**（`sector_ranking()[0]["sector"]`），AI 看到的
    「大盘」字面上就是「传媒」两个字，故 8/12 个 agent 报「无法判断市场状态」。

    Args:
        indices:    `ds.index_quotes()["indices"]`
        amount_yi:  两市成交额（亿）
        idx_struct: {指数名: digest()}，给市场状态识别用的指数 K 线结构
        limit_up/limit_down: 涨停/跌停家数（`ds.market_breadth()`，可为 None）
        sectors:    `universe_store.sector_ranking()` 的前几名
    """
    lines = []
    for i in indices:
        s = f"- {i['name']} {i['point']} ({i['chg_pct']:+.2f}%) 成交{i['amount_yi']}亿"
        st = idx_struct.get(i["name"])
        if st:
            lines.append(s + f"\n    {fmt_stock(st)}")
        else:
            lines.append(s)
    if amount_yi:
        lines.append(f"- 两市成交额 {amount_yi} 亿")
    if limit_up is not None or limit_down is not None:
        lines.append(f"- 涨停 {limit_up if limit_up is not None else '—'} 家 / "
                     f"跌停 {limit_down if limit_down is not None else '—'} 家")
    if sectors:
        strong = "、".join(f"{s['sector']}{s['avg_chg']:+.2f}%" for s in sectors[:5])
        lines.append(f"- 板块最强：{strong}")
    return "\n".join(lines) or "（大盘数据获取失败）"
