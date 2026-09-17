"""到点提醒的配置与去重状态（个人库 `data/users/<uid>/alerts.db`）。

用户的三个要求（2026-09-17 会话）：一个账号可以配**多个手机**、买点卖点默认由 AI 分析给出、
但可以自己按这份分析改点位。所以这张库只存两样东西：通知目标（多台手机）与**手动点位覆盖**。

AI 的点位不落这张库，检查时现读观点账本；有手动覆盖就以手动为准。这样 AI 每天更新点位不会
跟用户改过的打架，而用户改过的那只永远听用户的（覆盖是「这只我另有打算」，不是缓存）。

去重：同一标的同一类触发（买点 / 卖点 / 止损）每天最多发一次，记在 `sent` 表。
地址（Bark key、机器人 webhook）是本人自有数据，与画像里的费率同一口径：登录后原样回显。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

import userctx

KINDS = ("buy", "sell", "stop")
CHANNELS = ("ntfy", "bark", "wecom", "dingtalk", "log")
DB_PATH: str | None = None      # 测试用覆盖；正常走 userctx.user_path
KEEP_DAYS = 365

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', label TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS points(
  code TEXT PRIMARY KEY, name TEXT DEFAULT '',
  buy_lo REAL, buy_hi REAL, sell_lo REAL, sell_hi REAL, stop REAL,
  horizon TEXT DEFAULT '', note TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sent(
  date TEXT NOT NULL, code TEXT NOT NULL, kind TEXT NOT NULL,
  price REAL, pushed INTEGER DEFAULT 0, targets INTEGER DEFAULT 0, created_at TEXT DEFAULT '',
  PRIMARY KEY (date, code, kind)
);
"""


def _path() -> str:
    if DB_PATH:
        return DB_PATH
    return userctx.user_path("alerts.db")


def _conn() -> sqlite3.Connection:
    return userctx.open_db(_path())


def init() -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)


def _ensure() -> None:
    init()


# ── 通知目标（多台手机）────────────────────────────────────────────────────
def targets(only_enabled: bool = False) -> list[dict[str, Any]]:
    _ensure()
    sql = "SELECT * FROM targets"
    if only_enabled:
        sql += " WHERE enabled=1"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id")]


# 免填渠道：认得出这些地址属于哪个渠道，用户直接粘地址就行（用户 2026-09-17：
# 「加手机提醒那个太繁琐了」）。裸串按 ntfy 主题处理——那是唯一一种「不带任何特征」
# 的地址，而且是最省事的用法（装 App、订阅同名主题，不注册不备案）。
# ntfy 主题名的合法字符集（与官方一致）：裸串只有长这样才当主题，别的一律报错
_TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}")
_HOST_HINTS = (("qyapi.weixin.qq.com", "wecom"),
               ("oapi.dingtalk.com", "dingtalk"),
               ("ntfy.sh", "ntfy"),
               ("api.day.app", "bark"))


def infer_target(addr: str) -> tuple[str, str]:
    """从地址猜渠道。返回 (channel, address)；认不出返回 ("", "")。"""
    a = (addr or "").strip()
    if not a:
        return "", ""
    for hint, ch in _HOST_HINTS:
        if hint in a:
            return ch, a
    if a.startswith("bark:"):            # bark key 的一种常见写法
        return "bark", a.split(":", 1)[1]
    # 裸串：只有当它「长得像主题」才当 ntfy 主题，判据是含连字符/下划线/数字——
    # 也就是官方允许、但英文单词一般不带的那几类字符。
    # 为什么这么保守：`telegram`、`barkk` 这种打错的渠道名也是 ASCII 裸串，一旦宽松接受，
    # 就会被当成一个永远没人订阅的主题存下去，用户以为配好了、其实永远收不到
    # （2026-09-17 两轮测试各抓到一次）。宁可报错让他写成 `ntfy 主题名`。
    if _TOPIC_RE.fullmatch(a) and re.search(r"[-_0-9]", a):
        return "ntfy", a
    return "", ""


def replace_targets(lines: list[str]) -> dict[str, Any]:
    """整表替换通知目标。两种写法都收：

    - `渠道 地址 [备注]`，渠道 ntfy / bark / wecom / dingtalk / log，`log` 可只写渠道名；
    - **只写地址**（一行一个），渠道自动认（企业微信/钉钉 webhook、ntfy 主题或 ntfy.sh 链接、
      Bark key）。

    整体替换而不是逐条增删：用户的原话是「我们自己罗列就行」，罗列式编辑最不容易出现
    「删了这台却不知道另一台还在」的状态。解析失败的行走 errors 返回，**整批不落库**
    （半套目标比没有更危险：你以为配好了两台，其实只生效一台）。
    """
    _ensure()
    parsed: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        channel = parts[0].lower()
        if channel not in CHANNELS:
            # 不是已知渠道名 → 当成地址，猜渠道（猜不出才报错）
            ch, addr = infer_target(parts[0])
            if not ch:
                errors.append(f"第 {i} 行：认不出 {parts[0]!r} 是哪家的地址。"
                              f"可以直接粘贴 webhook 链接或 ntfy 主题名，也可以写成 "
                              f"「{'/'.join(CHANNELS)} 地址」")
                continue
            label = parts[1] if len(parts) > 1 else ""
            parsed.append((ch, addr, label))
            continue
        if channel == "log":
            parsed.append((channel, "", parts[1] if len(parts) > 1 else ""))
            continue
        if len(parts) < 2 or not parts[1]:
            errors.append(f"第 {i} 行：{channel} 需要一个地址（key / webhook / 主题名）")
            continue
        parsed.append((channel, parts[1], parts[2] if len(parts) > 2 else ""))
    if errors:
        return {"ok": False, "count": 0, "errors": errors}
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM targets")
        c.executemany(
            "INSERT INTO targets(channel,address,label,enabled,created_at) VALUES(?,?,?,1,?)",
            [(ch, addr, label, now) for ch, addr, label in parsed])
    return {"ok": True, "count": len(parsed), "errors": []}


def format_targets() -> list[str]:
    """反解成可编辑的文本行（页面上的「罗列」框直接用）。"""
    out = []
    for t in targets():
        bits = [t["channel"]]
        if t["address"]:
            bits.append(t["address"])
        if t["label"]:
            bits.append(t["label"])
        out.append(" ".join(bits))
    return out


# ── 手动点位覆盖 ──────────────────────────────────────────────────────────
def points(only_enabled: bool = False) -> list[dict[str, Any]]:
    _ensure()
    sql = "SELECT * FROM points"
    if only_enabled:
        sql += " WHERE enabled=1"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY code")]


def _num(tok: str) -> float | None:
    if tok in ("-", "", "无"):
        return None
    try:
        v = float(tok)
    except ValueError:
        raise ValueError(f"{tok!r} 不是数字（空位写 -）") from None
    if v <= 0:
        raise ValueError(f"价格必须为正：{tok!r}")
    return v


def parse_point_line(line: str) -> dict[str, Any]:
    """`代码 [买下 买上] [卖下 卖上] [止损] [备注]`，空位写 `-`。

    位置固定是为了好罗列：前 5 个数字位依次是买点下界、买点上界、卖点下界、卖点上界、止损。
    后面剩下的都算备注。只给一个数就当作「就这个价」，上下界相同。
    """
    parts = line.split()
    if not parts:
        raise ValueError("空行")
    code = parts[0]
    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"代码要 6 位数字，收到 {code!r}")
    now = datetime.now().isoformat(timespec="seconds")
    nums: list[float | None] = []
    note_bits: list[str] = []
    for tok in parts[1:]:
        if len(nums) < 5:
            if tok in ("-", "", "无"):
                nums.append(None)                  # 占位：这个点没设
                continue
            if not _looks_numeric(tok):
                if not nums:
                    # 第一个数字位之前出现非数字，几乎肯定是写错了顺序（例如「卖 12.5」）。
                    # 这时候猜用户的意思很危险：猜成买点就会在错误的价位推提醒。
                    raise ValueError(
                        f"点位要按 买下 买上 卖下 卖上 止损 的顺序写，收到 {tok!r}；"
                        "没设的点写 -，说明写在最后")
                note_bits.append(tok)              # 数字位写完了，后面都算备注
                continue
            nums.append(_num(tok))
            continue
        note_bits.append(tok)
    nums += [None] * (5 - len(nums))
    buy_lo, buy_hi, sell_lo, sell_hi, stop = nums
    if buy_lo is not None and buy_hi is None:
        buy_hi = buy_lo
    if sell_lo is not None and sell_hi is None:
        sell_hi = sell_lo
    if buy_lo is None and buy_hi is not None:
        buy_lo = buy_hi
    if sell_lo is None and sell_hi is not None:
        sell_lo = sell_hi
    if buy_lo is not None and buy_hi is not None and buy_lo > buy_hi:
        raise ValueError(f"买点下界 {buy_lo} 大于上界 {buy_hi}")
    if sell_lo is not None and sell_hi is not None and sell_lo > sell_hi:
        raise ValueError(f"卖点下界 {sell_lo} 大于上界 {sell_hi}")
    if buy_lo is None and sell_lo is None and stop is None:
        raise ValueError("至少要有一个点（买点 / 卖点 / 止损）")
    return {"code": code, "buy_lo": buy_lo, "buy_hi": buy_hi, "sell_lo": sell_lo,
            "sell_hi": sell_hi, "stop": stop, "note": " ".join(note_bits),
            "updated_at": now}


def _looks_numeric(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def replace_points(lines: list[str]) -> dict[str, Any]:
    """整表替换手动点位。任一行解析失败则整批不落库（同通知目标的理由）。"""
    _ensure()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = parse_point_line(line)
        except ValueError as e:
            errors.append(f"第 {i} 行：{e}")
            continue
        if row["code"] in seen:
            errors.append(f"第 {i} 行：{row['code']} 重复")
            continue
        seen.add(row["code"])
        rows.append(row)
    if errors:
        return {"ok": False, "count": 0, "errors": errors}
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM points")
        c.executemany(
            "INSERT INTO points(code,buy_lo,buy_hi,sell_lo,sell_hi,stop,note,enabled,updated_at) "
            "VALUES(:code,:buy_lo,:buy_hi,:sell_lo,:sell_hi,:stop,:note,1,:updated_at)", rows)
    return {"ok": True, "count": len(rows), "errors": []}


def format_points() -> list[str]:
    """反解成可编辑文本行；空位统一写 `-`。"""
    out = []
    for p in points():
        cells = []
        for k in ("buy_lo", "buy_hi", "sell_lo", "sell_hi", "stop"):
            v = p.get(k)
            cells.append("-" if v is None else f"{v:g}")
        line = f"{p['code']} {' '.join(cells)}"
        if p.get("note"):
            line += f" {p['note']}"
        out.append(line)
    return out


# ── 去重 ────────────────────────────────────────────────────────────────
def sent_today(day: str = "") -> dict[tuple[str, str], dict[str, Any]]:
    d = day or date_cls.today().isoformat()
    _ensure()
    with _conn() as c:
        return {(r["code"], r["kind"]): dict(r)
                for r in c.execute("SELECT * FROM sent WHERE date=?", (d,))}


def mark_sent(code: str, kind: str, price: float, pushed: int, targets_n: int,
              day: str = "") -> None:
    _ensure()
    d = day or date_cls.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO sent(date,code,kind,price,pushed,targets,created_at) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(date,code,kind) DO UPDATE SET "
                  "price=excluded.price, pushed=excluded.pushed, targets=excluded.targets, "
                  "created_at=excluded.created_at",
                  (d, code, kind, price, pushed, targets_n, now))


def purge(days: int = KEEP_DAYS) -> int:
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    _ensure()
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM sent WHERE date < ?", (cutoff,)).rowcount


def status() -> dict[str, Any]:
    _ensure()
    with _conn() as c:
        return {"targets": c.execute("SELECT COUNT(*) n FROM targets WHERE enabled=1").fetchone()["n"],
                "points": c.execute("SELECT COUNT(*) n FROM points WHERE enabled=1").fetchone()["n"],
                "sent_today": c.execute("SELECT COUNT(*) n FROM sent WHERE date=?",
                                        (date_cls.today().isoformat(),)).fetchone()["n"]}
