"""提示词模板库（SQLite `data/templates.db`）：版本化 + 按版本统计客观指标。

**为什么必须版本化**：没有它，「让 AI 优化提示词」无从谈起——你分不清效果变化是提示词
改的还是市场变的。有了版本号，每次 AI 调用记录用了哪个 `(name, version)`，归因数据就能
按版本分组，v1 与 v2 可直接对比，改坏了能回滚。

**只统计客观指标**（见 `plan/2026-07-16-agent-evolution-design.md`）：
    引用有效性  provenance.verify_basis 的 ✓/⚠ 数（后端权威校验，AI 编不了）
    schema 合规 JSON 解析/字段是否失败
这些**每次调用就是一个样本**、客观、即时反馈、不受市场影响。

**红线：不统计、也绝不朝收益率优化。** 用户 20–30 笔/年，统计验证策略优势需 784 笔≈31 年；
用 5–8 个样本优化提示词只会拟合噪音，且标签被大盘 beta 污染（大盘涨时随便买都赚，
优化器会学到「AI 很棒」）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "templates.db")
_LOCK = threading.Lock()

STATS_KEEP_DAYS = 365  # 模板统计滚动保留（模板数 × 版本 × 245 日/年，不清则无限涨）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, version INTEGER NOT NULL,
  body TEXT NOT NULL, active INTEGER DEFAULT 0,
  note TEXT DEFAULT '', created_at TEXT,
  UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_tpl_active ON templates(name, active);
CREATE TABLE IF NOT EXISTS template_stats(
  name TEXT NOT NULL, version INTEGER NOT NULL, date TEXT NOT NULL,
  calls INTEGER DEFAULT 0, basis_ok INTEGER DEFAULT 0, basis_bad INTEGER DEFAULT 0,
  schema_fail INTEGER DEFAULT 0,
  PRIMARY KEY (name, version, date)
);
CREATE INDEX IF NOT EXISTS idx_ts_date ON template_stats(date);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def seed(name: str, body: str, note: str = "") -> int:
    """首次灌入种子（v1 并设为 active）。已存在同名模板则不动——**加性补入**，不覆盖用户改动。

    返回 active 版本号。
    """
    init()
    with _LOCK, _conn() as c:
        row = c.execute("SELECT version FROM templates WHERE name=? AND active=1",
                        (name,)).fetchone()
        if row:
            return int(row["version"])
        c.execute("INSERT OR IGNORE INTO templates(name,version,body,active,note,created_at) "
                  "VALUES(?,1,?,1,?,?)",
                  (name, body, note or "种子版本（代码内置）",
                   datetime.now().isoformat(timespec="seconds")))
    logger.info("模板种子灌入: %s v1", name)
    return 1


def get(name: str, fallback: str = "") -> tuple[str, int]:
    """取 active 版本的 (body, version)。查不到返回 (fallback, 0)。

    version=0 表示「用的是代码内置的兜底」，统计时可据此区分。
    """
    try:
        with _conn() as c:
            row = c.execute("SELECT body, version FROM templates WHERE name=? AND active=1",
                            (name,)).fetchone()
        if row:
            return row["body"], int(row["version"])
    except sqlite3.Error as e:
        logger.warning("模板读取失败 %s（用代码兜底）: %s", name, e)
    return fallback, 0


def list_versions(name: str = "") -> list[dict[str, Any]]:
    sql = "SELECT id,name,version,active,note,created_at,length(body) AS size FROM templates"
    args: list[Any] = []
    if name:
        sql += " WHERE name=?"
        args.append(name)
    sql += " ORDER BY name, version DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def add_version(name: str, body: str, note: str = "", activate: bool = False) -> int:
    """新增版本（版本号自增）。activate=True 则同时切为 active。返回新版本号。"""
    with _LOCK, _conn() as c:
        row = c.execute("SELECT MAX(version) v FROM templates WHERE name=?", (name,)).fetchone()
        ver = int((row["v"] or 0)) + 1
        c.execute("INSERT INTO templates(name,version,body,active,note,created_at) "
                  "VALUES(?,?,?,0,?,?)",
                  (name, ver, body, note, datetime.now().isoformat(timespec="seconds")))
        if activate:
            c.execute("UPDATE templates SET active=0 WHERE name=?", (name,))
            c.execute("UPDATE templates SET active=1 WHERE name=? AND version=?", (name, ver))
    logger.info("模板新版本: %s v%d%s", name, ver, "（已激活）" if activate else "")
    return ver


def activate(name: str, version: int) -> bool:
    """切换 active 版本（回滚即切回旧版本号）。"""
    with _LOCK, _conn() as c:
        hit = c.execute("SELECT 1 FROM templates WHERE name=? AND version=?",
                        (name, version)).fetchone()
        if not hit:
            return False
        c.execute("UPDATE templates SET active=0 WHERE name=?", (name,))
        c.execute("UPDATE templates SET active=1 WHERE name=? AND version=?", (name, version))
    logger.info("模板激活: %s v%d", name, version)
    return True


def record(name: str, version: int, *, basis_ok: int = 0, basis_bad: int = 0,
           schema_fail: int = 0) -> None:
    """记一次调用的客观指标（按 name+version+日 聚合）。失败不抛——统计不该拖垮主流程。"""
    try:
        today = date_cls.today().isoformat()
        with _LOCK, _conn() as c:
            c.execute(
                "INSERT INTO template_stats(name,version,date,calls,basis_ok,basis_bad,schema_fail)"
                " VALUES(?,?,?,1,?,?,?) ON CONFLICT(name,version,date) DO UPDATE SET "
                "calls=calls+1, basis_ok=basis_ok+excluded.basis_ok, "
                "basis_bad=basis_bad+excluded.basis_bad, schema_fail=schema_fail+excluded.schema_fail",
                (name, version, today, basis_ok, basis_bad, schema_fail))
    except sqlite3.Error as e:
        logger.warning("模板统计记录失败 %s v%s: %s", name, version, e)


def purge(days: int = STATS_KEEP_DAYS) -> int:
    """删除 > days 天的模板统计。返回删除行数。

    与 news_store.purge / universe_store.purge 同为滚动策略——按日累积的表一律要有。
    """
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with _LOCK, _conn() as c:
        before = c.total_changes
        c.execute("DELETE FROM template_stats WHERE date < ?", (cutoff,))
        return c.total_changes - before


def stats(name: str = "", days: int = 30) -> list[dict[str, Any]]:
    """按 (name, version) 聚合近 N 日客观指标 —— 这是 A/B 对比 v1 与 v2 的依据。"""
    since = (date_cls.today() - timedelta(days=days)).isoformat()
    sql = ("SELECT name, version, SUM(calls) calls, SUM(basis_ok) basis_ok, "
           "SUM(basis_bad) basis_bad, SUM(schema_fail) schema_fail "
           "FROM template_stats WHERE date >= ?")
    args: list[Any] = [since]
    if name:
        sql += " AND name=?"
        args.append(name)
    sql += " GROUP BY name, version ORDER BY name, version DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    for r in rows:
        tot = (r["basis_ok"] or 0) + (r["basis_bad"] or 0)
        r["basis_ok_pct"] = round(100 * (r["basis_ok"] or 0) / tot, 1) if tot else None
        r["schema_fail_pct"] = (round(100 * (r["schema_fail"] or 0) / r["calls"], 1)
                                if r["calls"] else None)
    return rows


def status() -> dict[str, Any]:
    """模板库健康度 + 容量（容量要可见——按日累积的表都得盯着）。"""
    try:
        with _conn() as c:
            n_tpl = c.execute("SELECT COUNT(DISTINCT name) n FROM templates").fetchone()["n"]
            n_ver = c.execute("SELECT COUNT(*) n FROM templates").fetchone()["n"]
            n_stat = c.execute("SELECT COUNT(*) n FROM template_stats").fetchone()["n"]
    except sqlite3.Error as e:
        logger.warning("模板 status 失败: %s", e)
        return {"ready": False}
    try:
        db_mb = round(os.path.getsize(DB_PATH) / 1048576, 2)
    except OSError:
        db_mb = 0.0
    return {"ready": True, "templates": n_tpl, "versions": n_ver,
            "stat_rows": n_stat, "keep_days": STATS_KEEP_DAYS, "db_mb": db_mb}
