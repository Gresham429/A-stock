"""A股交易成本模型（单一事实源）。

费率**按券商/账户而异**，故不硬编码：存在投资画像里（画像 = 一档资金 = 一个券商账户），
`paper_store` 撮合、`portfolio` 盈亏、AI 提示词三处共用此模块，避免各算各的。

费率构成（2026-07 现行）：
    佣金    双向，按成交额收。多数券商有**最低值**（常见 5 元），但**部分账户免最低**
            （如国信金太阳万9免最低）。费率与是否免最低都因账户而异、可议价。
    印花税  **仅卖出**，千分之 0.5（2023-08-28 由千1 减半至千0.5）。国家收，不可议。
    过户费  双向，万分之 0.1（2022-04 起沪深统一）。不可议。

**有无最低佣金决定了能不能分批**，比费率本身更影响小资金：
`佣金 = max(成交额 × 费率, 最低值)`，故存在分界点 `最低值 / 费率`，单笔低于它时实际费率被抬高，
**分笔买入会成倍放大佣金**（4 笔各 2500 = 4×最低值，合并一笔只收一次）。
免最低时佣金完全线性，**拆单零惩罚**，可自由分批控制风险。

因此低费率不一定更省——取决于单笔金额：
    单笔 2500 元：万9免最低 = 2.25 元  <  万2.5+最低5 = 5 元
    单笔 1 万  ：万9免最低 = 9 元     >  万2.5+最低5 = 5 元
    分界点 = 最低值 / 高费率（万9 + 最低5 → 5556 元）
议价时**要同时争取低费率与免最低**，只降费率却带回最低值，对分批建仓反而更贵。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 市场常见档，仅作新建画像的初值——真实费率必须由用户按自己券商填写。
# 印花税/过户费是法定的，一般不需要改；佣金率务必改成自己的。
DEFAULT_COMMISSION_RATE = 0.00025  # 万2.5
DEFAULT_MIN_COMMISSION = 5.0       # 5 元
STAMP_RATE = 0.0005                # 卖出印花税 千0.5（法定）
TRANSFER_RATE = 0.00001            # 过户费 万0.1 双向（法定）


@dataclass(frozen=True)
class FeeSchedule:
    """一个券商账户的费率表。佣金可议价，印花税/过户费法定。"""

    commission_rate: float = DEFAULT_COMMISSION_RATE
    min_commission: float = DEFAULT_MIN_COMMISSION
    stamp_rate: float = STAMP_RATE
    transfer_rate: float = TRANSFER_RATE

    @property
    def has_min(self) -> bool:
        """是否有最低佣金。部分账户免最低（如万9免5元），此时拆单无额外成本。"""
        return self.min_commission > 0

    @property
    def min_amount_for_rate(self) -> float:
        """佣金下限分界点：单笔低于此金额时实付佣金被最低值抬高。免最低时返回 0。"""
        if self.commission_rate <= 0 or not self.has_min:
            return 0.0
        return round(self.min_commission / self.commission_rate, 0)

    def describe(self) -> str:
        """人读的一行摘要（供 UI 与 AI 提示词）。"""
        floor = f"(最低{self.min_commission:.0f}元)" if self.has_min else "(免最低)"
        return (f"佣金{self.commission_rate * 10000:.3g}‱{floor}"
                f" + 卖出印花{self.stamp_rate * 1000:.3g}‰ + 过户{self.transfer_rate * 10000:.3g}‱")


def compute(side: str, amount: float, sched: FeeSchedule | None = None) -> dict[str, float]:
    """单笔成交的费用明细。side: buy|sell；amount: 成交额(元)。

    返回 {commission, stamp, transfer, total, hit_min}；`hit_min` 标记是否触发最低佣金。
    """
    s = sched or FeeSchedule()
    if amount <= 0:
        return {"commission": 0.0, "stamp": 0.0, "transfer": 0.0, "total": 0.0, "hit_min": False}
    raw = amount * s.commission_rate
    commission = max(raw, s.min_commission)
    stamp = amount * s.stamp_rate if side == "sell" else 0.0
    transfer = amount * s.transfer_rate
    return {"commission": round(commission, 2), "stamp": round(stamp, 2),
            "transfer": round(transfer, 2),
            "total": round(commission + stamp + transfer, 2),
            "hit_min": raw < s.min_commission}


def total(side: str, amount: float, sched: FeeSchedule | None = None) -> float:
    """单笔总费用（元）。"""
    return compute(side, amount, sched)["total"]


def round_trip(amount: float, sched: FeeSchedule | None = None) -> dict[str, float]:
    """一轮完整往返（等额买入+卖出）的成本与占比——用于判断策略要先跑赢多少。"""
    buy = compute("buy", amount, sched)["total"]
    sell = compute("sell", amount, sched)["total"]
    tot = round(buy + sell, 2)
    return {"buy": buy, "sell": sell, "total": tot,
            "pct": round(tot / amount * 100, 3) if amount > 0 else 0.0}


def breakeven_pct(amount: float, sched: FeeSchedule | None = None) -> float:
    """保本涨幅(%)：买入后至少要涨这么多，卖出才不亏钱。"""
    return round_trip(amount, sched)["pct"]


def from_row(row: Any) -> FeeSchedule:
    """从画像行（sqlite3.Row / dict）取费率；字段缺失回落默认值。"""
    def g(k: str, d: float) -> float:
        try:
            v = row[k] if row is not None else None
        except (KeyError, IndexError, TypeError):
            return d
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d
    return FeeSchedule(
        commission_rate=g("commission_rate", DEFAULT_COMMISSION_RATE),
        min_commission=g("min_commission", DEFAULT_MIN_COMMISSION),
        stamp_rate=g("stamp_rate", STAMP_RATE),
        transfer_rate=g("transfer_rate", TRANSFER_RATE),
    )


def for_ai(sched: FeeSchedule, capital: float) -> str:
    """喂给 AI 的【交易成本】块：给具体数字，而非「注意手续费」这种空话。"""
    rt = round_trip(capital, sched)
    one = compute("buy", capital, sched)
    lines = [f"【交易成本（真实费率，每次买卖必然发生，判断买卖点时必须计入）】",
             f"- 费率：{sched.describe()}",
             f"- 以当前可用资金 {capital:.0f} 元单笔满仓计：买入费 {rt['buy']:.2f} 元，"
             f"卖出费 {rt['sell']:.2f} 元，**一轮往返 {rt['total']:.2f} 元 = {rt['pct']:.3f}%**",
             f"- **保本涨幅 {rt['pct']:.3f}%**：买入后涨幅不超过它就是亏损，不要建议博取小于此的价差"]
    if not sched.has_min:
        lines.append("- 该账户**免最低佣金**：佣金按成交额线性收取，"
                     "**分批建仓/减仓不产生额外佣金**（拆 4 笔与一次买入佣金相同），"
                     "故可自由分批控制风险，不必为省佣金而一次性满仓")
    elif one["hit_min"]:
        lines.append(f"- ⚠️ 该笔触发最低佣金 {sched.min_commission:.0f} 元"
                     f"（单笔低于 {sched.min_amount_for_rate:.0f} 元时实际费率被抬高）"
                     f"，**分笔买入会成倍放大佣金**，建议一次建仓而非拆单")
    else:
        lines.append(f"- 单笔低于 {sched.min_amount_for_rate:.0f} 元会触发最低佣金"
                     f"{sched.min_commission:.0f}元、抬高实际费率，拆单需谨慎")
    lines.append("- 频繁交易会被成本吃掉：每多做一轮就多付一次上述成本，"
                 "宁可少动、不可为小波动进出")
    return "\n".join(lines)
