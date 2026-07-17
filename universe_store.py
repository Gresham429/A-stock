"""全市场股票池（SQLite `data/universe.db`）：全 A 名单 + 板块归属 + 板块日变化统计。

替代 `universe.py` 手工维护的 170 只龙头池（后者降级为「精选龙头」标记 + 回填未完成时的降级路径）。

数据源（2026-07-15 实测）：
    名单 + 快照 -> 新浪 hs_a（5527 只，70 页并发 ~12s，自带涨跌幅/成交额/换手/市值）
    板块归属   -> 东财 slist 逐股（0.2s/股，走 em_get 限流 ≥1s）
                  注：东财按端点封 IP —— clist（批量出数）封、slist（逐股）通，故只能逐股回填。

设计见 plan/2026-07-15-full-market-universe-design.md。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

import datasources as ds
import universe

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "universe.db")
_LOCK = threading.Lock()
_backfill_lock = threading.Lock()      # 板块归属回填（逐股 slist）单飞
_sd_backfill_lock = threading.Lock()   # 板块日变化历史回填单飞（与归属回填独立，互不阻塞）

# 申万一级 31 类（2021 版）——硬编码锚点。slist 标签顺序不稳定、Ⅱ/Ⅲ 后缀只有部分股票带，
# 靠顺序/后缀解析不可靠；用这份闭集匹配实测 12/12 命中。
SW1_SET = frozenset({
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产", "商贸零售",
    "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备", "国防军工", "计算机", "传媒",
    "通信", "银行", "非银金融", "机械设备", "煤炭", "石油石化", "环保", "美容护理",
})

OTHER = "其他"
_SUB_SCAN = 4          # 细分只在前 N 个标签里找（再往后是概念/地域/指数）
_HEARTBEAT_STALE = 120  # 回填心跳超过该秒数视为进程已死、锁可抢占
SECTOR_KEEP_DAYS = 365  # 板块日统计滚动保留天数（977 板块 ≈ 24 万行/年 ≈ 82MB，不清则无限涨）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks(
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  board TEXT DEFAULT '',
  sw1  TEXT DEFAULT '',
  sub  TEXT DEFAULT '',
  price REAL DEFAULT 0,
  float_mcap REAL DEFAULT 0,
  is_st INTEGER DEFAULT 0,
  is_suspended INTEGER DEFAULT 0,
  is_leader INTEGER DEFAULT 0,
  eligible INTEGER DEFAULT 1,
  sectors_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stocks_sw1 ON stocks(sw1);
CREATE INDEX IF NOT EXISTS idx_stocks_elig ON stocks(eligible);
CREATE TABLE IF NOT EXISTS stock_sectors(
  code TEXT NOT NULL,
  sector TEXT NOT NULL,
  kind TEXT NOT NULL,
  PRIMARY KEY (code, sector)
);
CREATE INDEX IF NOT EXISTS idx_ss_sector ON stock_sectors(sector);
CREATE TABLE IF NOT EXISTS sector_daily(
  date TEXT NOT NULL, sector TEXT NOT NULL, kind TEXT DEFAULT '',
  n INTEGER DEFAULT 0, avg_chg REAL DEFAULT 0,
  up_n INTEGER DEFAULT 0, down_n INTEGER DEFAULT 0, limit_up_n INTEGER DEFAULT 0,
  amount REAL DEFAULT 0,
  leader_code TEXT DEFAULT '', leader_name TEXT DEFAULT '', leader_chg REAL DEFAULT 0,
  PRIMARY KEY (date, sector)
);
CREATE INDEX IF NOT EXISTS idx_sd_date ON sector_daily(date);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


# ── DB 基础 ────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── 代码分类 helper ────────────────────────────────────────────────────────
def is_bj(code: str) -> bool:
    """北交所：8xxxxx / 4xxxxx / 920xxx（920 为 2024 年起的新号段）。

    与 `ds.market_prefix(code) == "bj"` 等价，测试固化了该不变量；
    此处独立实现是为了语义清晰（判交易所归属，不关心 URL 前缀）。
    """
    return code.startswith(("4", "8", "92"))


def board_of(code: str) -> str:
    if is_bj(code):
        return "bj"
    if code.startswith("688"):
        return "kcb"
    if code.startswith("30"):
        return "cyb"
    if code.startswith("6"):
        return "sh"
    return "sz"


def _kind_of(tag: str) -> str:
    if tag in SW1_SET:
        return "sw1"
    if tag.endswith("板块"):
        return "region"
    if tag.endswith("_"):
        return "index"
    return "concept"


def parse_tags(tags: list[str]) -> tuple[str, str, list[tuple[str, str]]]:
    """东财 slist 混合标签 -> (申万一级, 细分, [(板块名, kind)])。

    细分不严格等于申万二级（中芯国际得到「集成电路制造」而申万二级是「半导体」），
    但语义正确、自解释，作细分方向标签够用。故对外措辞用「细分」而非「申万二级」。
    """
    # 先去后缀再判定：东财同一板块会以 '银行Ⅱ'/'银行' 两种形态出现，
    # 若按原始标签分类，'银行Ⅱ' 会被判成 concept 并抢占去重位，真正的 sw1 标记就永远进不去。
    cleans = [t.rstrip("ⅡⅢ") for t in tags]
    sw1 = next((c for c in cleans if c in SW1_SET), "")
    sub = ""
    for c in cleans[:_SUB_SCAN]:
        if c in SW1_SET or c.endswith("板块") or c.endswith("_"):
            continue
        sub = c
        break
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for c in cleans:
        if not c or c in seen:
            continue
        seen.add(c)
        # sw1/sub 判定优先于通用分类，避免细分被误标成 concept
        kind = "sw1" if c == sw1 else "sub" if c == sub else _kind_of(c)
        uniq.append((c, kind))
    if sub and sub not in seen:
        uniq.append((sub, "sub"))
    return sw1 or OTHER, sub or (sw1 or OTHER), uniq


# ── 名单刷新 ───────────────────────────────────────────────────────────────
def refresh_roster() -> int:
    """全 A 名单刷新（新浪 hs_a）。返回写入只数；源失败返回 0 且不动 db。"""
    rows = ds.sina_all_stocks()
    if not rows:
        logger.warning("全市场名单刷新失败：新浪返回空，保留 db 旧名单")
        return 0
    leaders = set(universe.all_codes())
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = []
    for r in rows:
        code, name = r["code"], r["name"]
        is_st = 1 if "ST" in name.upper() else 0
        susp = 1 if r["price"] <= 0 else 0
        elig = 0 if (is_st or susp or is_bj(code)) else 1
        payload.append((code, name, board_of(code), r["price"], r["float_mcap"],
                        is_st, susp, 1 if code in leaders else 0, elig, now))
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO stocks(code,name,board,price,float_mcap,is_st,is_suspended,"
            "is_leader,eligible,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, board=excluded.board, "
            "price=excluded.price, float_mcap=excluded.float_mcap, is_st=excluded.is_st, "
            "is_suspended=excluded.is_suspended, is_leader=excluded.is_leader, "
            "eligible=excluded.eligible, updated_at=excluded.updated_at",
            payload)
        c.execute("INSERT INTO meta(k,v) VALUES('roster_at',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (now,))
    logger.info("全市场名单刷新: %d 只（eligible %d）",
                len(payload), sum(1 for p in payload if p[8]))
    return len(payload)


# ── 板块归属回填 ───────────────────────────────────────────────────────────
def pending_sector_codes(eligible_only: bool = True) -> list[str]:
    sql = "SELECT code FROM stocks WHERE sectors_at='' "
    if eligible_only:
        sql += "AND eligible=1 "
    sql += "ORDER BY code"
    with _conn() as c:
        return [r["code"] for r in c.execute(sql)]


def _claim_backfill() -> bool:
    """跨进程抢锁：db 里写 pid + 心跳。心跳 <_HEARTBEAT_STALE 秒视为他人在跑。

    必须跨进程——`ds.em_get` 的限流器是进程内全局（`_em_last_call`），
    两个进程同时回填会让东财实际请求速率翻倍；东财已因此封掉 clist 端点，
    slist 再被封则整个板块归属功能失效。
    """
    now = time.time()
    with _LOCK, _conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k='backfill_hb'").fetchone()
        if row:
            try:
                pid_s, ts_s = row["v"].split(",")
                if int(pid_s) != os.getpid() and now - float(ts_s) < _HEARTBEAT_STALE:
                    return False
            except (ValueError, AttributeError):
                pass  # 心跳格式坏了，视为无主
        c.execute("INSERT INTO meta(k,v) VALUES('backfill_hb',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (f"{os.getpid()},{now}",))
    return True


def _beat() -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE meta SET v=? WHERE k='backfill_hb'", (f"{os.getpid()},{time.time()}",))


def _release_backfill() -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM meta WHERE k='backfill_hb'")


def backfill_sectors(limit: int | None = None, eligible_only: bool = True) -> int:
    """逐股回填板块归属（东财 slist，走 em_get 限流 ≥1s）。可断点续传：只抓 sectors_at 为空的。

    全量 ~4989 只 eligible × ~1.25s ≈ 100 分钟，故设计为后台线程跑。
    回填期间 sector_of() 自动降级到手工池，功能不中断。
    线程锁 + db 心跳双重互斥：前者挡同进程并发，后者挡多进程（app 与命令行同时跑）。
    """
    if not _backfill_lock.acquire(blocking=False):
        logger.info("板块回填已在本进程运行，跳过")
        return 0
    try:
        if not _claim_backfill():
            logger.info("板块回填已由其他进程运行（db 心跳未过期），跳过")
            return 0
        todo = pending_sector_codes(eligible_only)
        if limit:
            todo = todo[:limit]
        if not todo:
            _release_backfill()
            return 0
        logger.info("板块归属回填开始：%d 只待抓（预计 %.0f 分钟）", len(todo), len(todo) * 1.25 / 60)
        done = 0
        try:
            for i, code in enumerate(todo, 1):
                tags = ds.concept_tags(code, limit=30)
                if not tags:
                    continue
                sw1, sub, pairs = parse_tags(tags)
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with _LOCK, _conn() as c:
                    c.execute("UPDATE stocks SET sw1=?, sub=?, sectors_at=? WHERE code=?",
                              (sw1, sub, now, code))
                    c.executemany(
                        "INSERT OR IGNORE INTO stock_sectors(code,sector,kind) VALUES(?,?,?)",
                        [(code, s, k) for s, k in pairs])
                done += 1
                if i % 100 == 0:
                    _beat()
                if i % 200 == 0:
                    logger.info("板块回填进度 %d/%d", i, len(todo))
        finally:
            _release_backfill()
        logger.info("板块归属回填完成：%d/%d 只", done, len(todo))
        return done
    finally:
        _backfill_lock.release()


# ── 查询（对外契约与 universe.py 一致） ────────────────────────────────────
def _ready() -> bool:
    """db 是否已有可用名单（否则全部降级到手工池）。"""
    try:
        with _conn() as c:
            return (c.execute("SELECT COUNT(*) n FROM stocks").fetchone()["n"] or 0) > 0
    except sqlite3.Error:
        return False


def sector_of(code: str) -> tuple[str, str]:
    """代码 -> (申万一级, 细分)。未回填/查不到时降级手工池，再落 ('其他','其他')。

    签名与语义与 `universe.sector_of` 一致，news_store / app.py 无需改动。
    """
    try:
        with _conn() as c:
            row = c.execute("SELECT sw1, sub FROM stocks WHERE code=?", (code,)).fetchone()
        if row and row["sw1"]:
            return row["sw1"], row["sub"] or row["sw1"]
    except sqlite3.Error as e:
        logger.warning("sector_of 查询失败 %s: %s", code, e)
    p, s = universe.sector_of(code)
    return (p, s) if p != OTHER else (OTHER, OTHER)


def sectors_map(codes: list[str]) -> dict[str, tuple[str, str]]:
    """批量 code -> (一级, 细分)，一次查询。

    `_balanced_pick` 会对候选池每只股查板块；全市场 ~5000 只若逐只走 sector_of()
    就是 5000 次 DB 查询，必须批量。未回填/查不到的降级手工池，与 sector_of 一致。
    """
    if not codes:
        return {}
    out: dict[str, tuple[str, str]] = {}
    try:
        with _conn() as c:
            for i in range(0, len(codes), 900):  # SQLite 变量数上限 999
                chunk = codes[i:i + 900]
                ph = ",".join("?" * len(chunk))
                for r in c.execute(
                        f"SELECT code, sw1, sub FROM stocks WHERE code IN ({ph}) AND sw1!=''",
                        chunk):
                    out[r["code"]] = (r["sw1"], r["sub"] or r["sw1"])
    except sqlite3.Error as e:
        logger.warning("sectors_map 查询失败: %s", e)
    for c_ in codes:  # 未回填的降级手工池
        if c_ not in out:
            out[c_] = universe.sector_of(c_)
    return out


def codes_of(focus: str = "", eligible_only: bool = True) -> list[str]:
    """板块名（申万一级/细分/概念）-> 成分股代码；空 focus -> 全池。db 未就绪时降级手工池。"""
    if not _ready():
        return universe.codes_of(focus)
    elig = " AND s.eligible=1" if eligible_only else ""
    try:
        with _conn() as c:
            if not focus:
                return [r["code"] for r in c.execute(
                    f"SELECT code FROM stocks s WHERE 1=1{elig} ORDER BY code")]
            rows = c.execute(
                "SELECT s.code FROM stocks s JOIN stock_sectors ss ON ss.code=s.code "
                f"WHERE ss.sector=?{elig} ORDER BY s.float_mcap DESC", (focus,)).fetchall()
        return [r["code"] for r in rows]
    except sqlite3.Error as e:
        logger.warning("codes_of 查询失败 %s: %s", focus, e)
        return universe.codes_of(focus)


def taxonomy() -> dict[str, list[str]]:
    """{申万一级: [细分...]}，供前端下拉与 /api/config。未就绪时降级手工池。"""
    if not _ready():
        return universe.taxonomy()
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT sw1, sub FROM stocks WHERE eligible=1 AND sw1!='' AND sw1!=? "
                "GROUP BY sw1, sub ORDER BY sw1, sub", (OTHER,)).fetchall()
    except sqlite3.Error as e:
        logger.warning("taxonomy 查询失败: %s", e)
        return universe.taxonomy()
    out: dict[str, list[str]] = {}
    for r in rows:
        if r["sub"] and r["sub"] != r["sw1"]:
            out.setdefault(r["sw1"], []).append(r["sub"])
        else:
            out.setdefault(r["sw1"], [])
    return out or universe.taxonomy()


def status() -> dict[str, Any]:
    """池子健康度：总数/eligible/已回填板块/板块数/最近刷新。"""
    try:
        with _conn() as c:
            g = c.execute(
                "SELECT COUNT(*) total, SUM(eligible) elig, SUM(is_st) st, "
                "SUM(CASE WHEN sectors_at!='' THEN 1 ELSE 0 END) tagged, "
                "SUM(is_leader) leaders FROM stocks").fetchone()
            sectors = c.execute(
                "SELECT COUNT(DISTINCT sector) n FROM stock_sectors").fetchone()["n"]
            at = c.execute("SELECT v FROM meta WHERE k='roster_at'").fetchone()
            days = c.execute("SELECT COUNT(DISTINCT date) n FROM sector_daily").fetchone()["n"]
            sd_rows = c.execute("SELECT COUNT(*) n FROM sector_daily").fetchone()["n"]
    except sqlite3.Error as e:
        logger.warning("status 查询失败: %s", e)
        return {"ready": False}
    try:
        db_mb = round(os.path.getsize(DB_PATH) / 1048576, 1)
    except OSError:
        db_mb = 0.0
    total = g["total"] or 0
    return {"ready": total > 0, "total": total, "eligible": g["elig"] or 0,
            "st": g["st"] or 0, "leaders": g["leaders"] or 0,
            "sectors_tagged": g["tagged"] or 0,
            "sectors_pending": (g["elig"] or 0) - (g["tagged"] or 0),
            "sector_count": sectors or 0, "sector_daily_days": days or 0,
            "sector_daily_rows": sd_rows or 0, "keep_days": SECTOR_KEEP_DAYS,
            "db_mb": db_mb, "roster_at": at["v"] if at else ""}


# ── 板块日变化统计 ─────────────────────────────────────────────────────────
def _agg_sector_payload(date: str, snap: dict[str, Any], memb: list) -> list[tuple]:
    """按板块聚合某一天的快照 -> sector_daily 行元组。

    `snapshot_daily`（当日 live）与 `backfill_sector_daily`（历史回填）**共用本函数**，
    保证两者口径完全一致、不漂移；纯函数、零网络，可离线单测。

    参数：
        snap: code -> {"chg_pct", "amount", "price", "name"}（price<=0 的股跳过）。
        memb: `stock_sectors` 行（含 code/sector/kind）；一股可属多板块（多行）。
    返回：INSERT 用的 12 元组列表，列序同 `sector_daily` 表。
    """
    agg: dict[str, dict[str, Any]] = {}
    for m in memb:
        q = snap.get(m["code"])
        if not q or q["price"] <= 0:
            continue
        a = agg.setdefault(m["sector"], {"kind": m["kind"], "chg": [], "up": 0, "down": 0,
                                         "lu": 0, "amt": 0.0, "lead": None})
        chg = q["chg_pct"]
        a["chg"].append(chg)
        a["amt"] += q["amount"]
        if chg > 0:
            a["up"] += 1
        elif chg < 0:
            a["down"] += 1
        # 涨停近似：主板 ±10%、创业板/科创板 ±20%（留 0.3 容差，不取精确撮合价）
        cap = 19.7 if m["code"].startswith(("30", "688")) else 9.7
        if chg >= cap:
            a["lu"] += 1
        if a["lead"] is None or chg > a["lead"]["chg_pct"]:
            a["lead"] = {"code": m["code"], "name": q.get("name", ""), "chg_pct": chg}
    payload = []
    for sector, a in agg.items():
        if not a["chg"]:
            continue
        lead = a["lead"] or {}
        payload.append((date, sector, a["kind"], len(a["chg"]),
                        round(sum(a["chg"]) / len(a["chg"]), 3), a["up"], a["down"], a["lu"],
                        a["amt"], lead.get("code", ""), lead.get("name", ""),
                        round(lead.get("chg_pct", 0.0), 3)))
    return payload


def snapshot_daily(date: str = "") -> int:
    """按板块聚合当日全市场快照 -> sector_daily（支撑「板块每日变化对比」）。

    数据用新浪 hs_a 同一份快照（自带涨跌幅/成交额），不再走腾讯分批。
    源失败则跳过、不写脏数据。返回写入板块数。
    """
    rows = ds.sina_all_stocks()
    if not rows:
        logger.warning("板块日统计跳过：行情源返回空")
        return 0
    date = date or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        memb = c.execute("SELECT code, sector, kind FROM stock_sectors").fetchall()
    if not memb:
        logger.warning("板块日统计跳过：板块归属尚未回填")
        return 0
    snap = {r["code"]: r for r in rows}
    payload = _agg_sector_payload(date, snap, memb)
    with _LOCK, _conn() as c:
        c.executemany(
            "INSERT INTO sector_daily(date,sector,kind,n,avg_chg,up_n,down_n,limit_up_n,"
            "amount,leader_code,leader_name,leader_chg) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date,sector) DO UPDATE SET n=excluded.n, avg_chg=excluded.avg_chg, "
            "up_n=excluded.up_n, down_n=excluded.down_n, limit_up_n=excluded.limit_up_n, "
            "amount=excluded.amount, leader_code=excluded.leader_code, "
            "leader_name=excluded.leader_name, leader_chg=excluded.leader_chg", payload)
    dropped = purge()  # 滚动 1 年，否则 24 万行/年无限累积
    logger.info("板块日统计 %s: %d 个板块%s", date, len(payload),
                f"（清理 {dropped} 行过期）" if dropped else "")
    return len(payload)


def backfill_sector_daily(days: int = 95, workers: int = 10) -> dict[str, Any]:
    """回填过去 ~一个季度的 `sector_daily`（板块走势历史）。

    逐股拉日 K（新浪 getKLineData，不封 IP），按**当前**板块归属逐日聚合，口径与 live
    `snapshot_daily` 完全一致（共用 `_agg_sector_payload`）。**只填缺失日期**（不覆盖已有
    live 行），可重复运行。

    未来函数安全：某历史日 `avg_chg` 仅用当日/前一日既成收盘价（同「日 K low 补条件单」，
    见 plan/2026-07-17-sector-history-backfill-design.md）。近似：当前归属套历史日有轻微错配、
    前复权口径致除权日略偏、amount 由 close×volume 估。

    返回 {ok, fetched(股票数), days(写入天数), rows(写入行数)}。
    """
    from concurrent.futures import ThreadPoolExecutor

    if not _sd_backfill_lock.acquire(blocking=False):
        logger.info("板块历史回填：已有一轮在跑，跳过")
        return {"ok": False, "msg": "回填进行中"}
    try:
        with _conn() as c:
            memb = c.execute("SELECT code, sector, kind FROM stock_sectors").fetchall()
            names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM stocks")}
            have = {r["date"] for r in c.execute("SELECT DISTINCT date FROM sector_daily")}
        if not memb:
            logger.warning("板块历史回填跳过：板块归属尚未回填")
            return {"ok": False, "msg": "板块归属未就绪"}
        codes = sorted({m["code"] for m in memb})
        logger.info("板块历史回填开始：%d 只 × ~%d 日 K（已有 %d 天，只填缺失）",
                    len(codes), days, len(have))

        # 1) 并发逐股拉日 K，算每根 K 的 chg_pct，按日归组（跳过已有日期）
        per_day: dict[str, dict[str, Any]] = {}
        done = [0]

        def _fetch(code: str) -> tuple[str, list]:
            return code, ds.sina_kline(code, num=days + 5, scale=240)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for code, bars in ex.map(_fetch, codes):
                done[0] += 1
                if done[0] % 500 == 0:
                    logger.info("板块历史回填抓取 %d/%d", done[0], len(codes))
                if len(bars) < 2:
                    continue
                nm = names.get(code, "")
                for i in range(1, len(bars)):
                    prev_c = bars[i - 1]["close"]
                    cur = bars[i]
                    if prev_c <= 0 or cur["close"] <= 0:
                        continue
                    d = str(cur["date"])[:10]
                    if d in have:  # live 已有 / 本轮已处理的日期，不重算
                        continue
                    per_day.setdefault(d, {})[code] = {
                        "chg_pct": (cur["close"] / prev_c - 1) * 100,
                        "amount": cur["close"] * cur["volume"],
                        "price": cur["close"], "name": nm}

        # 2) 逐日聚合 + 写入（ON CONFLICT DO NOTHING：绝不覆盖 live 行）
        total_rows = 0
        for d in sorted(per_day):
            payload = _agg_sector_payload(d, per_day[d], memb)
            if not payload:
                continue
            with _LOCK, _conn() as c:
                c.executemany(
                    "INSERT INTO sector_daily(date,sector,kind,n,avg_chg,up_n,down_n,"
                    "limit_up_n,amount,leader_code,leader_name,leader_chg) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,sector) DO NOTHING",
                    payload)
            total_rows += len(payload)
        purge()
        logger.info("板块历史回填完成：写 %d 天 / %d 行", len(per_day), total_rows)
        return {"ok": True, "fetched": len(codes), "days": len(per_day), "rows": total_rows}
    finally:
        _sd_backfill_lock.release()


def purge(days: int = SECTOR_KEEP_DAYS) -> int:
    """删除 > days 天的板块日统计。返回删除行数。

    977 个板块 × 245 交易日 ≈ 24 万行/年（约 82MB），不清理则无限增长（10 年 ~820MB）。
    与 `news_store.purge` 同为滚动 1 年策略。
    注：SQLite 的 DELETE 不回收文件空间（页会复用），故文件稳态约等于 1 年峰值而非持续缩小。
    """
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM sector_daily WHERE date < ?", (cutoff,))
        return c.total_changes - before


def sector_ranking(date: str = "", kind: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """某日板块排行（按平均涨跌降序）。kind 可筛 sw1/sub/concept。"""
    with _conn() as c:
        if not date:
            row = c.execute("SELECT MAX(date) d FROM sector_daily").fetchone()
            date = row["d"] if row and row["d"] else ""
        if not date:
            return []
        sql = "SELECT * FROM sector_daily WHERE date=? "
        args: list[Any] = [date]
        if kind:
            sql += "AND kind=? "
            args.append(kind)
        sql += "AND n>=3 ORDER BY avg_chg DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in c.execute(sql, args)]


def sector_history(sector: str, days: int = 30) -> list[dict[str, Any]]:
    """单板块近 N 日变化（供跨日对比）。"""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM sector_daily WHERE sector=? ORDER BY date DESC LIMIT ?",
            (sector, days))][::-1]
