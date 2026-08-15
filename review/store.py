"""每日复盘落盘：data/review/<date>.json + latest.json（原子写）+ 历史序列 + purge。

- 只有裁判真收敛（有 focus 或 硬指标齐全）才算 usable，避免「成功但空」覆盖旧好文件。
- history() 供 metrics.cycle_position 用（近 N 日 涨停家数/最高连板/炸板率 序列）。
- 按 CLAUDE.md 约定：按日累积必须有 purge()。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_DIR = os.path.join(_ROOT, "data", "review")
_LATEST = os.path.join(REVIEW_DIR, "latest.json")
HISTORY_FILE = os.path.join(REVIEW_DIR, "history.json")  # 每日情绪快照序列（供 cycle_position）
KEEP_DAYS = 365
HIST_CAP = 400


def _ensure_dir() -> None:
    os.makedirs(REVIEW_DIR, exist_ok=True)


def _path(date: str) -> str:
    return os.path.join(REVIEW_DIR, f"{date}.json")


def _atomic_write(path: str, obj: dict) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=REVIEW_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def usable(envelope: dict) -> bool:
    """裁判真收敛 或 至少硬指标齐全（涨停池非空）才算可落盘。"""
    if (envelope.get("ai") or {}).get("focus"):
        return True
    return bool((envelope.get("metrics") or {}).get("breadth", {}).get("zt_count"))


def save(envelope: dict) -> bool:
    """落盘一份复盘。usable 才写，并更新 latest.json。返回是否写入。"""
    date = envelope.get("target_date")
    if not date:
        logger.error("save: envelope 缺 target_date")
        return False
    if not usable(envelope):
        logger.warning("复盘 %s 不 usable（无 focus 且涨停池空），拒绝落盘", date)
        return False
    _atomic_write(_path(date), envelope)
    _atomic_write(_LATEST, envelope)
    purge()
    logger.info("复盘 %s 已落盘", date)
    return True


def load(date: str) -> Optional[dict]:
    p = _path(date.replace("-", ""))
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.error("读取复盘 %s 失败: %s", date, e)
        return None


def latest() -> Optional[dict]:
    if not os.path.exists(_LATEST):
        return None
    try:
        with open(_LATEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.error("读取 latest 复盘失败: %s", e)
        return None


def dates() -> list[str]:
    """有存档的交易日（YYYYMMDD），新→旧。"""
    if not os.path.isdir(REVIEW_DIR):
        return []
    out = [f[:-5] for f in os.listdir(REVIEW_DIR)
           if f.endswith(".json") and f[:-5].isdigit()]
    return sorted(out, reverse=True)


def hist_load() -> dict:
    """读情绪历史序列 {date: {zt_count,max_height,break_rate}}。"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def hist_upsert(date: str, snap: dict) -> None:
    """写入/更新某交易日情绪快照到 history.json（容量上限 HIST_CAP）。"""
    _ensure_dir()
    h = hist_load()
    h[date] = {"zt_count": snap.get("zt_count", 0),
               "max_height": snap.get("max_height", 0),
               "break_rate": snap.get("break_rate", 0)}
    if len(h) > HIST_CAP:
        for d in sorted(h)[:-HIST_CAP]:
            del h[d]
    _atomic_write(HISTORY_FILE, h)


def history(n: int = 10, before: Optional[str] = None) -> list[dict]:
    """近 n 个交易日情绪序列（旧→新），**严格早于 before**（不含当日；当日由 pipeline 追加为曲线末点）。"""
    h = hist_load()
    ds = sorted(d for d in h if (before is None or d < before))
    ds = ds[-n:]
    return [{"date": d, **h[d]} for d in ds]


def prev_theme(before: str) -> Optional[list[dict]]:
    """上一个存档交易日的题材串（供题材延续率）。无则 None。"""
    ds = [d for d in sorted(dates()) if d < before]
    if not ds:
        return None
    env = load(ds[-1])
    return (env or {}).get("raw_theme")


def purge(keep_days: int = KEEP_DAYS) -> int:
    """只保留最近 keep_days 个交易日的存档，返回删除数。"""
    ds = dates()
    stale = ds[keep_days:]
    removed = 0
    for d in stale:
        try:
            os.remove(_path(d))
            removed += 1
        except OSError:
            pass
    if removed:
        logger.info("purge 复盘存档：删除 %d 个过期（保留 %d）", removed, keep_days)
    return removed
