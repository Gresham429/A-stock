"""复盘端到端编排：resolve 交易日 → 取数 → 体检闸 → 算硬指标 →（AI 研判+文稿）→ 落盘。

设计要点：
- 只复盘已收盘定稿场次（fetch.resolve_trade_date 逐日回探）。
- 体检闸：核心数据（涨停池）缺 → 硬拒，绝不拿空数据喂 AI（防编造）。
- AI 整块可降级：失败/未配 key → 保留硬指标照常落盘。
- history / prev_theme 从既有存档取，供 情绪周期 / 题材延续率。
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from . import fetch, llm_review, metrics, store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def run_review(date: Optional[str] = None, force: bool = False,
               with_ai: bool = True) -> dict:
    """跑一场复盘。返回结果 dict（含 status）。

    status: done(新算并落盘) / already(已有存档，未 force) / error(体检闸失败)。
    """
    target = fetch.resolve_trade_date(date)
    if not target:
        return {"status": "error", "error": "无法确定交易日（数据源被封/网络不通/端点变更）"}

    if not force:
        existing = store.load(target)
        if existing and store.usable(existing):
            logger.info("复盘 %s 已有存档，跳过（force=1 可重跑）", target)
            return {"status": "already", "target_date": target, "envelope": existing}

    dash = fetch.to_dash(target)
    logger.info("开始复盘 %s …", target)

    # ── 取数 ──
    zt = fetch.zt_pool(target)
    zb = fetch.zb_pool(target)
    dt = fetch.dt_pool(target)
    yzt = fetch.yzt_pool(target)
    theme = fetch.theme_reasons(target)
    lhb = fetch.dragon_tiger(dash)

    # ── 体检闸：核心数据缺则硬拒 ──
    if not zt:
        return {"status": "error", "target_date": target,
                "error": "核心数据（涨停池）为空——非交易日 / 盘后未定稿 / IP 被封。"
                         "不带病生成。稍后重试或换网络。"}

    warnings = []
    for name, val in (("炸板池", zb), ("跌停池", dt), ("昨日涨停池", yzt),
                      ("题材串", theme), ("龙虎榜", lhb)):
        if val is None:
            warnings.append(f"⚠️ {name}取数失败，相关指标降级")
    zb = zb or []
    dt = dt or []
    yzt = yzt or []
    theme = theme or []
    lhb = lhb or []

    # ── 硬指标（纯计算）──
    today_b = metrics.breadth(zt, zb, dt)
    th_boards = [t.get("boards", 0) for t in theme]  # 当日快照用同花顺，与回填同源
    today_snap = {"date": target,
                  "zt_count": len(theme) if theme else today_b["zt_count"],
                  "max_height": max(th_boards, default=today_b["max_height"]),
                  "break_rate": today_b["break_rate"]}
    hist = store.history(10, before=target) + [today_snap]  # 含当日作为周期曲线末点
    prev_theme = store.prev_theme(target)
    m = metrics.compute_all(zt, zb, dt, yzt, theme, history=hist, prev_theme=prev_theme)
    store.hist_upsert(target, today_snap)                   # 持久化，供次日/回填共用

    counts = {"zt": len(zt), "zb": len(zb), "dt": len(dt),
              "yzt": len(yzt), "theme": len(theme), "lhb": len(lhb)}

    # ── AI（可降级）──
    ai = None
    if with_ai:
        focus = llm_review.judge(m, counts, target)
        art = llm_review.article(m, counts, target, focus)
        if focus or art:
            ai = {"focus": focus, "article": art}

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "target_date": target,
        "target_date_dash": dash,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "warnings": warnings,
        "counts": counts,
        "metrics": m,
        "raw_theme": theme,      # 供次日题材延续率
        "ai": ai,
    }
    store.save(envelope)
    logger.info("复盘 %s 完成（涨停 %d · AI %s）", target, counts["zt"],
                "有" if ai else "无")
    return {"status": "done", "target_date": target, "envelope": envelope}


def latest_review() -> Optional[dict]:
    return store.latest()


def review_dates() -> list[str]:
    return store.dates()
