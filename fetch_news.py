"""L2 抓取入口：供 launchd 定时 / 手动命令行调用（独立于 Flask 运行）。

用法：
  python3 fetch_news.py             # 增量（launchd 每日调用；含交易日门控）
  python3 fetch_news.py --backfill  # 一次性回填 1–2 季度 + 研报
  python3 fetch_news.py --force     # 忽略交易日门控强制增量

launchd 每日触发点（北京时间）：交易日 08:40 / 11:40 / 14:00 / 15:30 / 20:30；
非交易日只有晚间那次真抓——门控在本脚本里：非交易日且 <18 点直接跳过。
"""
import logging
import sys
from datetime import datetime

import news_store as ns

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    args = sys.argv[1:]
    if "--backfill" in args:
        print(ns.backfill())
        return
    force = "--force" in args
    # 交易日门控：非交易日只在晚间(>=18 点)那次真抓，盘前/盘中/盘后 no-op
    if not force and not ns.is_trading_day() and datetime.now().hour < 18:
        print("非交易日且非晚间，跳过增量抓取")
        return
    print(ns.fetch_incremental())


if __name__ == "__main__":
    main()
