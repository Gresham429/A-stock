"""自选股清单持久化：读写本地 watchlist.json。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import userctx

logger = logging.getLogger(__name__)

# 测试钩子：设成某个 Path 即全局覆盖（tests/test_portfolio.py 用它做隔离）。
# 留 None ＝按当前登录用户解析，生产时走的是这一支。
WATCHLIST_PATH: Path | None = None


def _path() -> Path:
    """当前登录用户的 watchlist.json（多用户隔离，见 userctx.py）。"""
    return WATCHLIST_PATH or Path(userctx.user_path("watchlist.json"))


# 首次运行时的默认自选股（第一轮分析筛出的 6 只）
DEFAULT_CODES = ["002415", "300059", "002241", "000938", "002049", "002475"]


def load_watchlist() -> list[str]:
    """读取自选股代码列表；文件不存在时用默认列表初始化。"""
    if not _path().exists():
        save_watchlist(DEFAULT_CODES)
        return list(DEFAULT_CODES)
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        codes = data.get("codes", []) if isinstance(data, dict) else data
        return [str(c) for c in codes]
    except (json.JSONDecodeError, OSError) as e:
        logger.error("读取 watchlist 失败，回退默认列表: %s", e)
        return list(DEFAULT_CODES)


def save_watchlist(codes: list[str]) -> None:
    """写入自选股代码列表（去重保序）。"""
    seen: set[str] = set()
    unique = [c for c in codes if not (c in seen or seen.add(c))]
    _path().write_text(
        json.dumps({"codes": unique}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def add_code(code: str) -> list[str]:
    """新增一只股票，返回更新后的列表。"""
    codes = load_watchlist()
    if code not in codes:
        codes.append(code)
        save_watchlist(codes)
    return codes


def remove_code(code: str) -> list[str]:
    """移除一只股票，返回更新后的列表。"""
    codes = [c for c in load_watchlist() if c != code]
    save_watchlist(codes)
    return codes
