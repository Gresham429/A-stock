"""交易规则库（SQLite，可增删改 + 启用停用）。

蒸馏自 Al Brooks 价格行为体系（PA_Agent），改写为 A股波段/持仓分析可用的框架规则。
启用中的规则由 `app._ai_web_context` 注入各 AI 分析提示词，让看板 AI 按此框架给意见。
data/rules.db（gitignore）。每条带 created_at/updated_at。
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
DB_PATH = os.path.join(_DIR, "rules.db")
_LOCK = threading.Lock()

CATEGORIES = ["总则纪律", "市场状态识别", "趋势与通道", "区间震荡",
              "K线信号", "止损止盈", "入场时机"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, updated_at TEXT,
  category TEXT, title TEXT, content TEXT,
  enabled INTEGER DEFAULT 1, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_cat ON rules(category);
"""

# 蒸馏种子：(分类, 标题, 要点)。改写自 PA_Agent，适配 A股波段分析。
_SEED: list[tuple[str, str, str]] = [
    ("总则纪律", "交易者方程", "只在『胜率×回报 > 败率×风险』时才动手；不满足就观望——不做也是一个合法决策。"),
    ("总则纪律", "概率管理者，不预测方向", "只评估概率分布，不断言涨跌；先判断市场处于什么状态，再选策略，最后才谈买卖。"),
    ("总则纪律", "禁止逆势", "不逆当前趋势/主方向操作；反转（MTR）只是诊断标签，不构成买入或卖出依据。"),
    ("总则纪律", "禁止追高潮", "出现衰竭信号（长尾线/小实体/反向棒）或明显买卖高潮后，禁止追原方向，只等回撤后顺势或观望。"),
    ("总则纪律", "惯性优先", "默认趋势延续；确认反转需『趋势线被突破 + 极点测试失败』双重证据，否则按延续处理。"),
    ("总则纪律", "看不懂就等", "信号不清晰、上下文矛盾时不动手，等下一根 K 线/下一个交易日给出更清楚的信号。"),

    ("市场状态识别", "先定状态再选策略", "市场位置是第一优先级：尖峰/极速/窄通道/常规通道/宽通道/趋势型区间/震荡区间/极端震荡——先归类，再路由策略。"),
    ("市场状态识别", "尖峰(Spike)", "连续同向强趋势棒、回撤极小；只顺势不追突破；连续 6 根以上或出现衰竭信号即警惕高潮，停止追单。"),
    ("市场状态识别", "通道是倾斜的区间", "窄通道更接近强趋势、宽通道更接近区间；有更高高点/更高低点不自动否定其区间属性，宽通道要按区间防假突破。"),
    ("市场状态识别", "嵌套思维", "用长程结构窗口定方向偏好，用即时信号窗口定入场时机；大中小周期一起看。"),
    ("市场状态识别", "Always In 方向", "问自己『此刻若只能持一个方向，该持多还是空』，顺这个 Always In 方向操作。"),

    ("趋势与通道", "上涨通道只做多", "上涨通道里只顺势做多（回撤、旗形、突破测试失败后进场）；禁止在通道顶部做空。下跌通道镜像做空。"),
    ("趋势与通道", "均线回撤 High1/High2", "价格回撤到 EMA20 附近、出现多头信号棒，是上涨通道里最可靠的顺势买点（H1 首次回撤、H2 二次回撤）。下跌通道对应 L1/L2。"),
    ("趋势与通道", "微型通道别硬追", "微型（极陡）通道回撤极浅，等反向假突破失败或浅回撤再顺势，别直接市价追。"),
    ("趋势与通道", "趋势末端警惕", "长趋势后出现横向重叠整理（10~20+ 根）＝最终旗形嫌疑，趋势可能衰竭，降低顺势追单意愿。"),

    ("区间震荡", "震荡区间→观望为主", "无明显方向的震荡区间：中部三分之一不交易；只在边界、且顺已判定方向时才考虑。"),
    ("区间震荡", "趋势型区间只做一侧", "trending_tr（偏多/偏空的区间）只顺方向一侧操作（偏多→下边界买）；禁止双边高抛低吸、禁止逆势。"),
    ("区间震荡", "区间里多数突破失败", "震荡区间中大多数突破会失败，不追突破（尤其不追高潮棒突破），等突破测试确认再说。"),

    ("K线信号", "好信号棒", "好信号棒：收盘接近极点、实体较大尾部短、长度不超均长 1.5 倍。十字星/长尾/超长实体都是差信号。"),
    ("K线信号", "信号需跟随确认", "信号棒之后必须有 K 线突破其极点才形成有效入场；没有跟随的信号不算高概率机会。"),
    ("K线信号", "入场质量决定交易质量", "强入场棒（大实体、收盘近极点）＝强确认；弱入场棒（小实体/十字星）风险大、宁可放弃。"),
    ("K线信号", "先上下文再形态", "同一 K 线形态在趋势/区间/突破后含义完全不同；先判上下文，再判形态，再判信号质量。"),

    ("止损止盈", "结构止损", "止损放在信号棒极点外 1 跳（宽通道/噪声大→放最近波段极点外 1 跳）；用结构位，不用固定跳数拍脑袋。"),
    ("止损止盈", "止损过大过滤器", "若结构止损超过 8 跳或信号棒高度 60%，判定止损过大→观望或放弃，别硬做。"),
    ("止损止盈", "定价顺序与 RR", "先定入场→保守目标 TP1（RR≥1）→更远结构目标 TP2→结构止损；RR<1 就收紧止损或调整入场，禁止为凑 RR 向外扩止损。"),
    ("止损止盈", "铁丝网/无交易环境", "紧密重叠、边界频繁假突破（铁丝网）时，禁止贴边界 1~2 跳止损，须更大结构缓冲，或直接观望。"),

    ("入场时机", "二次入场优先", "第一次突破/信号失败后的第二次尝试（H2/L2）通常比第一次更可靠，优先等二次入场。"),
    ("入场时机", "突破要测试", "突破后等『突破测试』（回踩不破前高/前低）再顺势，别追第一根突破棒。"),
    ("入场时机", "结构目标 Measured Move", "用等距投射估目标位：旗形/尖峰高度向外等距投射，作为止盈参考，而非拍价。"),
    ("入场时机", "MTR 只诊断不逆势", "主要趋势反转需『趋势线突破 + 更高高点/更低低点测试失败』；即便成立也只作诊断，本框架不据此逆势下单。"),
]


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)
    seed_if_empty()


def seed_if_empty() -> int:
    """空库时灌入蒸馏种子。返回插入条数。"""
    with _LOCK, _conn() as c:
        if c.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        c.executemany(
            "INSERT INTO rules(created_at,updated_at,category,title,content,enabled,source)"
            " VALUES(?,?,?,?,?,1,'PA_Agent')",
            [(now, now, cat, title, content) for cat, title, content in _SEED])
        n = c.total_changes
    logger.info("规则库种子灌入 %d 条", n)
    return n


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    d["enabled"] = bool(d.get("enabled"))
    return d


def list_rules(category: str = "", enabled_only: bool = False) -> list[dict[str, Any]]:
    where, args = [], []
    if category:
        where.append("category = ?"); args.append(category)
    if enabled_only:
        where.append("enabled = 1")
    sql = "SELECT * FROM rules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, id"
    with _conn() as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def add(category: str, title: str, content: str, source: str = "user") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO rules(created_at,updated_at,category,title,content,enabled,source)"
            " VALUES(?,?,?,?,?,1,?)", (now, now, category, title, content, source))
        return cur.lastrowid


def update(rule_id: int, **fields: Any) -> None:
    allowed = ("category", "title", "content", "enabled")
    sets = [f"{k}=?" for k in fields if k in allowed]
    if not sets:
        return
    args = [int(fields[k]) if k == "enabled" else fields[k] for k in fields if k in allowed]
    args.append(datetime.now().isoformat(timespec="seconds"))
    args.append(rule_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE rules SET {','.join(sets)}, updated_at=? WHERE id=?", args)


def delete(rule_id: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def for_ai(limit: int = 40) -> str:
    """启用中的规则拼成提示词块（按分类分组），供 AI 分析时遵循。空则返回空串。"""
    rows = list_rules(enabled_only=True)
    if not rows:
        return ""
    by_cat: dict[str, list[str]] = {}
    for r in rows[:limit]:
        by_cat.setdefault(r["category"], []).append(f"{r['title']}：{r['content']}")
    parts = []
    for cat in CATEGORIES:
        if by_cat.get(cat):
            parts.append(f"【{cat}】\n" + "\n".join(f"- {x}" for x in by_cat[cat]))
    for cat, items in by_cat.items():  # 自定义分类兜底
        if cat not in CATEGORIES:
            parts.append(f"【{cat}】\n" + "\n".join(f"- {x}" for x in items))
    return "\n".join(parts)


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
