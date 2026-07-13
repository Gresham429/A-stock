"""L2：新闻/政策资讯库（SQLite，滚动 1 年）。

- `data/news.db`：表 news，UNIQUE(title_hash) 去重。
- 抓取：全市场快讯（财联社 + 东财 7×24）+ 自选股新闻 + 自选股研报；增量去重。
- 查询：按 板块/个股/kind/天数。清理 >1 年。
- `is_trading_day()`：供 launchd 脚本判断（工作日 + 内置节假日兜底，best-effort）。
- 可被后台线程 / `fetch_news.py` / HTTP 接口调用；东财源走 datasources.em_get 限流。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import datasources as ds
import store
import universe

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "news.db")
_LOCK = threading.Lock()

_POLICY_KW = ("政策", "央行", "证监会", "证监", "发改委", "国常会", "国务院", "财政部",
              "降准", "降息", "部委", "规划", "通知", "意见", "补贴", "减税", "工信部",
              "商务部", "监管", "新规", "税收", "关税")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, date TEXT, source TEXT,
  sector1 TEXT, sector2 TEXT, code TEXT,
  title TEXT, url TEXT, summary TEXT, kind TEXT,
  title_hash TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news(date);
CREATE INDEX IF NOT EXISTS idx_news_code ON news(code);
CREATE INDEX IF NOT EXISTS idx_news_sector1 ON news(sector1);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# 法定节假日按年动态抓取（holiday-cn，jsDelivr CDN 国内可达），缓存到 db，一年只抓一次
_HOLIDAY_URLS = (
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
)
_hol_cache: dict[int, set[str]] = {}


# ── DB 基础 ────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def _hash(title: str) -> str:
    return hashlib.sha1(title.strip().encode("utf-8")).hexdigest()


def _classify(title: str) -> str:
    return "政策" if any(k in title for k in _POLICY_KW) else "新闻"


def insert_many(rows: list[dict[str, Any]]) -> int:
    """批量入库（INSERT OR IGNORE 去重）。返回新增条数。"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.executemany(
            "INSERT OR IGNORE INTO news"
            "(ts,date,source,sector1,sector2,code,title,url,summary,kind,title_hash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(now, r.get("date", ""), r.get("source", ""), r.get("sector1"),
              r.get("sector2"), r.get("code"), r.get("title", ""), r.get("url", ""),
              r.get("summary", ""), r.get("kind", "新闻"), _hash(r.get("title", "")))
             for r in rows if r.get("title")])
        return c.total_changes - before


def query(sector: str = "", code: str = "", kind: str = "",
          days: int = 365, limit: int = 60) -> list[dict[str, Any]]:
    """按 板块(一级/二级) / 个股 / kind / 近 days 天 查询，按日期倒序。"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    where = ["date >= ?"]
    args: list[Any] = [cutoff]
    if code:
        where.append("code = ?"); args.append(code)
    if sector:
        where.append("(sector1 = ? OR sector2 = ?)"); args += [sector, sector]
    if kind:
        where.append("kind = ?"); args.append(kind)
    sql = ("SELECT date,source,sector1,sector2,code,title,url,summary,kind FROM news"
           f" WHERE {' AND '.join(where)} ORDER BY date DESC, id DESC LIMIT ?")
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def purge(days: int = 365) -> int:
    """删除 > days 天的旧数据。返回删除条数。"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM news WHERE date < ?", (cutoff,))
        return c.total_changes - before


def stats() -> dict[str, Any]:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        by_kind = {r[0]: r[1] for r in
                   c.execute("SELECT kind,COUNT(*) FROM news GROUP BY kind").fetchall()}
        rng = c.execute("SELECT MIN(date),MAX(date) FROM news").fetchone()
    return {"total": total, "by_kind": by_kind,
            "oldest": rng[0], "newest": rng[1], "last_fetch": get_meta("last_fetch")}


def get_meta(k: str) -> str:
    with _conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else ""


def set_meta(k: str, v: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ── 抓取 ──────────────────────────────────────────────────────────────────
def _full_date(mmdd_hhmm: str) -> str:
    """'07-13 21:52' → '2026-07-13'（快讯无年份，补当年）。"""
    m = re.match(r"(\d{2})-(\d{2})", mmdd_hhmm or "")
    return f"{datetime.now().year}-{m.group(1)}-{m.group(2)}" if m else date.today().isoformat()


def _market_rows(limit: int = 40) -> list[dict[str, Any]]:
    """全市场快讯（财联社 + 东财 7×24）→ 市场级新闻/政策。"""
    rows = []
    for n in ds.cls_telegraph(limit) + ds.eastmoney_global_news(limit):
        title = n.get("title", "")
        if not title:
            continue
        rows.append({"date": _full_date(n.get("time", "")), "source": n.get("source", ""),
                     "sector1": "市场", "sector2": None, "code": None,
                     "title": title, "url": n.get("url", ""), "kind": _classify(title)})
    return rows


def _stock_rows(code: str, page_size: int = 10) -> list[dict[str, Any]]:
    """单只自选股新闻 → 带一级/二级板块归属。"""
    p, s = universe.sector_of(code)
    rows = []
    for n in ds.stock_news(code, page_size=page_size):
        title = n.get("title", "")
        if not title:
            continue
        rows.append({"date": (n.get("date", "") or "")[:10], "source": n.get("source", ""),
                     "sector1": p, "sector2": s, "code": code,
                     "title": title, "url": "", "kind": _classify(title)})
    return rows


def _report_rows(code: str, page_size: int = 20) -> list[dict[str, Any]]:
    """单只自选股研报 → 基本面历史（kind=研报）。"""
    p, s = universe.sector_of(code)
    rows = []
    for r in ds.eastmoney_reports(code, page_size=page_size):
        title = r.get("title", "")
        if not title:
            continue
        rows.append({"date": r.get("date", ""), "source": r.get("org", ""),
                     "sector1": p, "sector2": s, "code": code,
                     "title": title, "url": r.get("pdf", ""),
                     "summary": f"{r.get('rating','')} EPS今{r.get('eps_this','')}/明{r.get('eps_next','')}",
                     "kind": "研报"})
    return rows


def _universe_slice(n: int = 15) -> list[str]:
    """从全市场候选池轮询取 n 只（游标存 db meta），让各板块持续有新闻、又不每次抓满 170 只。"""
    codes = universe.all_codes()
    if not codes:
        return []
    try:
        cur = int(get_meta("uni_cursor") or 0) % len(codes)
    except ValueError:
        cur = 0
    sl = codes[cur:cur + n]
    if len(sl) < n:  # 环形补齐
        sl += codes[:n - len(sl)]
    set_meta("uni_cursor", str((cur + n) % len(codes)))
    return sl


def fetch_incremental() -> dict[str, Any]:
    """增量：全市场快讯 + 自选股新闻 + 轮询一批全池龙头（覆盖各板块）。去重 + 清理 + 记 last_fetch。"""
    init()
    added = insert_many(_market_rows(40))
    seen: set[str] = set()
    for code in store.load_watchlist() + _universe_slice(15):
        if code in seen:
            continue
        seen.add(code)
        added += insert_many(_stock_rows(code, page_size=10))
    purged = purge(365)
    set_meta("last_fetch", datetime.now().isoformat(timespec="seconds"))
    logger.info("news 增量：+%d 条，清理 %d 条", added, purged)
    return {"added": added, "purged": purged}


def backfill(page_size: int = 50) -> dict[str, Any]:
    """一次性较重回填：**全市场候选池 170 只龙头**深翻页新闻（每个板块都有历史）+ 自选股研报。"""
    init()
    added = insert_many(_market_rows(60))
    for code in universe.all_codes():  # 全池龙头 → 覆盖全部一级/二级板块
        added += insert_many(_stock_rows(code, page_size=page_size))
    for code in store.load_watchlist():  # 自选股额外拉研报（基本面历史）
        added += insert_many(_report_rows(code, page_size=30))
    set_meta("last_backfill", datetime.now().isoformat(timespec="seconds"))
    logger.info("news 回填：+%d 条", added)
    return {"added": added}


def deepen(code: str) -> int:
    """L3：按需深抓单只股票的更久新闻 + 研报（本地稀疏时补历史）。返回新增条数。"""
    init()
    n = insert_many(_stock_rows(code, page_size=60))
    n += insert_many(_report_rows(code, page_size=40))
    logger.info("news 深抓 %s：+%d 条", code, n)
    return n


def _fetch_holidays(year: int) -> set[str]:
    """抓某年法定节假日（isOffDay=放假）→ 日期集合。失败返回空集。"""
    for tpl in _HOLIDAY_URLS:
        try:
            req = urllib.request.Request(tpl.format(year=year), headers={"User-Agent": ds.UA})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
            days = {x["date"] for x in data.get("days", []) if x.get("isOffDay") and x.get("date")}
            if days:
                return days
        except (OSError, ValueError) as e:
            logger.warning("交易日历 %s 抓取失败(%s): %s", year, tpl, e)
    return set()


def _holidays(year: int) -> set[str]:
    """某年法定节假日集合（内存 + db meta 双缓存，一年只抓一次）。"""
    if year in _hol_cache:
        return _hol_cache[year]
    cached = get_meta(f"holidays_{year}")
    if cached:
        try:
            s = set(json.loads(cached))
            _hol_cache[year] = s
            return s
        except ValueError:
            pass
    s = _fetch_holidays(year)
    if s:
        set_meta(f"holidays_{year}", json.dumps(sorted(s)))
    _hol_cache[year] = s
    return s


def is_trading_day(d: date | None = None) -> bool:
    """工作日且非法定节假日。节假日按年动态抓 holiday-cn 并缓存；抓不到则退化为纯工作日。"""
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in _holidays(d.year)
