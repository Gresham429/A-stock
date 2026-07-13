"""L5：私域信息笔记（SQLite，永久保留、不进 1 年清理）。

- `data/notes.db`（gitignore，绝不提交/外发）。
- 每条必带时间戳 `created_at`（编辑加 `updated_at`）。
- `codes/sectors/tags` 以逗号分隔字符串存；供 `_ai_web_context` 按 code/sector 检索注入。
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
DB_PATH = os.path.join(_DIR, "notes.db")
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, updated_at TEXT,
  codes TEXT, sectors TEXT, tags TEXT, kind TEXT,
  content TEXT, ai_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def add(content: str, codes: str = "", sectors: str = "", tags: str = "",
        kind: str = "", ai_summary: str = "") -> int:
    """新增一条笔记（必带 created_at）。返回 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO notes(created_at,updated_at,codes,sectors,tags,kind,content,ai_summary)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (now, now, codes, sectors, tags, kind, content, ai_summary))
        return cur.lastrowid


def update(note_id: int, **fields: Any) -> None:
    allowed = ("codes", "sectors", "tags", "kind", "content", "ai_summary")
    sets = [f"{k}=?" for k in fields if k in allowed]
    if not sets:
        return
    args = [fields[k] for k in fields if k in allowed]
    args.append(datetime.now().isoformat(timespec="seconds"))
    args.append(note_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE notes SET {','.join(sets)}, updated_at=? WHERE id=?", args)


def delete(note_id: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM notes WHERE id=?", (note_id,))


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def list_notes(code: str = "", sector: str = "", q: str = "", limit: int = 100) -> list[dict[str, Any]]:
    where, args = [], []
    if code:
        where.append("codes LIKE ?"); args.append(f"%{code}%")
    if sector:
        where.append("sectors LIKE ?"); args.append(f"%{sector}%")
    if q:
        where.append("(content LIKE ? OR tags LIKE ? OR ai_summary LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM notes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def for_ai(code: str = "", sectors: list[str] | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """供 AI 上下文检索：优先该股，其次相关板块的笔记（带时间戳）。"""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    if code:
        for r in list_notes(code=code, limit=limit):
            if r["id"] not in seen:
                seen.add(r["id"]); out.append(r)
    for s in (sectors or []):
        for r in list_notes(sector=s, limit=limit):
            if r["id"] not in seen:
                seen.add(r["id"]); out.append(r)
    return out[:limit]


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
