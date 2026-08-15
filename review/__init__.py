"""复盘自动化模块（/review）。

A股短线情绪「每日复盘」：收盘后自动出一份盘面复盘。
- fetch:    打板四池 + 题材串 + 龙虎榜 数据适配层（东财 push2ex / 同花顺 / 东财 datacenter）
- metrics:  情绪硬指标纯函数（赚钱效应/晋级率/连板梯队/情绪周期/题材/亏钱/封板质量）
- store:    每日复盘落盘 + purge
- llm_review: DeepSeek 5 分析师 + 复盘裁判 + 文稿（可降级）
- pipeline: 端到端编排 run_review(date)

边界：复盘=客观事实整理，只到板块层面、不荐个股、不给买卖点。
"""
from .pipeline import run_review, latest_review, review_dates

__all__ = ["run_review", "latest_review", "review_dates"]
