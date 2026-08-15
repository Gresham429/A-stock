"""复盘情绪硬指标离线单测（零依赖、不打网络）。

覆盖：宽度/温度 · 连板梯队+断层 · 赚钱效应(中位数) · 晋级率(1进2) ·
连板溢价 · 亏钱效应 · 封板质量 · 反馈矩阵 · 题材热点 · 情绪周期(含样本不足降级)。
运行：python3 tests/test_review_metrics.py
"""
import os
import sys

# 直接加载 review/metrics.py（纯函数、无包内依赖），不经过 review/__init__，
# 使本测试独立于 fetch/llm/pipeline（它们会碰网络/DeepSeek）。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "review"))

import metrics as M  # noqa: E402

# ── fixtures（规范化后的池子形状，人工构造、结果可手算）──────────────
ZT = [
    {"code": "A", "name": "甲", "limit_days": 1, "break_times": 0, "first_seal_int": 92500},
    {"code": "B", "name": "乙", "limit_days": 1, "break_times": 1, "first_seal_int": 101000},
    {"code": "C", "name": "丙", "limit_days": 2, "break_times": 0, "first_seal_int": 93000},
    {"code": "D", "name": "丁", "limit_days": 3, "break_times": 0, "first_seal_int": 143500},
    {"code": "E", "name": "戊", "limit_days": 5, "break_times": 2, "first_seal_int": 110000},
]
ZB = [{"code": "Z1"}, {"code": "Z2"}]
DT = [{"code": "X4"}, {"code": "DT2"}]
YZT = [
    {"code": "A", "y_limit_days": 1, "pct": 10.2},
    {"code": "X1", "y_limit_days": 1, "pct": -3.0},
    {"code": "X2", "y_limit_days": 1, "pct": 2.0},
    {"code": "C", "y_limit_days": 2, "pct": 10.1},
    {"code": "X3", "y_limit_days": 2, "pct": -5.5},
    {"code": "E", "y_limit_days": 3, "pct": 10.0},
    {"code": "X4", "y_limit_days": 1, "pct": -9.9},
]
THEME = [
    {"code": "A", "reason": "AI+算力"},
    {"code": "C", "reason": "AI+机器人"},
    {"code": "E", "reason": "机器人"},
    {"code": "B", "reason": ""},
]
HISTORY = [
    {"date": "d1", "zt_count": 20, "max_height": 3, "break_rate": 40},
    {"date": "d2", "zt_count": 10, "max_height": 2, "break_rate": 60},
    {"date": "d3", "zt_count": 30, "max_height": 4, "break_rate": 20},
    {"date": "d4", "zt_count": 63, "max_height": 5, "break_rate": 23},
]

_passed = 0


def check(name, got, exp):
    global _passed
    ok = got == exp
    print(f"  {'✅' if ok else '❌'} {name}: got={got} exp={exp}")
    assert ok, f"{name}: {got} != {exp}"
    _passed += 1


print("== breadth ==")
b = M.breadth(ZT, ZB, DT)
check("zt_count", b["zt_count"], 5)
check("break_rate 2/(5+2)", b["break_rate"], 28.6)
check("max_height", b["max_height"], 5)
check("temp 冰点(<25)", b["temp_tag"], "冰点")

print("== ladder + 断层 ==")
ld = M.ladder(ZT)
check("tiers", ld["tiers"], {1: 2, 2: 1, 3: 1, 5: 1})
check("highest", ld["highest"], 5)
check("gaps 缺4板", ld["gaps"], [4])
check("continuous", ld["continuous"], False)

print("== money_effect（看中位数）==")
me = M.money_effect(YZT)
check("n", me["n"], 7)
check("median", me["median"], 2.0)
check("avg", me["avg"], 1.99)
check("red_rate 4/7", me["red_rate"], 57.1)
check("again_rate 3/7", me["again_rate"], 42.9)

print("== promotion（1进2 最敏感）==")
pr = M.promotion(YZT, ZT)
check("1进2 base", pr["one_to_two"]["base"], 4)
check("1进2 promoted", pr["one_to_two"]["promoted"], 1)
check("1进2 rate", pr["one_to_two"]["rate"], 25.0)
check("2进3 rate", pr["two_to_three"]["rate"], 50.0)
check("3板+ rate", pr["three_plus"]["rate"], 100.0)
check("overall 3/7", pr["overall_rate"], 42.9)

print("== consec_premium ==")
cp = M.consec_premium(YZT)
check("n(≥2板)", cp["n"], 3)
check("median", cp["median"], 10.0)
check("red_rate 2/3", cp["red_rate"], 66.7)

print("== loss_effect ==")
le = M.loss_effect(YZT, DT)
check("deep5", le["deep5"], 2)
check("deep7", le["deep7"], 1)
check("limit_down", le["limit_down"], 1)
check("worst", le["worst"], -9.9)
check("market_limit_down", le["market_limit_down"], 2)

print("== seal_quality ==")
sq = M.seal_quality(ZT)
check("never_broken_rate 3/5", sq["never_broken_rate"], 60.0)
check("opening ≤09:35", sq["opening"], 2)
check("late ≥14:30", sq["late"], 1)
check("avg_broken_times", sq["avg_broken_times"], 0.6)
check("ever_opened_rate 2/5", sq["ever_opened_rate"], 40.0)

print("== feedback_matrix ==")
fm = M.feedback_matrix(YZT, ZT)
check("档数", len(fm), 3)
check("1板 n", fm[0]["n"], 4)
check("1板 promote_rate", fm[0]["promote_rate"], 25.0)

print("== theme_tree ==")
tt = M.theme_tree(THEME)
check("distinct tags", tt["distinct"], 3)
top = {d["theme"]: d["count"] for d in tt["top"]}
check("AI count", top["AI"], 2)
check("机器人 count", top["机器人"], 2)
check("continuation None(无昨日)", tt["continuation"], None)

print("== cycle_position ==")
cy = M.cycle_position(HISTORY)
check("available", cy["available"], True)
check("trough_date", cy["trough_date"], "d2")
check("day_n 距低点第3天", cy["day_n"], 3)
check("rising", cy["rising"], True)
check("样本不足降级", M.cycle_position(HISTORY[:2])["available"], False)

print(f"\n🎉 全部通过：{_passed} 断言")
