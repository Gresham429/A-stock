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
    # **结果导向**（2026-07-16）：唯一一条「事后证明错」的建仓类教训。
    # 判罪线来自 factor_lab 的 16 万样本超额分布，不是拍的；文案是**个案陈述**、
    # 不宣称规律（n 太小：20–30 笔/年，统计验证要 784 笔 ≈ 31 年，见 PITFALLS#5）。
    "bad_outcome": ("建仓结果不佳", "{v}"),
    # 文案原为「…此后回落」——但它在成交同一循环里就记，那时「此后」还没发生，
    # 代码里从无一处检查过回落。**已删掉那半句**：只陈述入场位置这个事实，
    # 不宣称未经验证的结果（PITFALLS#0a）。是否真的错，由 bad_outcome 事后结算说了算。
    "chase_high": ("追高", "在 20 日区间位置 {v} 时建仓（>85 视为追高）"),
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
CREATE TABLE IF NOT EXISTS conditions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT DEFAULT '',
  kind TEXT NOT NULL,              -- stop_loss | take_profit
  trigger_price REAL NOT NULL, shares INTEGER NOT NULL,
  created_date TEXT NOT NULL,      -- 挂单日：补判只看这之后的日K
  status TEXT DEFAULT 'live',      -- live | triggered | cancelled
  triggered_date TEXT DEFAULT '', fill_price REAL DEFAULT 0, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cond_agent ON conditions(agent_id, status);
CREATE TABLE IF NOT EXISTS pending(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT DEFAULT '',
  side TEXT NOT NULL, limit_price REAL NOT NULL, shares INTEGER NOT NULL,
  placed_date TEXT NOT NULL, placed_t TEXT NOT NULL,   -- placed_t: 'HH:MM' 挂单时刻
  reason TEXT DEFAULT '',
  status TEXT DEFAULT 'live',    -- live | filled | expired | cancelled
  fill_date TEXT DEFAULT '', fill_t TEXT DEFAULT '', fill_price REAL DEFAULT 0,
  note TEXT DEFAULT '',
  -- 决策时刻的属性快照(JSON)：**必须在挂单时抓**，不能在成交时补。
  -- 成交发生在后续循环的 sweep_orders 里，那时 ctx 还没建；且「AI 决策时看到的属性」
  -- 本就是挂单那一刻的，事后重取会取到已经变了的值（= 一种未来函数）。
  snap TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_agent ON pending(agent_id, status);
CREATE TABLE IF NOT EXISTS claims(
  agent_id INTEGER NOT NULL, date TEXT NOT NULL, slot TEXT NOT NULL, ts TEXT,
  PRIMARY KEY (agent_id, date, slot)
);
-- 建仓留痕：成交时**只记事实、不判罪**，5/10/20 交易日后由 outcome 结算超额收益。
-- 为什么不在成交当天判：chase_high 的文案写着「此后回落」却在成交同一循环里记，
-- 那时「此后」还没发生（见 plan/2026-07-16-outcome-driven-lessons-design.md）。
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT DEFAULT '',
  entry_date TEXT NOT NULL, entry_price REAL NOT NULL, shares INTEGER DEFAULT 0,
  -- 属性快照：判罪时回头看的就是这些（Phase 2）
  range_pos REAL, vol REAL, cum20 REAL, pa_score REAL,
  sector TEXT DEFAULT '', sector_chg REAL,
  -- 结算结果：r=个股绝对, x=超额vs大盘(主标签), s=超额vs板块(尽力而为，可 NULL)
  r5 REAL, r10 REAL, r20 REAL,
  x5 REAL, x10 REAL, x20 REAL,
  s5 REAL, s10 REAL, s20 REAL,
  settled_at TEXT DEFAULT '',    -- 20 日档结算完成的时刻；空=未结清
  UNIQUE(agent_id, code, entry_date)
);
CREATE INDEX IF NOT EXISTS idx_entries_open ON entries(settled_at, entry_date);
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
        # 旧库迁移：pending.snap 是后加的（2026-07-16 结果导向教训）。
        # CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，必须显式 ALTER。
        have = {r["name"] for r in c.execute("PRAGMA table_info(pending)")}
        if "snap" not in have:
            c.execute("ALTER TABLE pending ADD COLUMN snap TEXT DEFAULT ''")


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


# ── 建仓留痕 / 结果结算 ────────────────────────────────────────────────────
def add_entry(agent_id: int, code: str, name: str, entry_date: str, entry_price: float,
              shares: int, snap: dict[str, Any] | None = None) -> bool:
    """建仓留痕：**只记事实，不判罪**。判罪等 5/10/20 交易日后的结算。

    同 (agent, code, entry_date) 只留一条——同日多笔加仓视作同一次建仓决策，
    保留首次的属性快照（那才是「做这个决策时看到的东西」）。
    """
    s = snap or {}
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO entries(agent_id,code,name,entry_date,entry_price,shares,"
            "range_pos,vol,cum20,pa_score,sector,sector_chg) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (agent_id, code, name, entry_date, float(entry_price), int(shares),
             s.get("range_pos"), s.get("vol"), s.get("cum20"), s.get("pa_score"),
             s.get("sector") or "", s.get("sector_chg")))
        return cur.rowcount > 0


def open_entries(agent_id: int | None = None) -> list[dict[str, Any]]:
    """未结清的建仓（`settled_at` 为空）。结算扫描用。"""
    sql = "SELECT * FROM entries WHERE settled_at='' "
    args: list[Any] = []
    if agent_id is not None:
        sql += "AND agent_id=? "
        args.append(agent_id)
    sql += "ORDER BY entry_date"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def update_entry(entry_id: int, rets: dict[str, float | None], done: bool) -> None:
    """写回结算结果。**幂等**——同一条会被反复扫到（r5 先到、r20 后到）。

    done=True（20 日档已出）才落 `settled_at`，此后不再扫描。
    """
    cols = [k for k in ("r5", "r10", "r20", "x5", "x10", "x20", "s5", "s10", "s20") if k in rets]
    if not cols and not done:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    args: list[Any] = [rets[k] for k in cols]
    if done:
        sets += (", " if sets else "") + "settled_at=?"
        args.append(datetime.now().isoformat(timespec="seconds"))
    args.append(entry_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE entries SET {sets} WHERE id=?", args)


def entries(agent_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """建仓留痕（含结算结果），新的在前。供 API/前端。"""
    sql = "SELECT * FROM entries "
    args: list[Any] = []
    if agent_id is not None:
        sql += "WHERE agent_id=? "
        args.append(agent_id)
    sql += "ORDER BY entry_date DESC, id DESC LIMIT ?"
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


def for_ai(limit: int = 6, agent_id: int | None = None) -> str:
    """【历史教训】注入块：喂**事实统计**（犯过 N 次），不是让 AI 改写提示词。

    与「用 5 个样本优化提示词」有本质区别——这里是数事实，不是拟合参数。

    **两种作用域**（2026-07-16 用户明确区分）：
    - `agent_id` 给定 → **只召回该 agent 自己的**教训（个体记忆）。多-agent 对照实验
      要干净：A 不该被 B 的错误惩罚，否则 12 个 agent 的记忆会收敛到一起。
    - `agent_id=None` → 跨全体账户汇总（用户面 5 个 AI 用——它们本就该学舰队的集体教训）。
    """
    if agent_id is not None:
        with _conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT kind, SUM(hits) hits FROM lessons WHERE agent_id=? "
                "GROUP BY kind ORDER BY hits DESC LIMIT ?", (agent_id, limit))]
        scope = "你（本账户）过去"
    else:
        rows = lesson_rollup(limit)
        scope = "模拟盘上全体账户"
    if not rows:
        return ""
    lines = [f"【历史教训（{scope}的真实错误，按发生次数排序；这些是事实统计，不是推测）】"]
    for r in rows:
        label = r.get("label") or LESSON_KINDS.get(r["kind"], (r["kind"], ""))[0]
        with _conn() as c:
            # 代表性文案：优先取本 agent 的；跨 agent 时取 hits 最高的那条
            q = "SELECT lesson FROM lessons WHERE kind=?" + (" AND agent_id=?" if agent_id else "")
            args = (r["kind"], agent_id) if agent_id else (r["kind"],)
            hit = c.execute(q + " ORDER BY hits DESC LIMIT 1", args).fetchone()
        lines.append(f"- [{label}] 累计 {r['hits']} 次：{hit['lesson'] if hit else ''}")
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
        c.execute("DELETE FROM claims WHERE date < ?", (d365,))
        c.execute("DELETE FROM pending WHERE placed_date < ?", (d365,))
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


# ── 条件单（止损/止盈：纪律，不是预测） ───────────────────────────────────
def add_condition(agent_id: int, code: str, name: str, kind: str,
                  trigger_price: float, shares: int, note: str = "") -> int:
    """挂条件单。kind: stop_loss(跌破卖) | take_profit(涨到卖)。

    **只做纪律，不做预测**：「跌破买入价 8% 止损」是规则；「涨到 41 就追」是预测，不做。
    """
    if kind not in ("stop_loss", "take_profit"):
        raise ValueError(f"未知条件单类型: {kind}")
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO conditions(agent_id,code,name,kind,trigger_price,shares,"
            "created_date,status,note) VALUES(?,?,?,?,?,?,?,'live',?)",
            (agent_id, code, name, kind, round(float(trigger_price), 3), int(shares),
             date_cls.today().isoformat(), note))
        return cur.lastrowid


def live_conditions(agent_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM conditions WHERE status='live'"
    args: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id=?"
        args.append(agent_id)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id", args)]


def conditions_of(agent_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM conditions WHERE agent_id=? ORDER BY id DESC LIMIT ?",
            (agent_id, limit))]


def close_condition(cid: int, status: str, triggered_date: str = "",
                    fill_price: float = 0.0) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE conditions SET status=?, triggered_date=?, fill_price=? WHERE id=?",
                  (status, triggered_date, round(fill_price, 3), cid))


def cancel_conditions(agent_id: int, code: str) -> int:
    """清仓后撤掉该股所有挂着的条件单（否则会对空仓触发）。"""
    with _LOCK, _conn() as c:
        b = c.total_changes
        c.execute("UPDATE conditions SET status='cancelled' "
                  "WHERE agent_id=? AND code=? AND status='live'", (agent_id, code))
        return c.total_changes - b


# ── 时段占位（原子幂等） ───────────────────────────────────────────────────
def claim_slot(agent_id: int, date: str, slot: str) -> bool:
    """原子抢占「某 agent 某日某时段」。已被占则返回 False。

    **为什么不能用「查有没有复盘记录」当幂等门**：那是「开头查、180 秒后才写」，
    两个并发的 run_all 会双双从门缝挤过去——实测 10:19:58 与 10:20:08 各起一轮，
    两轮都跑完了(179s/177s)，微型-均衡单A/B 各跑了 2 次。
    改用 PRIMARY KEY 冲突做原子占位：抢不到就立刻退出，不存在窗口。
    """
    try:
        with _LOCK, _conn() as c:
            c.execute("INSERT INTO claims(agent_id,date,slot,ts) VALUES(?,?,?,?)",
                      (agent_id, date, slot, datetime.now().isoformat(timespec="seconds")))
        return True
    except sqlite3.IntegrityError:
        return False


def release_slot(agent_id: int, date: str, slot: str) -> None:
    """跑失败时释放占位，让下次开 app 能重试（成功则保留，形成幂等）。"""
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM claims WHERE agent_id=? AND date=? AND slot=?",
                  (agent_id, date, slot))


def slots_done(agent_id: int, date: str) -> list[str]:
    with _conn() as c:
        return [r["slot"] for r in c.execute(
            "SELECT slot FROM claims WHERE agent_id=? AND date=?", (agent_id, date))]


# ── 限价委托（挂单，非即时成交） ───────────────────────────────────────────
def place(agent_id: int, code: str, name: str, side: str, limit_price: float,
          shares: int, placed_t: str, reason: str = "",
          snap: dict[str, Any] | None = None) -> int:
    """挂一笔限价委托。**当日有效**（A股规则：收盘未成交自动撤销）。

    snap：决策时刻的属性快照，成交后转存进 `entries` 供日后结算判罪。
    在**这里**抓而不是成交时抓——见 pending.snap 列的注释。
    """
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO pending(agent_id,code,name,side,limit_price,shares,"
            "placed_date,placed_t,reason,status,snap) VALUES(?,?,?,?,?,?,?,?,?,'live',?)",
            (agent_id, code, name, side, round(float(limit_price), 3), int(shares),
             date_cls.today().isoformat(), placed_t, reason[:200],
             json.dumps(snap or {}, ensure_ascii=False)))
        return cur.lastrowid


def live_pending(agent_id: int | None = None, date: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM pending WHERE status='live'"
    args: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id=?"
        args.append(agent_id)
    if date:
        sql += " AND placed_date=?"
        args.append(date)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id", args)]


def close_pending(pid: int, status: str, fill_date: str = "", fill_t: str = "",
                  fill_price: float = 0.0, note: str = "") -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE pending SET status=?, fill_date=?, fill_t=?, fill_price=?, note=? "
                  "WHERE id=?", (status, fill_date, fill_t, round(fill_price, 3), note, pid))


def pending_of(agent_id: int, limit: int = 60) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pending WHERE agent_id=? ORDER BY id DESC LIMIT ?",
            (agent_id, limit))]


def pending_stats(days: int = 30) -> dict[str, Any]:
    """挂单成交率 —— 未成交本身也是信息（价格挂太保守 = 系统性错过）。"""
    since = (date_cls.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT status, COUNT(*) n FROM pending WHERE placed_date >= ? GROUP BY status",
            (since,))]
    tot = sum(r["n"] for r in rows) or 0
    by = {r["status"]: r["n"] for r in rows}
    return {"total": tot, **by,
            "fill_rate": round(100 * by.get("filled", 0) / tot, 1) if tot else None}
