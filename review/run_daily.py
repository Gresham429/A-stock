"""每日复盘批处理入口——收盘后由定时器（systemd/cron/launchd）或命令行触发。

用法:
    python -m review.run_daily                # 复盘最近已收盘交易日
    python -m review.run_daily 20260814       # 指定日期
    python -m review.run_daily --force        # 已存档也重跑
    python -m review.run_daily --no-ai        # 只算硬指标，不调 DeepSeek

退出码：0=done/already，3=体检闸失败（核心数据缺）。供定时器判成败/告警。
"""
from __future__ import annotations

import logging
import os
import sys

# 允许直接 `python review/run_daily.py` 运行（把仓库根加进 path）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review import run_review  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    date = pos[0] if pos else None
    force = "--force" in sys.argv
    with_ai = "--no-ai" not in sys.argv
    r = run_review(date, force=force, with_ai=with_ai)
    status = r.get("status")
    print(f"[复盘] status={status} date={r.get('target_date')} "
          f"{r.get('error') or ''}".rstrip())
    if status == "done":
        c = (r.get("envelope") or {}).get("counts", {})
        print(f"       涨停{c.get('zt')} 炸板{c.get('zb')} 跌停{c.get('dt')} "
              f"昨涨停{c.get('yzt')} 题材{c.get('theme')} 龙虎榜{c.get('lhb')}")
    return 0 if status in ("done", "already") else 3


if __name__ == "__main__":
    sys.exit(main())
