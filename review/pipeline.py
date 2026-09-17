"""复盘端到端编排：resolve 交易日 → 取数 → 体检闸 → 算硬指标 →（AI 研判+文稿）→ 落盘。

设计要点：
- 只复盘已收盘定稿场次（fetch.resolve_trade_date 逐日回探）。
- 体检闸：核心数据（涨停池）缺 → 硬拒，绝不拿空数据喂 AI（防编造）。
- AI 整块可降级：失败/未配 key → 保留硬指标照常落盘。
- history / prev_theme 从既有存档取，供 情绪周期 / 题材延续率。
- 跨进程互斥：文件锁 data/review/.running-<date>（gunicorn 多 worker + scheduler 互不可见，
  进程内的 app._review_job 挡不住双跑）。拿不到锁返回 status=running。
"""
from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Optional

import config

from . import fetch, llm_review, metrics, store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LOCK_STALE_SEC = 30 * 60   # 锁超过 30 分钟视为陈旧（进程崩溃未清理），可覆盖
AI_RETRY_AFTER_MIN = 20    # AI 整块降级（可重试类）后，至少隔这么久才自动重试


def _ai_retry_due(existing: dict, with_ai: bool) -> bool:
    """已有存档是否值得为了 AI 再跑一次。

    只在「AI 整块降级」且错误可重试时才重试：预算类当天重试必然同样失败（额度按自然日重置）。
    再加一个最小间隔，免得每个 10 分钟的调度心跳都去重取一轮打板数据。
    """
    if not with_ai or not existing.get("ai_degraded"):
        return False
    if existing.get("ai_error_kind") == "budget":
        return False
    ts = existing.get("generated_at") or ""
    try:
        age = datetime.datetime.now() - datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return age.total_seconds() / 60 >= AI_RETRY_AFTER_MIN


# ── 跨进程文件锁 ─────────────────────────────────────────────────────
def _lock_path(date: str) -> str:
    # 运行时读 store.REVIEW_DIR（不在 import 时绑定），测试可改目录
    return os.path.join(store.REVIEW_DIR, f".running-{date}")


def _lock_age(path: str) -> Optional[float]:
    """锁文件年龄（秒）。以文件内写的时间为准，解析失败退回 mtime；文件不存在返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            parts = f.read().split()
        ts = float(parts[1])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        try:
            ts = os.path.getmtime(path)
        except OSError:
            return None
    return time.time() - ts


def acquire_lock(date: str) -> bool:
    """原子创建 .running-<date>（O_CREAT|O_EXCL），写入 "pid time"。

    已存在且未陈旧返回 False；陈旧（超过 LOCK_STALE_SEC）则 warning 后删除重试一次。
    """
    os.makedirs(store.REVIEW_DIR, exist_ok=True)
    path = _lock_path(date)
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            age = _lock_age(path)
            if attempt == 1 or age is None or age < LOCK_STALE_SEC:
                return False
            logger.warning("复盘 %s 锁文件已 %.0f 分钟未释放，视为陈旧覆盖: %s",
                           date, age / 60, path)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {time.time():.0f}")
        return True
    return False


def release_lock(date: str) -> None:
    """删除锁文件；不存在也不报错。"""
    try:
        os.remove(_lock_path(date))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error("释放复盘锁 %s 失败: %s", date, e)


def is_running(date: str) -> bool:
    """是否有进程正在生成该场次（锁存在且未陈旧）。供 /api/review/status 合并显示。"""
    age = _lock_age(_lock_path(date))
    return age is not None and age < LOCK_STALE_SEC


def run_review(date: Optional[str] = None, force: bool = False,
               with_ai: bool = True) -> dict:
    """跑一场复盘。返回结果 dict（含 status）。

    status: done(新算并落盘) / already(已有存档，未 force) /
            running(别的进程正在生成该场次，本次跳过) / error(体检闸失败)。
    """
    target = fetch.resolve_trade_date(date)
    if not target:
        return {"status": "error", "error": "无法确定交易日（数据源被封/网络不通/端点变更）"}

    if not force:
        existing = store.load(target)
        if existing and store.usable(existing) and not _ai_retry_due(existing, with_ai):
            logger.info("复盘 %s 已有存档，跳过（force=1 可重跑）", target)
            return {"status": "already", "target_date": target, "envelope": existing}
        if existing and _ai_retry_due(existing, with_ai):
            logger.info("复盘 %s 上次 AI 整块降级（%s），本次重试补齐",
                        target, existing.get("ai_error_kind") or "api")

    # 存档检查之后、真正取数之前拿跨进程锁；结束（含异常）一定释放
    if not acquire_lock(target):
        logger.info("复盘 %s 已有进程在生成，本次跳过", target)
        return {"status": "running", "target_date": target,
                "error": "该场次正在由另一进程生成，稍后刷新即可"}
    try:
        return _run_locked(target, date, with_ai)
    finally:
        release_lock(target)


def _run_locked(target: str, date: Optional[str], with_ai: bool) -> dict:
    """持锁后的实际流程：取数 -> 体检闸 -> 硬指标 -> AI -> 落盘。"""
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

    # ── AI（可降级）：多角色分析师 → 裁判收敛 → 文稿 ──
    # 没配 key 时直接跳过：那不是「降级」，是这台机器就没开 AI，不该记成失败去重试。
    if with_ai and not config.llm_enabled():
        logger.info("未配置 DeepSeek key，本次只出硬指标")
        with_ai = False
    ai = None
    ai_degraded = False
    ai_error = ""
    ai_error_kind = ""
    if with_ai:
        leaders = [{"name": s["name"], "boards": s["limit_days"]}
                   for s in sorted(zt, key=lambda x: x["limit_days"], reverse=True)[:8]]
        # 板块资金流是实时快照（无历史）——仅在跑最新场次(date=None)时用，避免历史重跑取到实时值
        sector = fetch.sector_flow("concept", 15) if date is None else None
        analysts = llm_review.run_analysts(m, counts, target, lhb=lhb,
                                           leaders=leaders, sector_flow=sector)
        focus = llm_review.judge(m, counts, target, analyst_reports=analysts)
        art = llm_review.article(m, counts, target, focus)
        failed = [a for a in analysts if a.get("failed")]
        if focus or art or (analysts and not failed):
            ai = {"focus": focus, "article": art, "analysts": analysts}
        else:
            # 整块降级：不把占位文本当研判落盘（那看起来像一份完整复盘），
            # 只留硬指标加降级标记，供调度重试与前端提示。
            ai_degraded = True
            ai_error_kind = failed[0].get("error_kind") if failed else "api"
            ai_error = failed[0].get("report") if failed else "AI 未生成（分析师未运行）"

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
        "ai_degraded": ai_degraded,
        "ai_error": ai_error,
        "ai_error_kind": ai_error_kind,
    }
    store.save(envelope)
    logger.info("复盘 %s 完成（涨停 %d · AI %s）", target, counts["zt"],
                "有" if ai else ("降级" if ai_degraded else "无"))
    return {"status": "done", "target_date": target, "envelope": envelope,
            "degraded": ai_degraded, "error_kind": ai_error_kind,
            "error": ai_error or None}


def latest_review() -> Optional[dict]:
    return store.latest()


def review_dates() -> list[str]:
    return store.dates()
