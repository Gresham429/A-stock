"""情绪周期历史回填：爬近 N 个月每日情绪快照（涨停家数 / 最高连板 / 炸板率）。

只回填「既成事实」——已收盘日的涨停/炸板池不再变化，**无未来函数**（区别于不可补的
agent 决策/AI 复盘）。仅写 data/review/history.json（不生成完整复盘、不调 AI），
供 metrics.cycle_position 定位情绪周期。逐日探涨停池：空=非交易日跳过。

用法:
    python -m review.backfill            # 默认近 3 个月
    python -m review.backfill 6          # 近 6 个月
    python -m review.backfill --force    # 已有的也重爬
"""
from __future__ import annotations

import datetime
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review import fetch, store  # noqa: E402

logger = logging.getLogger(__name__)


def backfill(months: int = 3, force: bool = False) -> dict:
    """回填近 months 个月的情绪快照。返回统计 dict。"""
    start = datetime.date.today()
    span = int(months * 31)
    existing = store.hist_load()
    stats = {"scanned": 0, "filled": 0, "skipped": 0, "nontrading": 0, "failed": 0}

    day = start
    while (start - day).days <= span:
        if day.weekday() >= 5:      # 跳过周末
            day -= datetime.timedelta(days=1)
            continue
        compact = day.strftime("%Y%m%d")
        stats["scanned"] += 1
        day -= datetime.timedelta(days=1)  # 提前递减，下面 continue 都安全

        if not force and compact in existing:
            stats["skipped"] += 1
            continue
        # 同花顺涨停池（深 3 个月，单源一致）：zt_count=家数、max_height=最高连板
        th = fetch.theme_reasons(compact)
        if th is None:
            stats["failed"] += 1
            logger.warning("%s 同花顺涨停池取数失败，跳过（被封/网络）", compact)
            continue
        if not th:
            stats["nontrading"] += 1  # 非交易日
            continue
        n = len(th)
        snap = {"zt_count": n,
                "max_height": max((s.get("boards", 0) for s in th), default=0),
                "break_rate": 0}   # 炸板率深历史无源，不入周期
        store.hist_upsert(compact, snap)
        stats["filled"] += 1
        logger.info("回填 %s：涨停 %d 最高 %d 板", compact, n, snap["max_height"])
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    months = next((int(a) for a in sys.argv[1:] if a.isdigit()), 3)
    force = "--force" in sys.argv
    s = backfill(months, force=force)
    total = len(store.hist_load())
    print(f"[情绪周期回填] 扫描 {s['scanned']} · 填充 {s['filled']} · 已存跳过 {s['skipped']} "
          f"· 非交易日 {s['nontrading']} · 失败 {s['failed']} → history.json 现有 {total} 个交易日")
    return 0


if __name__ == "__main__":
    sys.exit(main())
