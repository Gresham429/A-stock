"""条件单补判的跨进程互斥（零依赖，离线，不打网络/不调 LLM）。

**为什么有这个文件**：条件单补判会被两个进程同时触发——两个 worker 的管理员各点一次
`run_all`，或服务器上 scheduler 撞手动触发。修前 `sweep_conditions` 是「读 live 再写回」：
两边都读到同一张条件单、都去补判，输家最后执行 `close_condition(cid, "cancelled")`，
把赢家写好的 `triggered` **覆盖成 cancelled** —— 止损明明成交了（成交价、日期都在），
账本和前端却显示「已撤销」。

修法是原子占位（同 `claims` 表那套）：`claim_condition` 抢到才动手，抢不到跳过；
`close_condition` 加状态守卫，只允许从 live/settling 迁移；中途失败放回 live，
超过 `STALE_CLAIM_MIN` 没动静的 settling 视为进程被杀，回滚重试。

跑法：python3 tests/test_condition_claim.py
"""
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_loop  # noqa: E402
import agent_store  # noqa: E402

BAR_AFTER_TODAY = "2099-01-01"    # 必然晚于 created_date，判定「触发过」
TRIGGER = 10.0


def fresh_db() -> None:
    agent_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "agents.db")
    agent_store.init()


def mk_agent() -> int:
    return agent_store.create_agent("t-cond", account_id=-777, profile_id=-1)


def mk_stop(agent_id: int) -> int:
    return agent_store.add_condition(agent_id, "600000", "测试股", "stop_loss", TRIGGER, 100)


def status_of(cid: int) -> str:
    with agent_store._conn() as c:
        row = c.execute("SELECT status FROM conditions WHERE id=?", (cid,)).fetchone()
    return row["status"]


def _resolve(dotted: str):
    """把 "agent_loop.ds.sina_kline" 解析成 (模块对象, 属性名)。"""
    parts = dotted.split(".")
    obj = globals()[parts[0]]
    for p in parts[1:-1]:
        obj = getattr(obj, p)
    return obj, parts[-1]


@contextmanager
def patched(mapping: dict):
    """临时替换若干模块属性（点号路径作键），退出时倒序还原。"""
    saved = []
    try:
        for dotted, value in mapping.items():
            obj, attr = _resolve(dotted)
            saved.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)
        yield
    finally:
        for obj, attr, old in reversed(saved):
            setattr(obj, attr, old)


def triggering_kline(code: str, num: int = 60, scale: int = 240):
    return [{"date": BAR_AFTER_TODAY, "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 9.5, "volume": 1000}]


def test_claim_is_exclusive():
    """一张 live 条件单只能被认领一次；释放后可重试。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)
    assert agent_store.claim_condition(cid) is True, "首次认领应成功"
    assert agent_store.claim_condition(cid) is False, "第二次认领应失败 —— 竞态未修"
    assert status_of(cid) == "settling"
    agent_store.release_condition(cid)
    assert status_of(cid) == "live", "释放后应回到 live"
    assert agent_store.claim_condition(cid) is True, "释放后应可再认领"


def test_close_cannot_overwrite_finished():
    """已收尾的条件单不能被后到的进程改写状态（止损成交不能被写成已撤销）。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)
    assert agent_store.claim_condition(cid) is True
    assert agent_store.close_condition(cid, "triggered", BAR_AFTER_TODAY, TRIGGER) is True
    assert status_of(cid) == "triggered"
    assert agent_store.close_condition(cid, "cancelled") is False, "守卫生效时应返回 False"
    assert status_of(cid) == "triggered", "输家把 triggered 覆盖成 cancelled —— 守卫没生效"


def test_stale_settling_is_reclaimed():
    """进程在补判中途被杀会留下 settling：超过阈值必须回滚成 live。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)
    assert agent_store.claim_condition(cid) is True
    # 把认领时刻改成很久以前，模拟进程被杀留下的陈旧占位
    with agent_store._conn() as c:
        c.execute("UPDATE conditions SET claimed_at='2000-01-01T00:00:00' WHERE id=?", (cid,))
    assert agent_store.reclaim_stale_conditions() == 1, "陈旧 settling 应被回滚"
    assert status_of(cid) == "live"
    # 刚认领的（claimed_at 是现在）不该被回滚
    assert agent_store.claim_condition(cid) is True
    assert agent_store.reclaim_stale_conditions() == 0, "新占位不该被回滚"


def test_loser_process_does_not_touch_condition():
    """核心回归：两个进程都读到同一张条件单时，输家不得改动它。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)
    assert agent_store.claim_condition(cid) is True, "模拟另一个进程先抢到"
    rec = [dict(r) for r in agent_store.conditions_of(aid) if r["id"] == cid]
    # 模拟本进程也在认领之前读到了它（真实竞态就是这样）
    with patched({"agent_store.live_conditions": lambda aid=None: rec,
                  "agent_loop.ds.sina_kline": triggering_kline,
                  "agent_loop.profile_store.fee_schedule": lambda pid: None}):
        fired = agent_loop.sweep_conditions(aid)
    assert fired == [], "输家不该产出成交"
    assert status_of(cid) == "settling", "输家改动了赢家占位的条件单"


def test_failure_releases_claim():
    """取数/撮合抛异常时放回 live，不能把条件单卡死在 settling。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)

    def boom(_aid):
        raise RuntimeError("模拟取持仓失败")

    with patched({"agent_loop.ds.sina_kline": triggering_kline,
                  "agent_loop.profile_store.fee_schedule": lambda pid: None,
                  "agent_loop.paper_store.positions_of": boom}):
        fired = agent_loop.sweep_conditions(aid)
    assert fired == []
    assert status_of(cid) == "live", "失败后应放回 live 供下次重试"


def test_no_position_still_cancels():
    """空仓（或 T+1 不可卖）时仍应收尾为 cancelled，且是走守卫的正常迁移。"""
    fresh_db()
    aid = mk_agent()
    cid = mk_stop(aid)
    with patched({"agent_loop.ds.sina_kline": triggering_kline,
                  "agent_loop.profile_store.fee_schedule": lambda pid: None,
                  "agent_loop.paper_store.positions_of": lambda _aid: []}):
        fired = agent_loop.sweep_conditions(aid)
    assert fired == []
    assert status_of(cid) == "cancelled"


def test_old_db_migrates_claimed_at():
    """旧库（conditions 无 claimed_at 列）必须能自动补列，否则线上升级即报错。"""
    path = os.path.join(tempfile.mkdtemp(), "agents.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE conditions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT DEFAULT '',
        kind TEXT NOT NULL, trigger_price REAL NOT NULL, shares INTEGER NOT NULL,
        created_date TEXT NOT NULL, status TEXT DEFAULT 'live',
        triggered_date TEXT DEFAULT '', fill_price REAL DEFAULT 0, note TEXT DEFAULT '')""")
    con.commit()
    con.close()
    agent_store.DB_PATH = path
    agent_store.init()
    with agent_store._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(conditions)")}
    assert "claimed_at" in cols, "旧库未补 claimed_at 列"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
