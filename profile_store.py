"""本地多档投资画像（SQLite data/profiles.db）。

每档 = 现金本金 + 风险偏好；支持多档建档、一个 active 全局生效。
分级依据 = 总资产（现金 + 已持有股票市值，由调用方传入），据阈值落到 5 档之一，
每档一套「玩法 template」（不同资产不同打法，核心=期望为正），拼成文本块注入所有 AI 分析。

本模块只管画像与分级逻辑，不依赖行情/持仓模块（总资产由 app 传入），保持纯净可测。
持仓归属 = 全局共享（profile 只管现金，持仓用全局 portfolio）——见 plan/2026-07-14-capital-profiles-templates-design.md。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "profiles.db")
_LOCK = threading.Lock()

RISK_PREFS = ["稳健", "均衡", "激进"]

# 5 档：按总资产(现金+持仓市值)落档。scen=同步给 rules_store 的本金维(小/中/大)。
# 阈值/维度按仓位管理·流动性·分散度理论设定，可后续调。
TIERS: list[dict[str, Any]] = [
    {"name": "微型", "lo": 0.0, "hi": 3e4, "scen": "小",
     "size": "1–2 只集中", "max_pos": "≤70%", "pool": "中低价/题材弹性",
     "horizon": "波段/短线", "plays": "事件/题材/波段", "churn": "灵活但算成本",
     "risk": "≤2%", "cash": "留 1 手子弹", "anchor": "搏高赔率、快进快出、错就砍"},
    {"name": "小型", "lo": 3e4, "hi": 30e4, "scen": "小",
     "size": "2–4 只", "max_pos": "≤40%", "pool": "成长+题材",
     "horizon": "波段(可短线)", "plays": "波段+打板(严控)", "churn": "中频",
     "risk": "≤1.5%", "cash": "留 20–30%", "anchor": "赔率优先、顺势"},
    {"name": "中型", "lo": 30e4, "hi": 200e4, "scen": "中",
     "size": "4–8 只", "max_pos": "≤25%", "pool": "成长+价值+题材",
     "horizon": "波段+趋势", "plays": "趋势+价值", "churn": "低频",
     "risk": "≤1%", "cash": "留 20%", "anchor": "胜率赔率并重"},
    {"name": "大型", "lo": 200e4, "hi": 2000e4, "scen": "大",
     "size": "8–15 只分散", "max_pos": "≤15%", "pool": "白马+行业龙头",
     "horizon": "趋势/波段", "plays": "趋势+配置、分批建仓", "churn": "低频、分批",
     "risk": "≤1%", "cash": "动态 20–40%", "anchor": "求稳、控回撤"},
    {"name": "超大", "lo": 2000e4, "hi": float("inf"), "scen": "大",
     "size": "15–30 只", "max_pos": "≤8%", "pool": "大盘蓝筹/流动性好",
     "horizon": "长线/趋势", "plays": "配置+仓位管理、大额分批算冲击成本", "churn": "极低频",
     "risk": "≤0.5%", "cash": "战略现金仓", "anchor": "绝对低回撤、资产保全优先"},
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT,
  cash REAL, risk_pref TEXT DEFAULT '均衡');
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    """建表；首次无档时建一个「默认」1 万档并设为 active（贴合用户画像）。"""
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)
        n = c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if n == 0:
            now = datetime.now().isoformat(timespec="seconds")
            cur = c.execute("INSERT INTO profiles(name,created_at,cash,risk_pref) VALUES(?,?,?,?)",
                            ("默认", now, 10000.0, "均衡"))
            c.execute("INSERT INTO meta(k,v) VALUES('active',?)", (str(cur.lastrowid),))


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "cash": r["cash"],
            "risk_pref": r["risk_pref"], "created_at": r["created_at"]}


def list_profiles() -> list[dict[str, Any]]:
    with _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM profiles ORDER BY id").fetchall()]


def get(pid: int) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        return _row(r) if r else None


def create(name: str, cash: float, risk_pref: str = "均衡") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rp = risk_pref if risk_pref in RISK_PREFS else "均衡"
    with _LOCK, _conn() as c:
        cur = c.execute("INSERT INTO profiles(name,created_at,cash,risk_pref) VALUES(?,?,?,?)",
                        (name or "画像", now, float(cash or 0), rp))
        return cur.lastrowid


def update(pid: int, **fields: Any) -> None:
    allowed = ("name", "cash", "risk_pref")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    args.append(pid)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE profiles SET {','.join(sets)} WHERE id=?", args)


def delete(pid: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))
        act = c.execute("SELECT v FROM meta WHERE k='active'").fetchone()
        if act and act[0] == str(pid):     # 删掉的是 active → 切到剩余第一个
            first = c.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
            if first:
                c.execute("UPDATE meta SET v=? WHERE k='active'", (str(first[0]),))
            else:
                c.execute("DELETE FROM meta WHERE k='active'")


def get_active() -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k='active'").fetchone()
        if r:
            p = c.execute("SELECT * FROM profiles WHERE id=?", (int(r[0]),)).fetchone()
            if p:
                return _row(p)
        p = c.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()
        return _row(p) if p else None


def set_active(pid: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES('active',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(pid),))


def tier_of(total_assets: float) -> dict[str, Any]:
    """总资产 → 档位（返回该档 template dict）。"""
    t = float(total_assets or 0)
    for tier in TIERS:
        if tier["lo"] <= t < tier["hi"]:
            return tier
    return TIERS[-1]


def _yi(v: float) -> str:
    v = float(v or 0)
    return f"{v / 1e4:.1f}万" if abs(v) >= 1e4 else f"{v:.0f}元"


def block_for_ai(cash: float, total_assets: float, holdings_n: int = 0) -> str:
    """拼给 AI 的【本金玩法档】文本块（前置到 web_context 注入所有分析）。"""
    tier = tier_of(total_assets)
    return (
        f"\n【本金玩法档·据总资产动态分级】\n"
        f"总资产≈{_yi(total_assets)}（现金{_yi(cash)} + 持仓{holdings_n}只）→ 档位：{tier['name']}\n"
        f"该档打法：持仓{tier['size']}、单标的仓位{tier['max_pos']}、标的池「{tier['pool']}」、"
        f"周期{tier['horizon']}、可用打法「{tier['plays']}」、换手{tier['churn']}、"
        f"单笔风险{tier['risk']}、现金管理{tier['cash']}。\n"
        f"核心目标=期望为正：{tier['anchor']}。请按此档打法给**组合级**建议"
        f"（该加/该减/是否分散、单标的是否超仓位上限、是否符合本档标的池与周期）。\n")
