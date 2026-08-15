"""情绪硬指标：纯函数、零网络、可离线单测。

输入 = fetch.py 规范化后的池子（zt/zb/dt/yzt/theme），输出 = 可直接渲染的指标 dict。
口径参考成熟短线复盘体系（赚钱效应看中位数、1进2 最敏感、梯队看断层）。
只算不判断——情绪档位由 AI 读这些读数自己定。
"""
from __future__ import annotations

from typing import Optional


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _rate(num: int, den: int) -> float:
    return round(num / den * 100, 1) if den else 0.0


# ── 宽度 / 温度 ───────────────────────────────────────────────────────
def breadth(zt: list[dict], zb: list[dict], dt: list[dict]) -> dict:
    """涨停/炸板/跌停家数 + 炸板率 + 最高连板 + 题材投机温度标签。"""
    n_zt, n_zb, n_dt = len(zt), len(zb), len(dt)
    br = round(n_zb / (n_zt + n_zb) * 100, 1) if (n_zt + n_zb) else 0.0
    max_h = max((s["limit_days"] for s in zt), default=0)
    if n_zt >= 80 and br < 30:
        temp = "亢奋"
    elif n_zt >= 50:
        temp = "活跃"
    elif n_zt >= 25:
        temp = "普通"
    else:
        temp = "冰点"
    return {"zt_count": n_zt, "zb_count": n_zb, "dt_count": n_dt,
            "break_rate": br, "max_height": max_h, "temp_tag": temp}


# ── 连板梯队 + 断层 ───────────────────────────────────────────────────
def ladder(zt: list[dict]) -> dict:
    """各档连板家数 + 断层检测（有高标却缺中间档 = 最高标悬空）。"""
    tiers: dict[int, int] = {}
    for s in zt:
        d = s["limit_days"]
        if d >= 1:
            tiers[d] = tiers.get(d, 0) + 1
    if not tiers:
        return {"tiers": {}, "highest": 0, "gaps": [], "continuous": True}
    highest = max(tiers)
    gaps = [d for d in range(2, highest) if d not in tiers]  # 2..highest-1 中缺的档
    return {"tiers": dict(sorted(tiers.items())), "highest": highest,
            "gaps": gaps, "continuous": not gaps}


# ── 赚钱效应（昨涨停股今表现）────────────────────────────────────────
def money_effect(yzt: list[dict]) -> dict:
    """赚钱效应 = 昨日涨停股今日 均值/中位数/翻红率/再涨停率。看中位数（均值易被大涨拉偏）。"""
    n = len(yzt)
    if not n:
        return {"n": 0, "avg": 0.0, "median": 0.0, "red_rate": 0.0, "again_rate": 0.0}
    pcts = [s["pct"] for s in yzt]
    red = sum(1 for p in pcts if p > 0)
    again = sum(1 for p in pcts if p >= 9.8)   # 今仍近涨停（10cm 制度近似）
    return {"n": n, "avg": round(sum(pcts) / n, 2), "median": round(_median(pcts), 2),
            "red_rate": _rate(red, n), "again_rate": _rate(again, n)}


# ── 晋级率（昨各档连板今是否仍封板）──────────────────────────────────
def promotion(yzt: list[dict], zt: list[dict]) -> dict:
    """晋级率 = 昨日各档连板今日仍封板的比例。今封板=昨票 code 出现在今日涨停池。
    1进2 最敏感。返回各档 base/promoted/rate + 总体。"""
    zt_codes = {s["code"] for s in zt}

    def _tier(pred) -> dict:
        base = [s for s in yzt if pred(s["y_limit_days"])]
        promoted = sum(1 for s in base if s["code"] in zt_codes)
        return {"base": len(base), "promoted": promoted, "rate": _rate(promoted, len(base))}

    b1 = _tier(lambda d: d == 1)
    b2 = _tier(lambda d: d == 2)
    b3 = _tier(lambda d: d >= 3)
    overall_base = b1["base"] + b2["base"] + b3["base"]
    overall_prom = b1["promoted"] + b2["promoted"] + b3["promoted"]
    return {"one_to_two": b1, "two_to_three": b2, "three_plus": b3,
            "overall_rate": _rate(overall_prom, overall_base)}


# ── 连板溢价（昨≥2板今承接）─────────────────────────────────────────
def consec_premium(yzt: list[dict]) -> dict:
    """连板溢价 = 昨日 ≥2 板个股今日 均值/中位数/翻红率（高标承接度）。"""
    hi = [s for s in yzt if s["y_limit_days"] >= 2]
    n = len(hi)
    if not n:
        return {"n": 0, "avg": 0.0, "median": 0.0, "red_rate": 0.0}
    pcts = [s["pct"] for s in hi]
    return {"n": n, "avg": round(sum(pcts) / n, 2), "median": round(_median(pcts), 2),
            "red_rate": _rate(sum(1 for p in pcts if p > 0), n)}


# ── 亏钱效应（昨涨停今大跌）─────────────────────────────────────────
def loss_effect(yzt: list[dict], dt: list[dict]) -> dict:
    """亏钱效应 = 昨日涨停股今日跌超5%/7%/跌停 家数与比例 + 最惨跌幅。"""
    n = len(yzt)
    if not n:
        return {"n": 0, "deep5": 0, "deep7": 0, "limit_down": 0,
                "deep5_rate": 0.0, "limit_down_rate": 0.0, "worst": 0.0,
                "market_limit_down": len(dt)}
    dt_codes = {s["code"] for s in dt}
    deep5 = sum(1 for s in yzt if s["pct"] <= -5)
    deep7 = sum(1 for s in yzt if s["pct"] <= -7)
    ld = sum(1 for s in yzt if s["code"] in dt_codes or s["pct"] <= -9.8)
    worst = min((s["pct"] for s in yzt), default=0.0)
    return {"n": n, "deep5": deep5, "deep7": deep7, "limit_down": ld,
            "deep5_rate": _rate(deep5, n), "limit_down_rate": _rate(ld, n),
            "worst": round(worst, 2), "market_limit_down": len(dt)}


# ── 封板质量 ─────────────────────────────────────────────────────────
def seal_quality(zt: list[dict]) -> dict:
    """封板质量：从未开板率 / 早盘封板(≤09:35) / 尾盘封板(≥14:30) / 平均炸板次数 / 曾开板率。"""
    n = len(zt)
    if not n:
        return {"n": 0, "never_broken_rate": 0.0, "opening": 0, "late": 0,
                "avg_broken_times": 0.0, "ever_opened_rate": 0.0}
    never = sum(1 for s in zt if s["break_times"] == 0)
    opening = sum(1 for s in zt if 0 < s["first_seal_int"] <= 93500)
    late = sum(1 for s in zt if s["first_seal_int"] >= 143000)
    ever = sum(1 for s in zt if s["break_times"] > 0)
    avg_bt = round(sum(s["break_times"] for s in zt) / n, 2)
    return {"n": n, "never_broken_rate": _rate(never, n), "opening": opening,
            "late": late, "avg_broken_times": avg_bt, "ever_opened_rate": _rate(ever, n)}


# ── 反馈矩阵（昨强势股按板位分档→今表现）────────────────────────────
def feedback_matrix(yzt: list[dict], zt: list[dict]) -> list[dict]:
    """昨日涨停股按昨连板档分组，看今日各档 晋级/收红/小跌/跌超5%/跌停 分布。"""
    zt_codes = {s["code"] for s in zt}
    buckets = [("1板", lambda d: d == 1), ("2板", lambda d: d == 2),
               ("3板+", lambda d: d >= 3)]
    rows = []
    for label, pred in buckets:
        grp = [s for s in yzt if pred(s["y_limit_days"])]
        n = len(grp)
        if not n:
            continue
        promoted = sum(1 for s in grp if s["code"] in zt_codes)
        red = sum(1 for s in grp if s["pct"] > 0)
        deep = sum(1 for s in grp if s["pct"] <= -5)
        rows.append({"tier": label, "n": n, "promoted": promoted,
                     "promote_rate": _rate(promoted, n), "red": red,
                     "red_rate": _rate(red, n), "deep5": deep})
    return rows


# ── 题材热点 ─────────────────────────────────────────────────────────
def theme_tree(theme: list[dict], prev_theme: Optional[list[dict]] = None) -> dict:
    """题材热点：按 reason 题材串拆 tag 聚合涨停家数，出主线。
    prev_theme（昨日题材串）给出时，附延续率 = 昨该题材涨停今仍涨停/昨该题材涨停。"""
    tag_count: dict[str, int] = {}
    for s in theme:
        for tag in _split_tags(s.get("reason", "")):
            tag_count[tag] = tag_count.get(tag, 0) + 1
    ranked = sorted(tag_count.items(), key=lambda kv: -kv[1])

    continuation = None
    if prev_theme:
        today_codes_by_tag: dict[str, set] = {}
        for s in theme:
            for tag in _split_tags(s.get("reason", "")):
                today_codes_by_tag.setdefault(tag, set()).add(s.get("code"))
        prev_codes_by_tag: dict[str, set] = {}
        for s in prev_theme:
            for tag in _split_tags(s.get("reason", "")):
                prev_codes_by_tag.setdefault(tag, set()).add(s.get("code"))
        continuation = {}
        for tag, prev_codes in prev_codes_by_tag.items():
            if len(prev_codes) < 2:
                continue
            still = len(prev_codes & today_codes_by_tag.get(tag, set()))
            continuation[tag] = _rate(still, len(prev_codes))

    return {"top": [{"theme": t, "count": c} for t, c in ranked[:12]],
            "distinct": len(tag_count), "continuation": continuation}


def _split_tags(reason: str) -> list[str]:
    if not reason:
        return []
    return [t.strip() for t in reason.replace("＋", "+").split("+") if t.strip()]


# ── 情绪周期（需历史序列）────────────────────────────────────────────
def cycle_position(history: list[dict]) -> dict:
    """情绪周期定位：近 N 日 (涨停家数 + 最高连板 + (1-炸板率)) 归一均值曲线，
    定位本轮低点与「第几天」。history=[{date,zt_count,max_height,break_rate}...] 旧→新。
    样本 < 3 时返回 available=False。"""
    if len(history) < 3:
        return {"available": False, "reason": "历史样本不足（需累积 ≥3 交易日）"}

    def _mm(vals: list[float]) -> list[float]:
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return [0.5] * len(vals)
        return [(v - lo) / (hi - lo) for v in vals]

    zt = _mm([h["zt_count"] for h in history])
    mh = _mm([h["max_height"] for h in history])
    br = _mm([h["break_rate"] for h in history])
    scores = [round((zt[i] + mh[i] + (1 - br[i])) / 3, 3) for i in range(len(history))]
    trough_idx = min(range(len(scores)), key=lambda i: scores[i])
    day_n = len(scores) - trough_idx  # 距低点第几天（含当日）
    rising = scores[-1] > scores[trough_idx]
    return {"available": True, "score": scores[-1], "day_n": day_n,
            "trough_date": history[trough_idx]["date"], "rising": rising,
            "curve": [{"date": history[i]["date"], "score": scores[i]}
                      for i in range(len(history))]}


# ── 汇总 ─────────────────────────────────────────────────────────────
def compute_all(zt: list[dict], zb: list[dict], dt: list[dict], yzt: list[dict],
                theme: list[dict], history: Optional[list[dict]] = None,
                prev_theme: Optional[list[dict]] = None) -> dict:
    """一次算齐全部硬指标。history/prev_theme 为空时对应块降级（标 available=False）。"""
    return {
        "breadth": breadth(zt, zb, dt),
        "ladder": ladder(zt),
        "money_effect": money_effect(yzt),
        "promotion": promotion(yzt, zt),
        "consec_premium": consec_premium(yzt),
        "loss_effect": loss_effect(yzt, dt),
        "seal_quality": seal_quality(zt),
        "feedback_matrix": feedback_matrix(yzt, zt),
        "theme_tree": theme_tree(theme, prev_theme),
        "cycle_position": cycle_position(history or []),
    }
