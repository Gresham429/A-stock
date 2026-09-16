# 三周期选股 + 观点账本 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三个周期各推荐 5 只、自选股每只给买点/卖点/止损/时间窗，所有结论进观点账本，改口只在触发事件时允许，结果每日贴回。

**Architecture:** 五个新模块各管一件事：`picks_levels`（K 线算候选价，纯函数）、`picks_store`（账本 sqlite + 记忆块）、`llm_picks`（两个提示词 + 返回校验）、`picks_pipeline`（候选池、运行、结算、定时循环）、`picks_routes`（Flask Blueprint）。`app.py` 只加注册和本地定时线程，`scheduler.py` 加服务器定时线程。

**Tech Stack:** Python 3.10 标准库 + sqlite3 + Flask；测试沿用仓库风格（`python3 tests/xxx.py` 直接跑，零网络，临时目录）。

**Spec:** `plan/2026-09-16-picks-ledger-design.md`

## Global Constraints

- 代码注释与文档不出现装饰符号（箭头、对勾、emoji）；数学/代码里的 `->` 可以。
- 每个新文件不超过 400 行；类型注解；模块级 `logger = logging.getLogger(__name__)`；不裸 `except`。
- 测试零网络：所有取数（`datasources`、`llm._chat`、`review.store`、`universe_store`）在测试里 monkeypatch。
- 个人数据走 `userctx.user_path("picks.db")`，公共走 `userctx.shared_path("picks_public.db")`；连接用 `userctx.open_db(path)`。
- 有效期（交易日）：short 5、mid 40、long 250。
- 改口规则：`decision=revise/withdraw` 且 `trigger=none` 降为 `keep`；`trigger=stop_hit/target_hit` 与上一条的 `touched` 不一致降为 `keep`。
- 价位校验：每个价必须落在某候选价 ±1% 内，否则替换为最近候选价并标 `adjusted`。
- 记忆块：近 60 天最多 12 条全文；远期为确定性汇总 + 最多 3 条相关记录（价位在今日价 ±5% 内，或同周期且 touched 为 stop/exit）。
- 提交用 Conventional Commits，中文标题，结尾加 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。
- 跑测试前 `export NO_PROXY='*'`；不要在仓库里起 `python3 app.py`。

---

### Task 1: picks_levels.py 候选价位（纯函数）

**Files:**
- Create: `picks_levels.py`
- Test: `tests/test_picks_levels.py`

**Interfaces:**
- Consumes: `structure.digest(bars)`（返回 `{ma5, ma20, ma60, hi20, lo20, last}` 或 None）；bars 为 `ds.sina_kline()` 格式 `[{date, open, high, low, close, volume}]` 时间正序。
- Produces:
  - `candidate_levels(bars: list[dict], short: bool = False) -> dict`，返回 `{"price": float, "atr_pct": float, "levels": [{"px": float, "kind": str}]}`；bars 不足 20 根返回 `{"price": None, "atr_pct": None, "levels": []}`。
  - `swing_points(bars: list[dict], k: int = 2) -> tuple[list[float], list[float]]`（lows, highs）。
  - `clusters(prices: list[float], tol: float = 0.01) -> list[float]`。
  - `snap(px: float, levels: list[dict], tol: float = 0.01) -> tuple[float, bool]`（吸附后价, 是否被调整）。
  - `fmt_levels(cl: dict) -> str` 给提示词用的一行文本。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_picks_levels.py  候选价位纯函数。python3 tests/test_picks_levels.py 直接跑。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_levels as pl

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

def mkbars(closes, spread=0.02):
    bars = []
    for i, c in enumerate(closes):
        bars.append({"date": f"2026-01-{i+1:02d}", "open": c, "high": round(c * (1 + spread), 2),
                     "low": round(c * (1 - spread), 2), "close": c, "volume": 1000})
    return bars

def test_too_short():
    r = pl.candidate_levels(mkbars([10.0] * 10))
    ck(r["levels"] == [] and r["price"] is None, "不足 20 根返回空")

def test_levels_contain_window_extremes_and_mas():
    closes = [10 + (i % 7) * 0.3 for i in range(70)]
    r = pl.candidate_levels(mkbars(closes))
    kinds = {l["kind"] for l in r["levels"]}
    for k in ("lo5", "hi5", "lo10", "hi10", "lo20", "hi20", "lo60", "hi60", "ma5", "ma20", "ma60", "atr_lo1", "atr_hi1"):
        ck(k in kinds, f"缺 {k}")
    ck(r["price"] == closes[-1], "price 是最后收盘")
    ck(all(l["px"] > 0 for l in r["levels"]), "价位都为正")
    ck(r["atr_pct"] > 0, "atr_pct 为正")

def test_swing_points():
    closes = [10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 8, 9, 10]
    lows, highs = pl.swing_points(mkbars(closes, spread=0.0), k=2)
    ck(9 in lows or 8 in lows, "找到摆动低点")
    ck(14 in highs or 12 in highs, "找到摆动高点")

def test_clusters_merge_within_tol():
    ck(pl.clusters([10.0, 10.05, 10.09, 11.0, 11.02]) == [10.05, 11.01] or len(pl.clusters([10.0, 10.05, 10.09, 11.0, 11.02])) == 2, "1% 内合并成两簇")
    ck(pl.clusters([]) == [], "空输入")

def test_short_adds_clusters():
    closes = [10, 9.5, 9.6, 10.4, 10.5, 9.55, 9.6, 10.45, 10.5, 9.5] * 3
    r = pl.candidate_levels(mkbars(closes, spread=0.0), short=True)
    kinds = {l["kind"] for l in r["levels"]}
    ck("cluster_lo" in kinds and "cluster_hi" in kinds, "短线加低点簇与高点簇")

def test_snap():
    levels = [{"px": 10.0, "kind": "lo20"}, {"px": 12.0, "kind": "hi20"}]
    px, adj = pl.snap(10.05, levels)
    ck(px == 10.05 and adj is False, "1% 内不调整")
    px, adj = pl.snap(10.5, levels)
    ck(px == 10.0 and adj is True, "1% 外吸附到最近候选")
    px, adj = pl.snap(11.9, levels)
    ck(px == 11.9 and adj is False, "接近 hi20 不调整")

def test_fmt():
    s = pl.fmt_levels({"price": 10.0, "atr_pct": 2.5, "levels": [{"px": 9.5, "kind": "lo20"}]})
    ck("lo20=9.5" in s and "现价 10.0" in s, "格式化含价位与现价")

if __name__ == "__main__":
    for fn in (test_too_short, test_levels_contain_window_extremes_and_mas, test_swing_points,
               test_clusters_merge_within_tol, test_short_adds_clusters, test_snap, test_fmt):
        fn()
    print(f"OK — test_picks_levels 全过（{N[0]} 断言）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_picks_levels.py`
Expected: `ModuleNotFoundError: No module named 'picks_levels'`

- [ ] **Step 3: 实现**

```python
"""候选价位：从日 K 算出买点/卖点/止损可以落在哪些价上（纯函数，零网络）。

模型不报无来源的价格：它只能从这里算出的候选里挑，返回后再用 snap() 校验。
候选来源：近 5/10/20/60 日高低、MA5/20/60、摆动高低点、ATR 上下带；
短线另加近 10 日低点簇/高点簇（相距 1% 内合并）。
"""
from __future__ import annotations

import logging
from typing import Any

import structure

logger = logging.getLogger(__name__)

MIN_BARS = 20
ATR_N = 20
SNAP_TOL = 0.01
CLUSTER_TOL = 0.01


def _clean(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for b in bars or []:
        try:
            out.append({"date": b.get("date"), "high": float(b["high"]), "low": float(b["low"]),
                        "close": float(b["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def swing_points(bars: list[dict[str, Any]], k: int = 2) -> tuple[list[float], list[float]]:
    """局部极值：左右各 k 根都不更低/更高。返回 (lows, highs)。"""
    rows = _clean(bars)
    lows: list[float] = []
    highs: list[float] = []
    for i in range(k, len(rows) - k):
        lo = rows[i]["low"]
        hi = rows[i]["high"]
        if all(lo <= rows[j]["low"] for j in range(i - k, i + k + 1)):
            lows.append(round(lo, 2))
        if all(hi >= rows[j]["high"] for j in range(i - k, i + k + 1)):
            highs.append(round(hi, 2))
    return lows, highs


def clusters(prices: list[float], tol: float = CLUSTER_TOL) -> list[float]:
    """相距 tol 以内的价合并成一簇，返回各簇均值（升序）。"""
    ps = sorted(float(p) for p in prices if p)
    if not ps:
        return []
    groups: list[list[float]] = [[ps[0]]]
    for p in ps[1:]:
        if p - groups[-1][-1] <= groups[-1][-1] * tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [round(sum(g) / len(g), 2) for g in groups]


def _atr_pct(rows: list[dict[str, Any]], n: int = ATR_N) -> float:
    win = rows[-n:]
    if not win:
        return 0.0
    return round(sum((r["high"] - r["low"]) / r["close"] for r in win if r["close"]) / len(win) * 100, 2)


def candidate_levels(bars: list[dict[str, Any]], short: bool = False) -> dict[str, Any]:
    """候选价位表。bars 不足 20 根返回空表。"""
    rows = _clean(bars)
    if len(rows) < MIN_BARS:
        return {"price": None, "atr_pct": None, "levels": []}
    price = rows[-1]["close"]
    levels: list[dict[str, Any]] = []
    for n in (5, 10, 20, 60):
        win = rows[-n:]
        levels.append({"px": round(min(r["low"] for r in win), 2), "kind": f"lo{n}"})
        levels.append({"px": round(max(r["high"] for r in win), 2), "kind": f"hi{n}"})
    d = structure.digest(bars) or {}
    for k in ("ma5", "ma20", "ma60"):
        if d.get(k):
            levels.append({"px": round(float(d[k]), 2), "kind": k})
    lows, highs = swing_points(rows)
    for px in lows[-3:]:
        levels.append({"px": px, "kind": "swing_lo"})
    for px in highs[-3:]:
        levels.append({"px": px, "kind": "swing_hi"})
    atr = _atr_pct(rows)
    for mult in (1, 2):
        levels.append({"px": round(price * (1 - atr / 100 * mult), 2), "kind": f"atr_lo{mult}"})
        levels.append({"px": round(price * (1 + atr / 100 * mult), 2), "kind": f"atr_hi{mult}"})
    if short:
        win = rows[-10:]
        for px in clusters([r["low"] for r in win]):
            levels.append({"px": px, "kind": "cluster_lo"})
        for px in clusters([r["high"] for r in win]):
            levels.append({"px": px, "kind": "cluster_hi"})
    return {"price": price, "atr_pct": atr, "levels": levels}


def snap(px: float, levels: list[dict[str, Any]], tol: float = SNAP_TOL) -> tuple[float, bool]:
    """px 落在某候选 ±tol 内则原样返回；否则吸附到最近候选并标记调整。"""
    if not levels or px is None:
        return px, False
    nearest = min(levels, key=lambda l: abs(l["px"] - px))
    if abs(nearest["px"] - px) <= nearest["px"] * tol:
        return px, False
    return nearest["px"], True


def fmt_levels(cl: dict[str, Any]) -> str:
    """一行文本给提示词：现价 + 各候选。"""
    if not cl.get("levels"):
        return "无K线，候选价位不可用"
    parts = [f"{l['kind']}={l['px']}" for l in cl["levels"]]
    return f"现价 {cl['price']}  近20日振幅均值 {cl['atr_pct']}%  候选价位: " + " ".join(parts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NO_PROXY='*' python3 tests/test_picks_levels.py`
Expected: `OK — test_picks_levels 全过（N 断言）`

- [ ] **Step 5: 提交**

```bash
git add picks_levels.py tests/test_picks_levels.py
git commit -m "feat(picks): 候选价位纯函数（区间高低/均线/摆动点/ATR/短线簇）

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: picks_store.py 观点账本 + 记忆块

**Files:**
- Create: `picks_store.py`
- Test: `tests/test_picks_store.py`

**Interfaces:**
- Consumes: `userctx.user_path`, `userctx.shared_path`, `userctx.open_db`；`news_store.is_trading_day(d: date) -> bool`。
- Produces:
  - `HORIZON_DAYS = {"short": 5, "mid": 40, "long": 250}`；`SCOPES = ("public", "watchlist")`。
  - `DB_PATHS: dict[str, str | None]` 测试覆盖钩子（默认 None，运行时按 scope 解析）。
  - `init(scope: str) -> None`
  - `apply(scope: str, proposed: dict, today: str, run_id: str) -> dict` 强制改口规则并落库，返回落库行。`proposed` 键：`code, name, horizon, stance, entry_lo, entry_hi, exit_lo, exit_hi, stop, thesis, trigger_note, basis_json, decision, trigger, px_at_call`。
  - `current(scope: str, code: str = "") -> list[dict]` 状态 open 的行。
  - `chain(scope: str, code: str, limit: int = 50) -> list[dict]` 按时间倒序。
  - `expire(scope: str, today: str) -> int`
  - `staple(scope: str, code: str, bars: list[dict], today: str) -> int` 结果贴回。
  - `rollup(scope: str, code: str, before: str) -> dict`
  - `memory_block(scope: str, code: str, price: float | None, today: str) -> str`
  - `valid_until(horizon: str, start: str) -> str` 按交易日数数。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_picks_store.py  观点账本：修改链、改口规则、到期、结果贴回、记忆块。"""
import os, sys, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_store as ps

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

import news_store
# 节假日表在缓存缺失时会联网抓；测试一律按纯工作日算，零网络
news_store.is_trading_day = lambda d=None: (d or dt.date.today()).weekday() < 5

TMP = tempfile.mkdtemp()
ps.DB_PATHS["watchlist"] = os.path.join(TMP, "picks.db")
ps.DB_PATHS["public"] = os.path.join(TMP, "picks_public.db")
ps.init("watchlist"); ps.init("public")

def prop(**kw):
    base = {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy",
            "entry_lo": 9.5, "entry_hi": 9.8, "exit_lo": 10.8, "exit_hi": 11.2, "stop": 9.2,
            "thesis": "低吸", "trigger_note": "破 9.2 走", "basis_json": "{}",
            "decision": "new", "trigger": "none", "px_at_call": 9.9}
    base.update(kw)
    return base

def test_new_and_keep_chain():
    r1 = ps.apply("watchlist", prop(), "2026-09-16", "run1")
    ck(r1["decision"] == "new" and r1["status"] == "open", "首条为 new/open")
    r2 = ps.apply("watchlist", prop(decision="keep"), "2026-09-17", "run2")
    ck(r2["parent_id"] == r1["id"] and r2["decision"] == "keep", "keep 生成新行指向旧行")
    ck(len(ps.current("watchlist", "600519")) == 1, "只有一条 open")
    ck(ps.chain("watchlist", "600519")[0]["id"] == r2["id"], "chain 倒序最新在前")

def test_revise_without_trigger_downgraded():
    r = ps.apply("watchlist", prop(decision="revise", trigger="none", entry_lo=9.0, entry_hi=9.2), "2026-09-18", "run3")
    ck(r["decision"] == "keep", "无触发的 revise 降为 keep")
    ck(r["entry_lo"] == 9.5, "keep 沿用上一条的价位")

def test_revise_with_thesis_broken_allowed():
    r = ps.apply("watchlist", prop(decision="revise", trigger="thesis_broken", stance="watch", entry_lo=9.0, entry_hi=9.2), "2026-09-19", "run4")
    ck(r["decision"] == "revise" and r["entry_lo"] == 9.0 and r["stance"] == "watch", "有触发的 revise 生效")

def test_target_hit_requires_touched():
    r = ps.apply("watchlist", prop(decision="revise", trigger="target_hit", stance="sell"), "2026-09-20", "run5")
    ck(r["decision"] == "keep", "上一条 touched 不是 exit 时 target_hit 降为 keep")

def test_withdraw():
    r = ps.apply("watchlist", prop(decision="withdraw", trigger="expired"), "2026-09-21", "run6")
    ck(r["status"] == "withdrawn", "撤销行状态 withdrawn")
    ck(ps.current("watchlist", "600519") == [], "撤销后无 open")

def test_new_with_prev_open_becomes_keep_unless_trigger():
    ps.apply("watchlist", prop(code="000001", name="平安银行"), "2026-09-16", "r")
    r = ps.apply("watchlist", prop(code="000001", name="平安银行", decision="new", entry_lo=1.0, entry_hi=1.1), "2026-09-17", "r")
    ck(r["decision"] == "keep" and r["entry_lo"] == 9.5, "已有 open 时 new 视为 keep")

def test_valid_until_counts_trading_days():
    ck(ps.valid_until("short", "2026-09-16") == "2026-09-23", "5 个交易日跨周末")
    ck(ps.valid_until("mid", "2026-09-16") > "2026-11-01", "40 个交易日约两个月")

def test_expire():
    ps.apply("public", prop(code="300001", name="特锐德"), "2026-01-05", "r")
    n = ps.expire("public", "2026-03-01")
    ck(n == 1 and ps.current("public", "300001") == [], "过期行标 expired")

def test_staple_outcome():
    ps.apply("watchlist", prop(code="002230", name="科大讯飞", entry_lo=9.5, entry_hi=9.8, exit_lo=10.8, exit_hi=11.2, stop=9.2, px_at_call=10.0), "2026-09-16", "r")
    bars = [{"date": "2026-09-16", "open": 10, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1},
            {"date": "2026-09-17", "open": 10, "high": 10.3, "low": 9.6, "close": 9.7, "volume": 1},
            {"date": "2026-09-18", "open": 9.7, "high": 11.0, "low": 9.6, "close": 10.9, "volume": 1}]
    n = ps.staple("watchlist", "002230", bars, "2026-09-18")
    row = ps.current("watchlist", "002230")[0]
    ck(n >= 1 and row["max_up"] == 10.0 and row["max_dn"] == -4.0, f"最高/最低相对给出价: {row['max_up']} {row['max_dn']}")
    ck(row["touched"] == "exit", "碰到卖点区间记 exit")

def test_memory_block_window_and_rollup():
    for i in range(15):
        ps.apply("watchlist", prop(code="600036", name="招商银行", decision="keep" if i else "new"), f"2026-09-{i+1:02d}", "r")
    blk = ps.memory_block("watchlist", "600036", 9.9, "2026-09-16")
    ck(blk.count("\n") <= 20, "近窗最多 12 条加标题")
    ck("招商银行" in blk and "keep" in blk or "维持" in blk, "含决定")
    old = ps.apply("watchlist", prop(code="601318", name="中国平安", entry_lo=9.5, entry_hi=9.8), "2025-01-10", "r")
    blk2 = ps.memory_block("watchlist", "601318", 9.6, "2026-09-16")
    ck("远期" in blk2 and "9.5" in blk2, "60 天外价位相近的记录被挑出")
    rl = ps.rollup("watchlist", "601318", "2026-07-18")
    ck(rl["n"] == 1, "远期汇总计数")

if __name__ == "__main__":
    for fn in (test_new_and_keep_chain, test_revise_without_trigger_downgraded, test_revise_with_thesis_broken_allowed,
               test_target_hit_requires_touched, test_withdraw, test_new_with_prev_open_becomes_keep_unless_trigger,
               test_valid_until_counts_trading_days, test_expire, test_staple_outcome, test_memory_block_window_and_rollup):
        fn()
    print(f"OK — test_picks_store 全过（{N[0]} 断言）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_picks_store.py`
Expected: `ModuleNotFoundError: No module named 'picks_store'`

- [ ] **Step 3: 实现**

```python
"""观点账本：每条推荐/买卖点一行，修改不覆盖旧行而是链上新行；改口规则在这里强制。

两个 scope：public（全站三周期，data/picks_public.db）、watchlist（个人自选股，
data/users/<uid>/picks.db）。表结构相同。
记忆块 memory_block()：近 60 天最多 12 条全文；更早只放确定性汇总加至多 3 条相关记录。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Any

import news_store
import userctx

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"short": 5, "mid": 40, "long": 250}
SCOPES = ("public", "watchlist")
DECISIONS = ("new", "keep", "revise", "withdraw")
TRIGGERS = ("none", "stop_hit", "target_hit", "expired", "thesis_broken")
WINDOW_DAYS = 60
WINDOW_MAX = 12
PICKUP_MAX = 3
PICKUP_TOL = 0.05

DB_PATHS: dict[str, str | None] = {"public": None, "watchlist": None}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, created_at TEXT, scope TEXT,
  code TEXT, name TEXT, horizon TEXT, stance TEXT,
  entry_lo REAL, entry_hi REAL, exit_lo REAL, exit_hi REAL, stop REAL,
  valid_from TEXT, valid_until TEXT,
  thesis TEXT, trigger_note TEXT, basis_json TEXT,
  decision TEXT, trigger TEXT, parent_id INTEGER, status TEXT,
  px_at_call REAL, max_up REAL, max_dn REAL, ret_5d REAL, ret_20d REAL, touched TEXT DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_calls_code ON calls(scope, code, created_at);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(scope, status);
"""


def _path(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"未知 scope: {scope}")
    if DB_PATHS.get(scope):
        return DB_PATHS[scope]  # type: ignore[return-value]
    return userctx.shared_path("picks_public.db") if scope == "public" else userctx.user_path("picks.db")


def _conn(scope: str) -> sqlite3.Connection:
    return userctx.open_db(_path(scope))


def init(scope: str) -> None:
    with _conn(scope) as c:
        c.executescript(_SCHEMA)


def valid_until(horizon: str, start: str) -> str:
    """从 start 起数 HORIZON_DAYS 个交易日。"""
    n = HORIZON_DAYS.get(horizon, 5)
    d = dt.date.fromisoformat(start)
    cnt = 0
    while cnt < n:
        d += dt.timedelta(days=1)
        if news_store.is_trading_day(d):
            cnt += 1
    return d.isoformat()


def current(scope: str, code: str = "") -> list[dict[str, Any]]:
    with _conn(scope) as c:
        sql = "SELECT * FROM calls WHERE scope=? AND status='open'"
        args: list[Any] = [scope]
        if code:
            sql += " AND code=?"
            args.append(code)
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at DESC", args)]


def chain(scope: str, code: str, limit: int = 50) -> list[dict[str, Any]]:
    with _conn(scope) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (scope, code, limit))]


def _enforce(prev: dict[str, Any] | None, p: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """改口规则。返回 (decision, trigger, 价位来源行)。"""
    decision = p.get("decision") if p.get("decision") in DECISIONS else "new"
    trigger = p.get("trigger") if p.get("trigger") in TRIGGERS else "none"
    if prev is None:
        return "new", "none", p
    if decision == "new":
        decision = "keep"
    if decision in ("revise", "withdraw") and trigger == "none":
        logger.warning("picks: %s %s 无触发却要 %s，按 keep 处理", p.get("code"), p.get("horizon"), decision)
        return "keep", "none", prev
    if decision == "revise" and trigger == "stop_hit" and prev.get("touched") != "stop":
        logger.warning("picks: %s 称止损触发但结果未记录到，按 keep", p.get("code"))
        return "keep", "none", prev
    if decision == "revise" and trigger == "target_hit" and prev.get("touched") != "exit":
        logger.warning("picks: %s 称目标达成但结果未记录到，按 keep", p.get("code"))
        return "keep", "none", prev
    if decision == "keep":
        return "keep", "none", prev
    return decision, trigger, p


def apply(scope: str, proposed: dict[str, Any], today: str, run_id: str) -> dict[str, Any]:
    """按改口规则落库，返回落库行。"""
    code, horizon = proposed["code"], proposed.get("horizon", "short")
    prev_rows = [r for r in current(scope, code) if r["horizon"] == horizon]
    prev = prev_rows[0] if prev_rows else None
    decision, trigger, src = _enforce(prev, proposed)
    status = "withdrawn" if decision == "withdraw" else "open"
    vf = today if decision in ("new", "revise") else (prev or {}).get("valid_from", today)
    vu = valid_until(horizon, today) if decision in ("new", "revise") else (prev or {}).get("valid_until", valid_until(horizon, today))
    row = {
        "run_id": run_id, "created_at": today, "scope": scope, "code": code,
        "name": proposed.get("name") or (prev or {}).get("name", ""), "horizon": horizon,
        "stance": src.get("stance", "watch"),
        "entry_lo": src.get("entry_lo"), "entry_hi": src.get("entry_hi"),
        "exit_lo": src.get("exit_lo"), "exit_hi": src.get("exit_hi"), "stop": src.get("stop"),
        "valid_from": vf, "valid_until": vu,
        "thesis": proposed.get("thesis", "") if decision != "keep" else (prev or {}).get("thesis", ""),
        "trigger_note": src.get("trigger_note", ""),
        "basis_json": src.get("basis_json", "{}") if isinstance(src.get("basis_json"), str) else json.dumps(src.get("basis_json") or {}, ensure_ascii=False),
        "decision": decision, "trigger": trigger,
        "parent_id": (prev or {}).get("id"), "status": status,
        "px_at_call": proposed.get("px_at_call"),
        "max_up": None, "max_dn": None, "ret_5d": None, "ret_20d": None, "touched": "none",
    }
    cols = ",".join(row)
    with _conn(scope) as c:
        if prev:
            c.execute("UPDATE calls SET status='superseded' WHERE id=?", (prev["id"],))
        cur = c.execute(f"INSERT INTO calls({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
        row["id"] = cur.lastrowid
    return row


def expire(scope: str, today: str) -> int:
    with _conn(scope) as c:
        return c.execute("UPDATE calls SET status='expired' WHERE scope=? AND status='open' AND valid_until<?",
                         (scope, today)).rowcount


def staple(scope: str, code: str, bars: list[dict[str, Any]], today: str) -> int:
    """把给出后的走势贴回近 60 天内的行：最高/最低相对给出价、5 日/20 日收益、碰到哪个区间。"""
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    n = 0
    with _conn(scope) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at>=? AND status IN ('open','superseded')",
            (scope, code, since))]
        for r in rows:
            after = [b for b in bars if str(b.get("date", ""))[:10] > r["created_at"]]
            px0 = r.get("px_at_call")
            if not after or not px0:
                continue
            hi = max(float(b["high"]) for b in after)
            lo = min(float(b["low"]) for b in after)
            closes = [float(b["close"]) for b in after]
            ret5 = round((closes[min(4, len(closes) - 1)] / px0 - 1) * 100, 2) if len(closes) >= 5 else None
            ret20 = round((closes[19] / px0 - 1) * 100, 2) if len(closes) >= 20 else None
            touched = "none"
            if r.get("stop") and lo <= r["stop"]:
                touched = "stop"
            elif r.get("exit_lo") and hi >= r["exit_lo"]:
                touched = "exit"
            elif r.get("entry_hi") and lo <= r["entry_hi"]:
                touched = "entry"
            c.execute("UPDATE calls SET max_up=?, max_dn=?, ret_5d=?, ret_20d=?, touched=? WHERE id=?",
                      (round((hi / px0 - 1) * 100, 2), round((lo / px0 - 1) * 100, 2), ret5, ret20, touched, r["id"]))
            n += 1
    return n


def rollup(scope: str, code: str, before: str) -> dict[str, Any]:
    """before 之前的确定性汇总：次数、买点被碰比例、20 日收益均值与最差、最近立场。"""
    with _conn(scope) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at<? AND decision!='keep' ORDER BY created_at",
            (scope, code, before))]
    if not rows:
        return {"n": 0}
    hit = sum(1 for r in rows if r.get("touched") in ("entry", "exit", "stop"))
    r20 = [r["ret_20d"] for r in rows if r.get("ret_20d") is not None]
    return {"n": len(rows), "entry_hit_rate": round(hit / len(rows), 2),
            "avg_ret20": round(sum(r20) / len(r20), 2) if r20 else None,
            "worst_ret20": min(r20) if r20 else None, "last_stance": rows[-1].get("stance")}


def _fmt_row(r: dict[str, Any]) -> str:
    res = ""
    if r.get("max_up") is not None:
        res = f" 结果:最高{r['max_up']:+}% 最低{r['max_dn']:+}%"
        if r.get("ret_5d") is not None:
            res += f" 5日{r['ret_5d']:+}%"
        if r.get("touched") and r["touched"] != "none":
            res += f" 碰到{r['touched']}"
    return (f"{r['created_at']} {r['horizon']} {r['stance']} 买{r.get('entry_lo')}-{r.get('entry_hi')} "
            f"卖{r.get('exit_lo')}-{r.get('exit_hi')} 止{r.get('stop')} {r['decision']}/{r['trigger']}{res}")


def memory_block(scope: str, code: str, price: float | None, today: str) -> str:
    """给模型的两层记忆：近窗全文 + 远期汇总与相关挑选。"""
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    with _conn(scope) as c:
        recent = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (scope, code, since, WINDOW_MAX))]
        old = [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE scope=? AND code=? AND created_at<? AND decision!='keep' ORDER BY created_at DESC",
            (scope, code, since))]
    lines = []
    if recent:
        lines.append(f"【{recent[0]['name']} 近 60 天观点（新在前）】")
        lines += [_fmt_row(r) for r in recent]
    if old:
        rl = rollup(scope, code, since)
        lines.append(f"【远期汇总】共 {rl['n']} 次，碰到比例 {rl.get('entry_hit_rate')}，20日收益均值 {rl.get('avg_ret20')}，最差 {rl.get('worst_ret20')}，最近立场 {rl.get('last_stance')}")
        picked = []
        for r in old:
            near = price and any(v and abs(v - price) <= price * PICKUP_TOL
                                 for v in (r.get("entry_lo"), r.get("entry_hi"), r.get("exit_lo"), r.get("exit_hi")))
            hit = r.get("touched") in ("stop", "exit")
            if near or hit:
                picked.append(r)
            if len(picked) >= PICKUP_MAX:
                break
        if picked:
            lines.append("【远期相关记录】")
            lines += [_fmt_row(r) for r in picked]
    return "\n".join(lines) if lines else "（该股尚无历史观点）"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NO_PROXY='*' python3 tests/test_picks_store.py`
Expected: `OK — test_picks_store 全过（N 断言）`。若 `test_memory_block_window_and_rollup` 里 "远期" 断言失败，检查 `old` 查询条件 `decision!='keep'` 与测试里首条为 `new`。

- [ ] **Step 5: 提交**

```bash
git add picks_store.py tests/test_picks_store.py
git commit -m "feat(picks): 观点账本（修改链/改口规则强制/到期/结果贴回/两层记忆块）

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: llm_picks.py 提示词与返回校验

**Files:**
- Create: `llm_picks.py`
- Test: `tests/test_llm_picks.py`

**Interfaces:**
- Consumes: `llm._chat(messages, *, json_mode=True, max_tokens=8000, model="") -> str`；`llm._parse_json(text) -> dict`；`llm._system_prompt() -> (str, int)`；`llm._market_ctx_block(market_ctx) -> str`；`picks_levels.snap`、`picks_levels.fmt_levels`。
- Produces:
  - `horizon_picks(horizon: str, candidates: list[dict], levels: dict[str, dict], memory: dict[str, str], market_ctx: dict | None, capital: float) -> list[dict]` 已校验的 calls。
  - `watchlist_points(rows: list[dict], levels: dict[str, dict], memory: dict[str, str], market_ctx: dict | None, capital: float) -> list[dict]`
  - `validate_calls(parsed: dict, levels: dict[str, dict], allowed: set[str]) -> list[dict]` 按条校验（代码不在 allowed 丢弃；价位 snap；decision/trigger 非法置默认；basis 转 json 字符串）。
  - 候选行字段：`code, name, primary, sub, price, pe_ttm, pb, vol, cum20, range_pos, net20, turnover, lot_cost`（与 `screening._screen_rows` 一致）。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_llm_picks.py  提示词函数：monkeypatch llm._chat，验证校验逻辑。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import llm
import llm_picks as lp

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

LEVELS = {"600519": {"price": 10.0, "atr_pct": 2.0, "levels": [{"px": 9.5, "kind": "lo20"}, {"px": 11.0, "kind": "hi20"}, {"px": 9.2, "kind": "atr_lo2"}]}}
CANDS = [{"code": "600519", "name": "贵州茅台", "primary": "食品", "sub": "白酒", "price": 10.0, "pe_ttm": 20, "pb": 5,
          "vol": 30, "cum20": 3.0, "range_pos": 40, "net20": 1.2, "turnover": 1.5, "lot_cost": 1000}]

def fake_chat(reply):
    def _c(messages, **kw):
        _c.last = messages
        return json.dumps(reply, ensure_ascii=False)
    return _c

def test_validate_snaps_and_drops_unknown():
    parsed = {"calls": [
        {"code": "600519", "name": "贵州茅台", "horizon": "short", "stance": "buy", "entry": [9.7, 9.9], "exit": [10.9, 11.3],
         "stop": 8.0, "decision": "bogus", "trigger": "xx", "thesis": "t", "trigger_note": "n", "basis": {"signals": ["区间位置"], "rules": ["R1"]}},
        {"code": "000000", "name": "不在候选", "horizon": "short", "stance": "buy", "entry": [1, 2], "exit": [3, 4], "stop": 0.5}]}
    out = lp.validate_calls(parsed, LEVELS, {"600519"})
    ck(len(out) == 1, "不在候选的被丢弃")
    c = out[0]
    ck(c["entry_lo"] == 9.5 and c["stop"] == 9.2, f"价位吸附到候选: {c['entry_lo']} {c['stop']}")
    ck(c["decision"] == "new" and c["trigger"] == "none", "非法 decision/trigger 置默认")
    ck(json.loads(c["basis_json"])["adjusted"] is True, "被调整过的标 adjusted")
    ck(isinstance(c["basis_json"], str), "basis 存字符串")

def test_horizon_picks_prompt_and_parse():
    reply = {"calls": [{"code": "600519", "name": "贵州茅台", "horizon": "mid", "stance": "buy", "entry": [9.5, 9.6],
                        "exit": [11.0, 11.0], "stop": 9.2, "decision": "new", "trigger": "none", "thesis": "板块强", "trigger_note": "破位走", "basis": {}}]}
    orig = llm._chat
    llm._chat = fake_chat(reply)
    try:
        out = lp.horizon_picks("mid", CANDS, LEVELS, {"600519": "（该股尚无历史观点）"}, {"regime": "震荡"}, 10000)
        txt = llm._chat.last[1]["content"]
        ck("中线" in txt and "候选价位" in txt and "尚无历史观点" in txt, "提示词含周期、候选价位、记忆块")
        ck("维持" in txt and "触发" in txt, "提示词写明改口规则")
        ck(out[0]["horizon"] == "mid" and out[0]["px_at_call"] == 10.0, "输出带 horizon 与给出价")
    finally:
        llm._chat = orig

def test_watchlist_points_bad_json_returns_empty():
    orig = llm._chat
    llm._chat = lambda messages, **kw: "not json"
    try:
        out = lp.watchlist_points(CANDS, LEVELS, {}, None, 10000)
        ck(out == [], "解析失败返回空列表而不是抛")
    finally:
        llm._chat = orig

if __name__ == "__main__":
    for fn in (test_validate_snaps_and_drops_unknown, test_horizon_picks_prompt_and_parse, test_watchlist_points_bad_json_returns_empty):
        fn()
    print(f"OK — test_llm_picks 全过（{N[0]} 断言）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_llm_picks.py`
Expected: `ModuleNotFoundError: No module named 'llm_picks'`

- [ ] **Step 3: 实现**

```python
"""三周期选股与自选股买卖点的提示词，复用 llm._chat。返回按条校验，不整批作废。"""
from __future__ import annotations

import json
import logging
from typing import Any

import llm
import picks_levels
import picks_store

logger = logging.getLogger(__name__)

HORIZON_CN = {"short": "短线（1 到 5 个交易日，交易活跃、低吸高抛，给具体价位）",
              "mid": "中线（2 到 8 周，板块轮动、趋势刚起，价位区间加趋势破坏即离场）",
              "long": "长线（3 到 12 个月，基本面与估值，分批建仓区间）"}

_RULES = """改口规则（必须遵守）：对已有观点的股票，decision 只能是 keep（维持）/ revise（修改）/ withdraw（撤销）；
revise 和 withdraw 必须给 trigger，且只能是 stop_hit（止损触发）/ target_hit（目标达成）/ expired（周期到期）/
thesis_broken（新信息推翻），并在 trigger_note 写明依据；没有触发事件就只能 keep。没有历史观点的股票 decision=new。
价位只能从候选价位里选（可取两个候选之间构成区间），不要编造。"""

_SCHEMA = """严格返回 JSON：
{"calls":[{"code":"","name":"","horizon":"short|mid|long","stance":"buy|watch|avoid|sell",
  "entry":[低,高],"exit":[低,高],"stop":价,
  "decision":"new|keep|revise|withdraw","trigger":"none|stop_hit|target_hit|expired|thesis_broken",
  "thesis":"理由(60字内)","trigger_note":"什么情况会改口(30字内)",
  "basis":{"signals":["区间位置"],"rules":["R12"]}}]}"""


def _rows_table(rows: list[dict[str, Any]]) -> str:
    head = "代码 名称 一级 二级 现价 PE PB 年化波动% 20日涨% 区间位置% 主力20日亿 换手% 1手成本"
    lines = [head]
    for r in rows:
        lines.append(" ".join(str(r.get(k, "")) for k in ("code", "name", "primary", "sub", "price", "pe_ttm", "pb",
                                                          "vol", "cum20", "range_pos", "net20", "turnover", "lot_cost")))
    return "\n".join(lines)


def _levels_block(rows: list[dict[str, Any]], levels: dict[str, dict], memory: dict[str, str]) -> str:
    parts = []
    for r in rows:
        c = r["code"]
        parts.append(f"[{c} {r.get('name','')}] {picks_levels.fmt_levels(levels.get(c) or {})}\n{memory.get(c) or '（该股尚无历史观点）'}")
    return "\n".join(parts)


def validate_calls(parsed: dict[str, Any], levels: dict[str, dict], allowed: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in (parsed or {}).get("calls") or []:
        code = str(c.get("code", "")).strip()
        if code not in allowed:
            logger.info("picks: 丢弃不在候选内的 %s", code)
            continue
        lv = (levels.get(code) or {}).get("levels") or []
        adjusted = False

        def _s(v: Any) -> float | None:
            nonlocal adjusted
            try:
                px = float(v)
            except (TypeError, ValueError):
                return None
            px2, adj = picks_levels.snap(px, lv)
            adjusted = adjusted or adj
            return px2

        entry = c.get("entry") or [None, None]
        exit_ = c.get("exit") or [None, None]
        basis = c.get("basis") if isinstance(c.get("basis"), dict) else {}
        row = {
            "code": code, "name": c.get("name", ""),
            "horizon": c.get("horizon") if c.get("horizon") in picks_store.HORIZON_DAYS else "short",
            "stance": c.get("stance") if c.get("stance") in ("buy", "watch", "avoid", "sell") else "watch",
            "entry_lo": _s(entry[0]), "entry_hi": _s(entry[1] if len(entry) > 1 else entry[0]),
            "exit_lo": _s(exit_[0]), "exit_hi": _s(exit_[1] if len(exit_) > 1 else exit_[0]),
            "stop": _s(c.get("stop")),
            "decision": c.get("decision") if c.get("decision") in picks_store.DECISIONS else "new",
            "trigger": c.get("trigger") if c.get("trigger") in picks_store.TRIGGERS else "none",
            "thesis": str(c.get("thesis", ""))[:200], "trigger_note": str(c.get("trigger_note", ""))[:100],
            "px_at_call": (levels.get(code) or {}).get("price"),
        }
        basis["adjusted"] = adjusted
        row["basis_json"] = json.dumps(basis, ensure_ascii=False)
        out.append(row)
    return out


def _ask(prompt: str, max_tokens: int) -> dict[str, Any]:
    try:
        text = llm._chat([{"role": "system", "content": llm._system_prompt()[0]},
                          {"role": "user", "content": prompt}], max_tokens=max_tokens)
        return llm._parse_json(text)
    except (llm.LLMError, ValueError) as e:
        logger.warning("picks: 模型调用或解析失败: %s", e)
        return {}


def horizon_picks(horizon: str, candidates: list[dict[str, Any]], levels: dict[str, dict],
                  memory: dict[str, str], market_ctx: dict[str, Any] | None, capital: float) -> list[dict[str, Any]]:
    prompt = f"""请在下面的候选里为【{HORIZON_CN[horizon]}】选出最值得关注的 5 只，并给每只买点区间、卖点区间、止损。
{llm._market_ctx_block(market_ctx)}
可用资金约 {int(capital)} 元。
【候选】
{_rows_table(candidates)}
【每只的候选价位与历史观点】
{_levels_block(candidates, levels, memory)}
{_RULES}
{_SCHEMA}
calls 恰好 5 条，horizon 填 {horizon}。"""
    parsed = _ask(prompt, 9000)
    out = validate_calls(parsed, levels, {r["code"] for r in candidates})
    for r in out:
        r["horizon"] = horizon
    return out[:5]


def watchlist_points(rows: list[dict[str, Any]], levels: dict[str, dict], memory: dict[str, str],
                     market_ctx: dict[str, Any] | None, capital: float) -> list[dict[str, Any]]:
    prompt = f"""下面是我的自选股。对每一只：判断它现在最适合哪个周期（short/mid/long），给出该周期下的买点区间、卖点区间、止损与立场；
有历史观点的先核对历史再决定 keep / revise / withdraw。
{llm._market_ctx_block(market_ctx)}
可用资金约 {int(capital)} 元。周期定义：短线 1 到 5 个交易日；中线 2 到 8 周；长线 3 到 12 个月。
【自选股】
{_rows_table(rows)}
【每只的候选价位与历史观点】
{_levels_block(rows, levels, memory)}
{_RULES}
{_SCHEMA}
calls 覆盖全部自选股，每只一条。"""
    parsed = _ask(prompt, 9000)
    return validate_calls(parsed, levels, {r["code"] for r in rows})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NO_PROXY='*' python3 tests/test_llm_picks.py`
Expected: `OK — test_llm_picks 全过（N 断言）`

- [ ] **Step 5: 提交**

```bash
git add llm_picks.py tests/test_llm_picks.py
git commit -m "feat(picks): 三周期与自选股提示词 + 返回按条校验（价位吸附/决定与触发合法性）

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: picks_pipeline.py 候选池与富化

**Files:**
- Create: `picks_pipeline.py`（本任务只写候选池部分，Task 5 续写运行部分）
- Test: `tests/test_picks_pipeline.py`

**Interfaces:**
- Consumes: `review.store.latest() -> dict | None`（`raw_theme` 为 `[{code,name,price,pct,reason,...}]`）；`datasources.sina_all_stocks() -> list[dict]`（含 `code,name,price,amount,turnover,float_mcap,pe_ttm,pb`）；`universe_store.sector_ranking(date="", kind="", limit=30) -> list[dict]`（含 `sector, avg_chg, leader_code, leader_name`）；`universe_store.codes_of(focus) -> list[str]`；`universe_store.sector_of(code)`；`screening._metrics_of(codes) -> dict[code, {vol,cum20,range_pos,net20}]`；`datasources.tencent_quote(codes) -> dict[code, {name,price,pe_ttm,pb,turnover,lot_cost}]`；`datasources.financial_summary(code) -> list[{period,revenue_yi,revenue_yoy,profit_yi,profit_yoy}]`；`datasources.sina_kline(code, num) -> bars`；`picks_levels.candidate_levels`。
- Produces:
  - `POOL_N = 30`
  - `short_pool(n: int = POOL_N) -> list[str]`、`mid_pool(n)`、`long_pool(n)` 返回代码列表。
  - `enrich(codes: list[str], short: bool = False) -> tuple[list[dict], dict[str, dict]]` 返回 (rows, levels_by_code)，rows 字段同 `screening._screen_rows`。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_picks_pipeline.py  候选池与富化：全部取数 monkeypatch。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picks_pipeline as pp

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

def bars(n=70, base=10.0):
    return [{"date": f"2026-01-{i+1:02d}", "open": base, "high": base * 1.02, "low": base * 0.98, "close": base + i * 0.01, "volume": 1} for i in range(n)]

def setup_fakes():
    pp.review_store.latest = lambda: {"raw_theme": [{"code": "603010", "name": "万盛股份", "pct": 10.0}, {"code": "300001", "name": "特锐德", "pct": 9.9}]}
    pp.ds.sina_all_stocks = lambda: [
        {"code": "603010", "name": "万盛股份", "price": 12, "turnover": 20, "amount": 5e8, "pe_ttm": 30, "pb": 3},
        {"code": "600000", "name": "浦发银行", "price": 8, "turnover": 0.5, "amount": 1e8, "pe_ttm": 5, "pb": 0.5},
        {"code": "000002", "name": "万科A", "price": 7, "turnover": 3, "amount": 3e8, "pe_ttm": 8, "pb": 0.7},
        {"code": "300750", "name": "宁德时代", "price": 200, "turnover": 6, "amount": 9e8, "pe_ttm": 25, "pb": 4}]
    pp.universe_store.sector_ranking = lambda date="", kind="", limit=30: [{"sector": "电池", "avg_chg": 3.1, "leader_code": "300750", "leader_name": "宁德时代"}]
    pp.universe_store.codes_of = lambda focus="", eligible_only=True: ["300750", "603010"] if focus == "电池" else ["603010", "600000", "000002", "300750"]
    pp.universe_store.sector_of = lambda code: ("电池", "锂电") if code == "300750" else ("化工", "阻燃")
    pp.screening._metrics_of = lambda codes: {c: {"vol": 30, "cum20": 2, "range_pos": 40, "net20": 1.0 if c == "300750" else -0.5} for c in codes}
    pp.ds.tencent_quote = lambda codes, workers=8: {c: {"name": "N" + c, "price": 10.0, "pe_ttm": 10, "pb": 1, "turnover": 2, "lot_cost": 1000} for c in codes}
    pp.ds.financial_summary = lambda code, periods=4: [{"period": "2026-06-30", "revenue_yoy": 12.0, "profit_yoy": 8.0}] if code != "600000" else [{"period": "2026-06-30", "revenue_yoy": -3.0, "profit_yoy": 1.0}]
    pp.ds.sina_kline = lambda code, num=120, scale=240: bars()

def test_short_pool_prefers_theme_and_turnover():
    setup_fakes()
    p = pp.short_pool(3)
    ck(p[0] in ("603010", "300001") and len(p) <= 3, f"题材池优先: {p}")
    ck("600000" not in p, "换手最低的不进短线池")

def test_mid_pool_uses_sector_leaders_and_flow():
    setup_fakes()
    p = pp.mid_pool(3)
    ck("300750" in p, f"板块龙头在中线池: {p}")

def test_long_pool_filters_by_valuation_and_growth():
    setup_fakes()
    p = pp.long_pool(3)
    ck("600000" not in p and "000002" in p, f"营收负增长被剔除、低估值双正保留: {p}")

def test_enrich_rows_and_levels():
    setup_fakes()
    rows, levels = pp.enrich(["603010", "300750"], short=True)
    ck(len(rows) == 2 and rows[0]["code"] == "603010" and rows[0]["lot_cost"] == 1000, "rows 字段齐")
    ck("603010" in levels and levels["603010"]["levels"], "每只有候选价位")
    ck(rows[0]["primary"] == "化工", "带板块归属")

if __name__ == "__main__":
    for fn in (test_short_pool_prefers_theme_and_turnover, test_mid_pool_uses_sector_leaders_and_flow,
               test_long_pool_filters_by_valuation_and_growth, test_enrich_rows_and_levels):
        fn()
    print(f"OK — test_picks_pipeline 全过（{N[0]} 断言）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_picks_pipeline.py`
Expected: `ModuleNotFoundError: No module named 'picks_pipeline'`

- [ ] **Step 3: 实现（候选池部分）**

```python
"""三周期选股流水线：候选池、富化、运行、结算、定时循环。

候选池都压到 POOL_N 只再喂模型（每周期一次提示词）。取数全部来自现有模块，
测试里整体 monkeypatch。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Callable

import datasources as ds
import picks_levels
import picks_store
import screening
import universe_store
import userctx
from review import store as review_store

logger = logging.getLogger(__name__)

POOL_N = 30
KLINE_N = 90
LONG_PREFILTER = 120


def _snapshot() -> list[dict[str, Any]]:
    try:
        return ds.sina_all_stocks() or []
    except Exception as e:  # noqa: BLE001 取数失败按空处理，池子退化不崩
        logger.warning("picks: 全市场快照失败: %s", e)
        return []


def short_pool(n: int = POOL_N) -> list[str]:
    """短线：当天复盘的题材/涨停池优先，再补全市场换手率最高的。"""
    out: list[str] = []
    env = review_store.latest() or {}
    for r in (env.get("raw_theme") or []):
        c = str(r.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            return out[:n]
    snap = [s for s in _snapshot() if s.get("turnover") and s.get("amount")]
    snap.sort(key=lambda s: (float(s["turnover"]), float(s["amount"])), reverse=True)
    for s in snap:
        c = str(s.get("code", ""))
        if c and c not in out:
            out.append(c)
        if len(out) >= n:
            break
    return out[:n]


def mid_pool(n: int = POOL_N) -> list[str]:
    """中线：排名前列板块的龙头与成员，按主力 20 日净流入为正筛。"""
    out: list[str] = []
    for row in universe_store.sector_ranking(limit=10):
        lead = str(row.get("leader_code") or "")
        if lead and lead not in out:
            out.append(lead)
        for c in universe_store.codes_of(row.get("sector", ""))[:8]:
            if c not in out:
                out.append(c)
        if len(out) >= n * 2:
            break
    if not out:
        return []
    m = screening._metrics_of(out)
    good = [c for c in out if (m.get(c) or {}).get("net20") is not None and m[c]["net20"] > 0]
    rest = [c for c in out if c not in good]
    return (good + rest)[:n]


def long_pool(n: int = POOL_N) -> list[str]:
    """长线：估值分位低（PE、PB 在快照里排前 LONG_PREFILTER）且营收、利润同比双正。"""
    snap = [s for s in _snapshot() if s.get("pe_ttm") and s["pe_ttm"] > 0 and s.get("pb") and s["pb"] > 0]
    snap.sort(key=lambda s: (float(s["pe_ttm"]) * float(s["pb"])))
    out: list[str] = []
    for s in snap[:LONG_PREFILTER]:
        c = str(s.get("code", ""))
        fin = ds.financial_summary(c) or []
        if not fin:
            continue
        f0 = fin[0]
        if (f0.get("revenue_yoy") or 0) > 0 and (f0.get("profit_yoy") or 0) > 0:
            out.append(c)
        if len(out) >= n:
            break
    return out


def enrich(codes: list[str], short: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """行情 + 指标 + 板块 + 候选价位。"""
    if not codes:
        return [], {}
    quotes = ds.tencent_quote(codes)
    metrics = screening._metrics_of(codes)
    rows: list[dict[str, Any]] = []
    levels: dict[str, dict] = {}
    for c in codes:
        q = quotes.get(c) or {}
        m = metrics.get(c) or {}
        try:
            primary, sub = universe_store.sector_of(c)
        except Exception:  # noqa: BLE001 归属缺失不影响选股
            primary, sub = "", ""
        rows.append({"code": c, "name": q.get("name", c), "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"), "range_pos": m.get("range_pos"),
                     "net20": m.get("net20"), "turnover": q.get("turnover"), "lot_cost": q.get("lot_cost")})
        try:
            levels[c] = picks_levels.candidate_levels(ds.sina_kline(c, KLINE_N), short=short)
        except Exception as e:  # noqa: BLE001 单只 K 线失败不拖垮整批
            logger.warning("picks: %s K线失败: %s", c, e)
            levels[c] = {"price": q.get("price"), "atr_pct": None, "levels": []}
    return rows, levels
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NO_PROXY='*' python3 tests/test_picks_pipeline.py`
Expected: `OK — test_picks_pipeline 全过（N 断言）`

- [ ] **Step 5: 提交**

```bash
git add picks_pipeline.py tests/test_picks_pipeline.py
git commit -m "feat(picks): 三周期候选池（复盘题材池/板块龙头资金/估值与财报）与富化

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: picks_pipeline.py 运行、结算、定时循环

**Files:**
- Modify: `picks_pipeline.py`（在 Task 4 的基础上追加）
- Test: `tests/test_picks_pipeline.py`（追加用例）

**Interfaces:**
- Consumes: Task 2/3/4 全部；`store.load_watchlist() -> list[str]`（代码列表）；`profile_store.get_active() -> dict | None`（含 `cash`）；`userctx.as_user`、`userctx.fleet_uid`。
- Produces:
  - `run_public(market_ctx=None, horizons=("short","mid","long"), capital: float = 10000) -> dict` 返回 `{"run_id", "calls": int, "horizons": [...], "error": str|None}`。
  - `run_watchlist(market_ctx=None, capital: float | None = None) -> dict` 在当前用户上下文里跑。
  - `settle(scope: str, today: str) -> dict` 到期 + 结果贴回。
  - `acquire_lock(scope) -> bool` / `release_lock(scope)`（`data/.picks-running-<scope>`，30 分钟视为陈旧）。
  - `due_slot(now: dt.datetime, last: dict[str, str]) -> str` 返回 `"full"` / `"morning"` / `""`。
  - `loop_forever(uids_fn: Callable[[], list[str]], market_ctx_fn: Callable[[], dict | None] | None = None) -> None`

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 tests/test_picks_pipeline.py 的 __main__ 之前
import datetime as dt
import json
import tempfile
import picks_store as ps
import llm_picks

def setup_store():
    tmp = tempfile.mkdtemp()
    ps.DB_PATHS["public"] = os.path.join(tmp, "pub.db"); ps.DB_PATHS["watchlist"] = os.path.join(tmp, "wl.db")
    ps.init("public"); ps.init("watchlist")
    pp.LOCK_DIR = tmp
    pp.news_store.is_trading_day = lambda d=None: True   # 零网络；valid_until 与 due_slot 都走这里

def test_run_public_persists_calls():
    setup_fakes(); setup_store()
    llm_picks.horizon_picks = lambda horizon, cands, levels, memory, mctx, cap: [
        {"code": cands[0]["code"], "name": cands[0]["name"], "horizon": horizon, "stance": "buy", "entry_lo": 9.8, "entry_hi": 10.0,
         "exit_lo": 10.5, "exit_hi": 10.8, "stop": 9.5, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n",
         "basis_json": "{}", "px_at_call": 10.0}]
    r = pp.run_public(horizons=("short", "mid"))
    ck(r["calls"] == 2 and r["error"] is None, f"两周期各落 1 条: {r}")
    ck(len(ps.current("public")) == 2, "账本 open 两条")

def test_run_watchlist_uses_user_watchlist():
    setup_fakes(); setup_store()
    pp.store.load_watchlist = lambda: ["603010", "300750"]
    pp.profile_store.get_active = lambda: {"cash": 5000}
    llm_picks.watchlist_points = lambda rows, levels, memory, mctx, cap: [
        {"code": r["code"], "name": r["name"], "horizon": "short", "stance": "watch", "entry_lo": 9.8, "entry_hi": 10.0,
         "exit_lo": 10.5, "exit_hi": 10.8, "stop": 9.5, "decision": "new", "trigger": "none", "thesis": "t", "trigger_note": "n",
         "basis_json": "{}", "px_at_call": 10.0} for r in rows]
    r = pp.run_watchlist()
    ck(r["calls"] == 2, f"自选股两条: {r}")

def test_run_public_llm_failure_keeps_ledger():
    setup_fakes(); setup_store()
    llm_picks.horizon_picks = lambda *a, **k: []
    r = pp.run_public(horizons=("short",))
    ck(r["calls"] == 0 and ps.current("public") == [], "模型无返回时账本不动")

def test_lock_and_due():
    setup_store()
    ck(pp.acquire_lock("public") is True and pp.acquire_lock("public") is False, "锁互斥")
    pp.release_lock("public")
    ck(pp.acquire_lock("public") is True, "释放后可再拿"); pp.release_lock("public")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 16, 5), {}) == "full", "16:05 到点全量")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 16, 5), {"full": "2026-09-16"}) == "", "同日不重复")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 9, 10), {}) == "morning", "09:10 到点早盘")
    ck(pp.due_slot(dt.datetime(2026, 9, 16, 12, 0), {"morning": "2026-09-16"}) == "", "午间无事")
```

并把这些函数加进 `__main__` 的调用列表。

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_picks_pipeline.py`
Expected: `AttributeError: module 'picks_pipeline' has no attribute 'run_public'`

- [ ] **Step 3: 追加实现**

```python
# 追加到 picks_pipeline.py 末尾；顶部 import 补：import llm_picks, news_store, profile_store, store, threading

LOCK_DIR = userctx.DATA_DIR
LOCK_STALE_SEC = 1800
FULL_AT = (16, 0)
MORNING_AT = (9, 5)
_last: dict[str, str] = {}


def _lock_path(scope: str) -> str:
    return os.path.join(LOCK_DIR, f".picks-running-{scope}")


def acquire_lock(scope: str) -> bool:
    p = _lock_path(scope)
    try:
        if os.path.exists(p) and time.time() - os.path.getmtime(p) > LOCK_STALE_SEC:
            logger.warning("picks: 锁 %s 超过 30 分钟，视为陈旧覆盖", p)
            os.remove(p)
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logger.warning("picks: 锁文件不可用，放行: %s", e)
        return True


def release_lock(scope: str) -> None:
    try:
        os.remove(_lock_path(scope))
    except OSError:
        pass


def settle(scope: str, today: str) -> dict[str, Any]:
    """到期 + 结果贴回（拉日 K，只对近 60 天有记录的股票）。"""
    expired = picks_store.expire(scope, today)
    codes = sorted({r["code"] for r in picks_store.current(scope)})
    n = 0
    for c in codes:
        try:
            n += picks_store.staple(scope, c, ds.sina_kline(c, 40), today)
        except Exception as e:  # noqa: BLE001
            logger.warning("picks: %s 结算失败: %s", c, e)
    return {"expired": expired, "stapled": n}


def _persist(scope: str, calls: list[dict[str, Any]], today: str, run_id: str) -> int:
    n = 0
    for c in calls:
        try:
            picks_store.apply(scope, c, today, run_id)
            n += 1
        except Exception as e:  # noqa: BLE001 单条落库失败不影响其余
            logger.warning("picks: %s 落库失败: %s", c.get("code"), e)
    return n


def run_public(market_ctx: dict[str, Any] | None = None,
               horizons: tuple[str, ...] = ("short", "mid", "long"), capital: float = 10000) -> dict[str, Any]:
    today = dt.date.today().isoformat()
    run_id = f"pub-{today}-{int(time.time())}"
    picks_store.init("public")
    if not acquire_lock("public"):
        return {"run_id": run_id, "calls": 0, "horizons": [], "error": "已在生成"}
    try:
        settle("public", today)
        total = 0
        for h in horizons:
            pool = {"short": short_pool, "mid": mid_pool, "long": long_pool}[h]()
            rows, levels = enrich(pool, short=(h == "short"))
            memory = {r["code"]: picks_store.memory_block("public", r["code"], (levels.get(r["code"]) or {}).get("price"), today) for r in rows}
            calls = llm_picks.horizon_picks(h, rows, levels, memory, market_ctx, capital)
            total += _persist("public", calls, today, run_id)
        return {"run_id": run_id, "calls": total, "horizons": list(horizons), "error": None}
    except Exception as e:  # noqa: BLE001 整轮失败账本不动
        logger.exception("picks: 公共运行失败")
        return {"run_id": run_id, "calls": 0, "horizons": list(horizons), "error": str(e)}
    finally:
        release_lock("public")


def run_watchlist(market_ctx: dict[str, Any] | None = None, capital: float | None = None) -> dict[str, Any]:
    today = dt.date.today().isoformat()
    run_id = f"wl-{userctx.get_uid() or 'nouser'}-{today}-{int(time.time())}"
    picks_store.init("watchlist")
    codes = [str(c) for c in (store.load_watchlist() or []) if c]   # load_watchlist 返回代码列表
    if not codes:
        return {"run_id": run_id, "calls": 0, "error": "自选股为空"}
    if capital is None:
        capital = float((profile_store.get_active() or {}).get("cash") or 10000)
    try:
        settle("watchlist", today)
        rows, levels = enrich(codes, short=True)
        memory = {c: picks_store.memory_block("watchlist", c, (levels.get(c) or {}).get("price"), today) for c in codes}
        calls = llm_picks.watchlist_points(rows, levels, memory, market_ctx, capital)
        return {"run_id": run_id, "calls": _persist("watchlist", calls, today, run_id), "error": None}
    except Exception as e:  # noqa: BLE001
        logger.exception("picks: 自选股运行失败")
        return {"run_id": run_id, "calls": 0, "error": str(e)}


def due_slot(now: dt.datetime, last: dict[str, str]) -> str:
    """到点判定：16:00 后当天未跑全量返回 full；09:05 到 16:00 之间当天未跑早盘返回 morning。"""
    if not news_store.is_trading_day(now.date()):
        return ""
    today = now.date().isoformat()
    hm = (now.hour, now.minute)
    if hm >= FULL_AT and last.get("full") != today:
        return "full"
    if MORNING_AT <= hm < FULL_AT and last.get("morning") != today:
        return "morning"
    return ""


def loop_forever(uids_fn: Callable[[], list[str]],
                 market_ctx_fn: Callable[[], dict[str, Any] | None] | None = None) -> None:
    """定时循环：每 60 秒探一次到点。full = 三周期 + 每个账号自选股；morning = 短线 + 自选股。"""
    while True:
        try:
            slot = due_slot(dt.datetime.now(), _last)
            if slot:
                _last[slot] = dt.date.today().isoformat()
                mctx = market_ctx_fn() if market_ctx_fn else None
                horizons = ("short", "mid", "long") if slot == "full" else ("short",)
                logger.info("picks: 到点 %s，公共三周期 %s", slot, horizons)
                run_public(mctx, horizons)
                for uid in uids_fn():
                    with userctx.as_user(uid):
                        r = run_watchlist(mctx)
                        logger.info("picks: [%s] 自选股 %s", uid, r)
        except Exception as e:  # noqa: BLE001 循环绝不停摆
            logger.warning("picks: 定时循环异常: %s", e)
        time.sleep(60)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `NO_PROXY='*' python3 tests/test_picks_pipeline.py`
Expected: `OK — test_picks_pipeline 全过（N 断言）`（含新增 4 个用例）

- [ ] **Step 5: 提交**

```bash
git add picks_pipeline.py tests/test_picks_pipeline.py
git commit -m "feat(picks): 公共/自选股运行、结算、文件锁与 16:00/09:05 定时循环

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: picks_routes.py Blueprint + app.py / scheduler.py 接线

**Files:**
- Create: `picks_routes.py`
- Modify: `app.py`（`app = Flask(__name__)` 之后、`auth.init_app(app)` 之后加注册；`__main__` 块加本地定时线程）
- Modify: `scheduler.py`（`_shared_boot` 之后 `userctx.spawn` 定时循环）
- Test: `tests/test_picks_routes.py`

**Interfaces:**
- Consumes: `picks_store.current/chain`、`picks_pipeline.run_public/run_watchlist/acquire_lock`、`auth.get_user(uid)`、`userctx.fleet_uid/as_fleet`、`flask.g.uid`。
- Produces: Blueprint `bp = Blueprint("picks", __name__, url_prefix="/api/picks")`，路由：
  - `GET /api/picks/public?horizon=` 返回 `{"calls": [...], "generated_at": str|None}`
  - `GET /api/picks/watchlist` 返回 `{"calls": [...]}`（当前用户 open 行）
  - `GET /api/picks/chain/<code>?scope=watchlist|public` 返回 `{"chain": [...]}`
  - `POST /api/picks/run` 后台线程跑 `run_watchlist`，返回 `{"status": "started"}`；已在跑返回 `{"status": "running"}`
  - `POST /api/picks/run_public` 管理员或站长，后台跑 `run_public`
  - `GET /api/picks/status` 返回 `{"public_running": bool, "watchlist_running": bool, "last": {...}}`
- 注意：`ratelimit.AI_PREFIXES` 加 `"/api/picks/run"`（前缀同时覆盖 run_public），让手动触发过 ai 门。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_picks_routes.py  Blueprint：临时目录 + Flask 测试客户端，不 import app.py。"""
import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"
from flask import Flask, g
import userctx, picks_store as ps, picks_pipeline as pp, picks_routes

N = [0]
def ck(cond, msg):
    N[0] += 1
    assert cond, msg

tmp = tempfile.mkdtemp()
ps.DB_PATHS["public"] = os.path.join(tmp, "pub.db"); ps.DB_PATHS["watchlist"] = os.path.join(tmp, "wl.db")
ps.init("public"); ps.init("watchlist"); pp.LOCK_DIR = tmp
picks_routes.auth.get_user = lambda uid: {"uid": uid, "is_admin": uid == "boss"}
picks_routes._market_ctx = lambda: None          # 不要在测试里 import app
import news_store
news_store.is_trading_day = lambda d=None: True  # 零网络
userctx.set_fleet_uid("owner")

app = Flask(__name__)
app.register_blueprint(picks_routes.bp)
@app.before_request
def _fake_gate():
    g.uid = app.config.get("TEST_UID", "friend")
    userctx.set_uid(g.uid)

def prop(code):
    return {"code": code, "name": "N", "horizon": "short", "stance": "buy", "entry_lo": 9.5, "entry_hi": 9.8,
            "exit_lo": 10.8, "exit_hi": 11.2, "stop": 9.2, "thesis": "t", "trigger_note": "n", "basis_json": "{}",
            "decision": "new", "trigger": "none", "px_at_call": 9.9}

def test_public_and_chain():
    ps.apply("public", prop("600519"), "2026-09-16", "r1")
    c = app.test_client()
    j = c.get("/api/picks/public?horizon=short").get_json()
    ck(len(j["calls"]) == 1 and j["calls"][0]["code"] == "600519", "公共列表")
    j = c.get("/api/picks/chain/600519?scope=public").get_json()
    ck(len(j["chain"]) == 1, "观点链")

def test_watchlist_is_per_user():
    c = app.test_client()
    with userctx.as_user("friend"):
        ps.apply("watchlist", prop("000001"), "2026-09-16", "r")
    j = c.get("/api/picks/watchlist").get_json()
    ck([x["code"] for x in j["calls"]] == ["000001"], "当前用户的自选股观点")

def test_run_permissions_and_status():
    c = app.test_client()
    pp.run_watchlist = lambda *a, **k: {"calls": 0, "error": None}
    pp.run_public = lambda *a, **k: {"calls": 0, "error": None}
    r = c.post("/api/picks/run_public")
    ck(r.status_code == 403, "普通用户不能跑公共")
    app.config["TEST_UID"] = "boss"
    r = c.post("/api/picks/run_public")
    ck(r.status_code == 200 and r.get_json()["status"] in ("started", "running"), "管理员可跑公共")
    r = c.post("/api/picks/run")
    ck(r.status_code == 200, "个人可跑")
    j = c.get("/api/picks/status").get_json()
    ck("public_running" in j and "watchlist_running" in j, "状态字段")

if __name__ == "__main__":
    for fn in (test_public_and_chain, test_watchlist_is_per_user, test_run_permissions_and_status):
        fn()
    print(f"OK — test_picks_routes 全过（{N[0]} 断言）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NO_PROXY='*' python3 tests/test_picks_routes.py`
Expected: `ModuleNotFoundError: No module named 'picks_routes'`

- [ ] **Step 3: 实现 Blueprint**

```python
"""选股与观点账本的接口。Blueprint 挂在 auth 闸门之后，自动受登录保护。"""
from __future__ import annotations

import logging
import threading
from typing import Any

from flask import Blueprint, g, jsonify, request

import auth
import picks_pipeline
import picks_store
import userctx

logger = logging.getLogger(__name__)
bp = Blueprint("picks", __name__, url_prefix="/api/picks")

_state: dict[str, Any] = {"public_running": False, "watchlist_running": {}, "last": {}}
_lock = threading.Lock()


def _can_manage() -> bool:
    uid = getattr(g, "uid", "")
    return bool((auth.get_user(uid) or {}).get("is_admin")) or uid == userctx.fleet_uid()


@bp.get("/public")
def api_public():  # noqa: ANN202
    h = request.args.get("horizon", "")
    rows = [r for r in picks_store.current("public") if not h or r["horizon"] == h]
    gen = max((r["created_at"] for r in rows), default=None)
    return jsonify({"calls": rows, "generated_at": gen})


@bp.get("/watchlist")
def api_watchlist():  # noqa: ANN202
    picks_store.init("watchlist")
    return jsonify({"calls": picks_store.current("watchlist")})


@bp.get("/chain/<code>")
def api_chain(code: str):  # noqa: ANN202
    scope = request.args.get("scope", "watchlist")
    if scope not in picks_store.SCOPES:
        return jsonify({"error": "scope 只能是 watchlist 或 public"}), 400
    return jsonify({"chain": picks_store.chain(scope, code)})


def _spawn(kind: str, fn, uid: str = "") -> dict[str, str]:
    with _lock:
        running = _state["public_running"] if kind == "public" else _state["watchlist_running"].get(uid)
        if running:
            return {"status": "running"}
        if kind == "public":
            _state["public_running"] = True
        else:
            _state["watchlist_running"][uid] = True

    def _run() -> None:
        try:
            r = fn()
            _state["last"][kind if kind == "public" else f"watchlist:{uid}"] = r
        finally:
            with _lock:
                if kind == "public":
                    _state["public_running"] = False
                else:
                    _state["watchlist_running"][uid] = False

    userctx.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@bp.post("/run")
def api_run():  # noqa: ANN202
    uid = getattr(g, "uid", "")
    return jsonify(_spawn("watchlist", lambda: picks_pipeline.run_watchlist(_market_ctx()), uid))


@bp.post("/run_public")
def api_run_public():  # noqa: ANN202
    if not _can_manage():
        return jsonify({"error": "需要管理员或站长", "msg": "只有管理员或站长能跑公共三周期"}), 403
    return jsonify(_spawn("public", lambda: picks_pipeline.run_public(_market_ctx())))


@bp.get("/status")
def api_status():  # noqa: ANN202
    uid = getattr(g, "uid", "")
    return jsonify({"public_running": bool(_state["public_running"]),
                    "watchlist_running": bool(_state["watchlist_running"].get(uid)),
                    "last": {k: v for k, v in _state["last"].items() if k == "public" or k == f"watchlist:{uid}"}})


def _market_ctx() -> dict[str, Any] | None:
    """大盘研判结论：延迟 import 避免和 app 循环依赖；取不到就不注入。"""
    try:
        import app as web  # noqa: WPS433
        return (web._market_overview_payload() or {}).get("ai")
    except Exception as e:  # noqa: BLE001
        logger.debug("picks: 大盘结论不可用: %s", e)
        return None
```

- [ ] **Step 4: 接线 app.py / scheduler.py / ratelimit.py**

在 `app.py` 里 `ratelimit.init_app(app)` 之后加：

```python
import picks_routes  # 放在文件顶部 import 区
app.register_blueprint(picks_routes.bp)   # 选股与观点账本（/api/picks/*，在 auth 闸门之后自动受保护）
```

在 `app.py` 的 `if __name__ == "__main__":` 块里、起 `_review_boot` 那行之后加：

```python
    import picks_pipeline
    # 本地开发：只给站长跑自选股；服务器上由 scheduler.py 遍历账号
    userctx.Thread(target=picks_pipeline.loop_forever,
                   args=(lambda: [userctx.fleet_uid()] if userctx.fleet_uid() else [], picks_routes._market_ctx),
                   daemon=True).start()
```

在 `scheduler.py` 的 `_shared_boot()` 末尾加：

```python
    import picks_pipeline
    import picks_routes
    # 三周期选股 + 各账号自选股买卖点：交易日 16:00 全量、09:05 短线
    userctx.spawn(picks_pipeline.loop_forever, auth.list_uids, picks_routes._market_ctx)
```

在 `ratelimit.py` 的 `AI_PREFIXES` 加一行 `"/api/picks/run",`（覆盖 run 与 run_public）。

- [ ] **Step 5: 跑测试与 import**

Run:
```bash
NO_PROXY='*' python3 tests/test_picks_routes.py
NO_PROXY='*' python3 tests/test_ratelimit.py
perl -e 'alarm 90; exec @ARGV' -- env NO_PROXY='*' python3 -c "import app, scheduler; print(len(list(app.app.url_map.iter_rules())))"
```
Expected: 三个都通过；路由数比之前多 6（75 变 81）。

- [ ] **Step 6: 提交**

```bash
git add picks_routes.py tests/test_picks_routes.py app.py scheduler.py ratelimit.py
git commit -m "feat(picks): /api/picks Blueprint 接线 + 本地与服务器定时线程 + 手动触发过 ai 门

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 前端「三周期推荐」与「自选股买卖点」

**Files:**
- Modify: `templates/index.html`（`<section class="recPanel" id="recPanel">` 之前插入新 section；`<style>` 末尾加样式）
- Modify: `static/app.js`（文件末尾追加函数；页面初始化处调用 `loadPicks()`，找 `loadWatchlist` 或 DOMContentLoaded 的初始化函数追加一行）

**Interfaces:**
- Consumes: `GET /api/picks/public`、`GET /api/picks/watchlist`、`GET /api/picks/chain/<code>?scope=`、`POST /api/picks/run`、`POST /api/picks/run_public`、`GET /api/picks/status`。
- 沿用页面里已有的 fetch 包装（401/403/429 处理在页尾角标脚本里）。

- [ ] **Step 1: index.html 加区块**

在 `<section class="recPanel" id="recPanel">` 之前插入：

```html
  <section class="picks" id="picksPanel">
    <div class="picksHead">
      <div class="t">三周期推荐 <span class="sub" id="picksGen"></span></div>
      <div class="acts">
        <button class="mini" onclick="runPicks('watchlist')">刷新自选股买卖点</button>
        <button class="mini" id="picksPubBtn" onclick="runPicks('public')">刷新三周期</button>
      </div>
    </div>
    <div class="picksCols" id="picksCols"></div>
    <div class="picksWl">
      <div class="t2">自选股买卖点</div>
      <table class="picksTbl"><thead><tr>
        <th>股票</th><th>周期</th><th>立场</th><th>买点</th><th>卖点</th><th>止损</th><th>有效到</th><th>本次</th><th>结果</th>
      </tr></thead><tbody id="picksWlRows"></tbody></table>
    </div>
    <div class="picksChain" id="picksChain" style="display:none"></div>
  </section>
```

在 `<style>` 末尾加：

```css
.picks{margin:12px 0;padding:12px;border:1px solid var(--line,#333);border-radius:10px}
.picksHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.picksHead .sub{font-size:12px;opacity:.7;margin-left:8px}
.picksCols{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.picksCol h4{margin:4px 0 6px;font-size:13px;opacity:.85}
.pickCard{border:1px solid var(--line,#333);border-radius:8px;padding:8px;margin-bottom:6px;font-size:12px}
.pickCard .nm{font-weight:600;font-size:13px}
.pickCard .lv{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0}
.pickCard .lv span{background:rgba(127,127,127,.12);padding:1px 6px;border-radius:4px}
.pickCard .tag{font-size:11px;opacity:.75}
.picksTbl{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
.picksTbl th,.picksTbl td{padding:4px 6px;border-bottom:1px solid var(--line,#333);text-align:left}
.picksTbl tr{cursor:pointer}
.picksChain{margin-top:8px;font-size:12px;white-space:pre-wrap;opacity:.9}
.stance-buy{color:var(--up,#e33)} .stance-sell{color:var(--down,#3a3)} .stance-watch,.stance-avoid{opacity:.8}
@media (max-width:900px){.picksCols{grid-template-columns:1fr}}
```

- [ ] **Step 2: app.js 加渲染**

追加到 `static/app.js` 末尾：

```javascript
// ── 三周期推荐 + 自选股买卖点（/api/picks/*） ──
const HZ_CN = {short:'短线', mid:'中线', long:'长线'};
const DEC_CN = {new:'新增', keep:'维持', revise:'修改', withdraw:'撤销'};
const TRG_CN = {none:'', stop_hit:'止损触发', target_hit:'目标达成', expired:'周期到期', thesis_broken:'新信息推翻'};
let picksSeq = 0;

function fmtRange(lo, hi){ return (lo==null&&hi==null) ? '-' : (lo===hi||hi==null ? `${lo}` : `${lo}-${hi}`); }
function fmtOutcome(r){
  if(r.max_up==null) return '';
  let s = `最高${r.max_up>0?'+':''}${r.max_up}% 最低${r.max_dn}%`;
  if(r.touched && r.touched!=='none') s += ` 碰到${({entry:'买点',exit:'卖点',stop:'止损'})[r.touched]||r.touched}`;
  return s;
}
function pickCard(r){
  const dec = DEC_CN[r.decision]||r.decision, trg = TRG_CN[r.trigger]||'';
  return `<div class="pickCard" onclick="showChain('${r.code}','public')">
    <div class="nm">${r.name} <span class="tag">${r.code}</span> <span class="stance-${r.stance}">${r.stance}</span></div>
    <div class="lv"><span>买 ${fmtRange(r.entry_lo,r.entry_hi)}</span><span>卖 ${fmtRange(r.exit_lo,r.exit_hi)}</span><span>止 ${r.stop??'-'}</span><span>到 ${r.valid_until||'-'}</span></div>
    <div>${r.thesis||''}</div>
    <div class="tag">本次${dec}${trg?'（'+trg+'）':''} ${fmtOutcome(r)}</div>
  </div>`;
}
async function loadPicks(){
  const gen = ++picksSeq;
  try{
    const [pub, wl] = await Promise.all([fetch('/api/picks/public').then(r=>r.json()), fetch('/api/picks/watchlist').then(r=>r.json())]);
    if(gen!==picksSeq) return;
    document.getElementById('picksGen').textContent = pub.generated_at ? `生成于 ${pub.generated_at}` : '尚未生成';
    document.getElementById('picksCols').innerHTML = ['short','mid','long'].map(h=>{
      const rows = (pub.calls||[]).filter(r=>r.horizon===h);
      return `<div class="picksCol"><h4>${HZ_CN[h]}</h4>${rows.length ? rows.map(pickCard).join('') : '<div class="tag">暂无</div>'}</div>`;
    }).join('');
    document.getElementById('picksWlRows').innerHTML = (wl.calls||[]).map(r=>`<tr onclick="showChain('${r.code}','watchlist')">
      <td>${r.name}<br><span class="tag">${r.code}</span></td><td>${HZ_CN[r.horizon]||r.horizon}</td>
      <td class="stance-${r.stance}">${r.stance}</td><td>${fmtRange(r.entry_lo,r.entry_hi)}</td><td>${fmtRange(r.exit_lo,r.exit_hi)}</td>
      <td>${r.stop??'-'}</td><td>${r.valid_until||'-'}</td><td>${DEC_CN[r.decision]||''}${TRG_CN[r.trigger]?'（'+TRG_CN[r.trigger]+'）':''}</td><td>${fmtOutcome(r)}</td></tr>`).join('')
      || '<tr><td colspan="9" class="tag">还没有自选股买卖点，点「刷新自选股买卖点」生成</td></tr>';
  }catch(e){ console.warn('picks load', e); }
}
async function showChain(code, scope){
  const j = await fetch(`/api/picks/chain/${code}?scope=${scope}`).then(r=>r.json());
  const el = document.getElementById('picksChain');
  el.style.display = 'block';
  el.textContent = (j.chain||[]).map(r=>`${r.created_at} ${HZ_CN[r.horizon]||r.horizon} ${r.stance} 买${fmtRange(r.entry_lo,r.entry_hi)} 卖${fmtRange(r.exit_lo,r.exit_hi)} 止${r.stop??'-'} ${DEC_CN[r.decision]||''}${TRG_CN[r.trigger]?'（'+TRG_CN[r.trigger]+'）':''} ${fmtOutcome(r)}\n  ${r.thesis||''}`).join('\n') || '无记录';
}
async function runPicks(kind){
  const url = kind==='public' ? '/api/picks/run_public' : '/api/picks/run';
  const j = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).catch(()=>({}));
  document.getElementById('picksGen').textContent = j.status==='started' ? '生成中（约 1 到 2 分钟）' : (j.status==='running' ? '已在生成' : (j.msg||j.error||''));
  if(j.status==='started'){
    const poll = setInterval(async ()=>{
      const s = await fetch('/api/picks/status').then(r=>r.json()).catch(()=>null);
      if(s && !s.public_running && !s.watchlist_running){ clearInterval(poll); loadPicks(); }
    }, 5000);
  }
}
```

在页面初始化函数里（搜 `loadWatchlist(` 的首次调用处，或 `DOMContentLoaded` 处理器末尾）加一行 `loadPicks();`。

- [ ] **Step 3: 语法检查与真机看一眼**

Run:
```bash
node --check static/app.js
grep -c "picksPanel" templates/index.html
```
Expected: node 无输出；grep 为 1。然后在本地（不是仓库目录里跑 app，而是按 CLAUDE.md「冒烟测试」的做法，或直接把这两个文件 `bash deploy/push.sh` 推到服务器后用 Safari 看）确认区块出现、按钮能触发、表格空态文案正确。

- [ ] **Step 4: 提交**

```bash
git add templates/index.html static/app.js
git commit -m "feat(picks): 前端三周期推荐卡片、自选股买卖点表、观点链与手动刷新

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 文档与部署

**Files:**
- Modify: `CLAUDE.md`（文件地图加五个模块与 4 个测试；「多用户与部署」节加一条运行时刻；测试计数 19 变 23）
- Modify: `deploy/README-deploy.md`（「额度怎么调」提一句 picks 每天 4 到 6 次记 system）

- [ ] **Step 1: CLAUDE.md**

在「多用户与部署」表后加一张表：

```markdown
### 三周期选股与观点账本（2026-09-16 加，设计 `plan/2026-09-16-picks-ledger-design.md`）
| 文件 | 职责 |
|------|------|
| `picks_levels.py` | K 线算候选价位（纯函数），模型只能从候选里挑，返回后 `snap` 校验 |
| `picks_store.py` | 观点账本 `data/picks_public.db`（公共三周期）与 `data/users/<uid>/picks.db`（自选股）；修改链、改口规则强制、到期、结果贴回、两层记忆块 |
| `llm_picks.py` | 两个提示词（三周期选股 / 自选股买卖点）+ 返回按条校验 |
| `picks_pipeline.py` | 候选池（短线=复盘题材池+换手；中线=板块龙头+资金；长线=估值+财报）、运行、结算、文件锁、16:00 / 09:05 定时循环 |
| `picks_routes.py` | `/api/picks/*` Blueprint（public / watchlist / chain / run / run_public / status） |
```

测试列表加 `test_picks_levels` / `test_picks_store` / `test_llm_picks` / `test_picks_pipeline` / `test_picks_routes`，总数改为 24；冒烟测试节的一行跑全部不变。

- [ ] **Step 2: 全套测试 + 推送服务器**

```bash
for f in tests/test_*.py; do NO_PROXY='*' python3 "$f" >/dev/null 2>&1 || echo "FAIL $f"; done
bash deploy/push.sh
```
Expected: 无 FAIL；push.sh 报两个服务运行中且 /healthz 可达。推送后在服务器看一眼：
`ssh aliyun_ecs 'sudo journalctl -u astock-scheduler -n 5 --no-pager -o cat'` 应无 Traceback。

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md deploy/README-deploy.md
git commit -m "docs: 三周期选股与观点账本 文件地图/测试/额度说明

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
