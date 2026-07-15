"""Agent 模拟交易的持久层（SQLite `data/agents.db`）：agent 配置 / 日循环日志 / **教训库**。

设计见 `plan/2026-07-16-agent-evolution-design.md`。三个要点：

**只记失败，不记成功。** 用户判断：成功多为 beta（大盘涨时随便买都赚），学之即噪音；
失败则有可指认的原因。且**失败归因不需要样本量**——它是核对事实（`range_pos=92` 就是追高、
赚 0.18% < 保本 0.232% 就是倒贴），不是推断概率。这绕开了「统计验证需 784 笔≈31 年」的死局。

**教训 kind 是闭集**（`LESSON_KINDS`），参照 `provenance.SIGNAL_DEFS` 的做法——闭集才能统计、
才能校验，自由文本不行。同 kind 累加 `hits` 而非新增行，故教训库天然稀疏、可永久保留。

**存储分层**：`runs` 存 LLM 原文最占空间 → 原文 90 天、结论 365 天，分列分策略。
（`sector_daily` 那次的教训：按日累积的表不清理，10 年能涨到 820MB。）
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "agents.db")
_LOCK = threading.Lock()

RUN_DETAIL_KEEP_DAYS = 90    # LLM 原文（最占空间）
RUN_KEEP_DAYS = 365          # 结论/摘要
EQUITY_KEEP_DAYS = 730       # 净值曲线要长一点

# 教训闭集：每条都必须能由**确定性检查**判定，不依赖 LLM 主观判断。
# key -> (人读名, 一句话教训模板)
LESSON_KINDS: dict[str, tuple[str, str]] = {
    "chase_high": ("追高", "在 20 日区间位置 {v} 时建仓（>85 视为追高），此后回落"),
    "below_breakeven": ("赚不抵费", "卖出收益 {v}% 低于保本涨幅，扣费后实为亏损"),
    "against_sector": ("逆势", "所属板块当日均跌 {v}% 时仍买入，与板块方向相悖"),
    "oversize": ("超仓", "单标的仓位 {v}% 超过本金档位上限"),
    "cash_exhausted": ("满仓", "现金降至 {v}% 以下，失去应对回撤与补仓的余地"),
    "high_vol_entry": ("波动失控", "在年化波动 {v}% 的标的上建仓，超出本档风险承受"),
    "stale_hold": ("僵持", "持有 {v} 个交易日无任何动作，资金被无效占用"),
    "rule_violation": ("违规", "违反规则 {v}"),
    "loss_cut_late": ("止损迟滞", "浮亏已达 {v}% 仍未止损"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, account_id INTEGER NOT NULL, profile_id INTEGER NOT NULL,
  decider TEXT DEFAULT 'single', note TEXT DEFAULT '',
  active INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, date TEXT NOT NULL, phase TEXT NOT NULL,
  summary TEXT DEFAULT '',      -- 结论（保留 365 天）
  detail TEXT DEFAULT '',       -- LLM 原文（保留 90 天，最占空间）
  ok INTEGER DEFAULT 1, ms INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_date ON runs(agent_id, date);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER, code TEXT DEFAULT '', kind TEXT NOT NULL,
  rule_id INTEGER, evidence TEXT DEFAULT '', lesson TEXT DEFAULT '',
  hits INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT,
  UNIQUE(agent_id, kind, code)
);
CREATE INDEX IF NOT EXISTS idx_lessons_kind ON lessons(kind);
CREATE TABLE IF NOT EXISTS equity(
  agent_id INTEGER NOT NULL, date TEXT NOT NULL,
  cash REAL, market_value REAL, total REAL, pnl_pct REAL,
  PRIMARY KEY (agent_id, date)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── agent 配置 ─────────────────────────────────────────────────────────────
def create_agent(name: str, account_id: int, profile_id: int,
                 decider: str = "single", note: str = "") -> int:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO agents(name,account_id,profile_id,decider,note,active,created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (name, account_id, profile_id, decider, note,
             datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def list_agents(active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agents"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql)]


def get_agent(aid: int) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def set_active(aid: int, on: bool) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE agents SET active=? WHERE id=?", (1 if on else 0, aid))


def delete_agent(aid: int) -> None:
    with _LOCK, _conn() as c:
        for t in ("agents", "runs", "lessons", "equity"):
            key = "id" if t == "agents" else "agent_id"
            c.execute(f"DELETE FROM {t} WHERE {key}=?", (aid,))


# ── 日循环日志 ─────────────────────────────────────────────────────────────
def log_run(agent_id: int, date: str, phase: str, summary: str = "",
            detail: Any = "", ok: bool = True, ms: int = 0) -> None:
    """记一步流水线。detail 存 LLM 原文（90 天后被 purge 清空但行还在）。"""
    body = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    try:
        with _LOCK, _conn() as c:
            c.execute("INSERT INTO runs(agent_id,date,phase,summary,detail,ok,ms,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (agent_id, date, phase, summary[:2000], body[:20000],
                       1 if ok else 0, ms, datetime.now().isoformat(timespec="seconds")))
    except sqlite3.Error as e:
        logger.warning("run 日志写入失败 agent=%s phase=%s: %s", agent_id, phase, e)


def runs_of(agent_id: int, date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM runs WHERE agent_id=?"
    args: list[Any] = [agent_id]
    if date:
        sql += " AND date=?"
        args.append(date)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ── 教训库 ─────────────────────────────────────────────────────────────────
def add_lesson(agent_id: int, kind: str, evidence: str, code: str = "",
               rule_id: int | None = None) -> bool:
    """记一条失败教训。kind 必须在闭集内；同 (agent, kind, code) 累加 hits 而非新增行。"""
    if kind not in LESSON_KINDS:
        logger.warning("教训 kind 不在闭集内，丢弃: %s", kind)
        return False
    _, tpl = LESSON_KINDS[kind]
    lesson = tpl.format(v=evidence)
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO lessons(agent_id,code,kind,rule_id,evidence,lesson,hits,first_seen,last_seen)"
            " VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(agent_id,kind,code) DO UPDATE SET "
            "hits=hits+1, evidence=excluded.evidence, lesson=excluded.lesson, last_seen=excluded.last_seen",
            (agent_id, code, kind, rule_id, str(evidence), lesson, now, now))
    return True


def lessons(agent_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """教训按 hits 降序 —— 犯得越多越该进提示词。"""
    sql = "SELECT * FROM lessons"
    args: list[Any] = []
    if agent_id is not None:
        sql += " WHERE agent_id=?"
        args.append(agent_id)
    sql += " ORDER BY hits DESC, last_seen DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def lesson_rollup(limit: int = 8) -> list[dict[str, Any]]:
    """跨 agent 按 kind 汇总——这是喂给提示词的**事实统计**，非拟合。"""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT kind, SUM(hits) hits, COUNT(DISTINCT agent_id) agents, "
            "MAX(last_seen) last_seen FROM lessons GROUP BY kind "
            "ORDER BY hits DESC LIMIT ?", (limit,))]
    for r in rows:
        r["label"] = LESSON_KINDS.get(r["kind"], (r["kind"], ""))[0]
    return rows


def for_ai(limit: int = 6) -> str:
    """【历史教训】注入块：喂**事实统计**（你犯过 N 次），不是让 AI 改写提示词。

    与「用 5 个样本优化提示词」有本质区别——这里是数事实，不是拟合参数。
    """
    rows = lesson_rollup(limit)
    if not rows:
        return ""
    lines = ["【历史教训（模拟盘上你的真实错误，按发生次数排序；这些是事实统计，不是推测）】"]
    for r in rows:
        one = None
        with _conn() as c:
            hit = c.execute("SELECT lesson FROM lessons WHERE kind=? ORDER BY hits DESC LIMIT 1",
                            (r["kind"],)).fetchone()
            one = hit["lesson"] if hit else ""
        lines.append(f"- [{r['label']}] 累计 {r['hits']} 次：{one}")
    lines.append("- 以上错误请在本次判断中主动规避；若本次建议可能重蹈其中某条，必须说明为何这次不同。")
    return "\n".join(lines)


# ── 净值 ───────────────────────────────────────────────────────────────────
def log_equity(agent_id: int, date: str, cash: float, mv: float, init_capital: float) -> None:
    total = cash + mv
    pnl = round((total / init_capital - 1) * 100, 3) if init_capital else 0.0
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO equity(agent_id,date,cash,market_value,total,pnl_pct) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(agent_id,date) DO UPDATE SET "
                  "cash=excluded.cash, market_value=excluded.market_value, "
                  "total=excluded.total, pnl_pct=excluded.pnl_pct",
                  (agent_id, date, round(cash, 2), round(mv, 2), round(total, 2), pnl))


def equity_of(agent_id: int, days: int = 90) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM equity WHERE agent_id=? ORDER BY date DESC LIMIT ?",
            (agent_id, days))][::-1]


# ── 存储管理（用户特别叮嘱） ───────────────────────────────────────────────
def purge() -> dict[str, int]:
    """分层清理：LLM 原文 90 天、结论 365 天、净值 730 天。教训永久（天然稀疏）。

    `runs.detail` 存 LLM 原文是最大头，故先清空原文再删整行——保住结论、砍掉体积。
    """
    today = date_cls.today()
    d90 = (today - timedelta(days=RUN_DETAIL_KEEP_DAYS)).isoformat()
    d365 = (today - timedelta(days=RUN_KEEP_DAYS)).isoformat()
    d730 = (today - timedelta(days=EQUITY_KEEP_DAYS)).isoformat()
    out = {}
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE runs SET detail='' WHERE date < ? AND detail != ''", (d90,))
        out["detail_cleared"] = cur.rowcount
        b = c.total_changes
        c.execute("DELETE FROM runs WHERE date < ?", (d365,))
        out["runs_deleted"] = c.total_changes - b
        b = c.total_changes
        c.execute("DELETE FROM equity WHERE date < ?", (d730,))
        out["equity_deleted"] = c.total_changes - b
    return out


def status() -> dict[str, Any]:
    try:
        with _conn() as c:
            n_agent = c.execute("SELECT COUNT(*) n FROM agents WHERE active=1").fetchone()["n"]
            n_run = c.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
            n_les = c.execute("SELECT COUNT(*) n FROM lessons").fetchone()["n"]
            hits = c.execute("SELECT COALESCE(SUM(hits),0) n FROM lessons").fetchone()["n"]
            n_eq = c.execute("SELECT COUNT(*) n FROM equity").fetchone()["n"]
    except sqlite3.Error as e:
        logger.warning("agent status 失败: %s", e)
        return {"ready": False}
    try:
        db_mb = round(os.path.getsize(DB_PATH) / 1048576, 2)
    except OSError:
        db_mb = 0.0
    return {"ready": True, "agents": n_agent, "runs": n_run, "lessons": n_les,
            "lesson_hits": hits, "equity_rows": n_eq, "db_mb": db_mb,
            "keep": {"run_detail": RUN_DETAIL_KEEP_DAYS, "runs": RUN_KEEP_DAYS,
                     "equity": EQUITY_KEEP_DAYS, "lessons": "永久"}}
