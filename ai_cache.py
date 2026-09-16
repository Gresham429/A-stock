"""L1：AI 输出短期缓存 —— 智能命中（输入指纹 + 当日）+ 时间戳，落盘避免重复慢调用。

- 存储 `ai_cache.json`；key = f"{kind}:{uid}:{input_hash}:{date}"，跨交易日自然失效。
- key 带当前用户：提示词里注入了这个人的画像/笔记/费率，同样的 inputs 不能跨人命中。
  无用户上下文（调度器）用 "-" 占位。文件仍是一份。
- 例外是 SHARED_KINDS（macro / profile）：这两类提示词只含公开资料（外围行情、公司公开叙事），
  不含任何个人数据，所以 uid 段固定为 "-"，所有人共用一份结果、少烧一次 LLM。
- 输入指纹只取「影响结论」的输入（自选/持仓/资金/板块/代码），**排除实时价格**，否则每次报价跳动都 miss。
- TTL 分类型；命中且未过期才返回。线程安全 + 原子写；文件只留当日条目，恒定很小。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

import userctx

logger = logging.getLogger(__name__)

# 缓存文件放 data/ 下：服务器上代码目录归 root 且 systemd 沙箱只放行 data/，
# 原来放仓库根目录时原子写的 .tmp 文件落不下去，缓存一直失效，每次刷新都重算大盘研判
# （2026-09-16 实测一天烧掉 41 次调用）。仓库根目录的旧文件只读一次作迁移。
_CACHE_FILE = userctx.shared_path("ai_cache.json")
_LEGACY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_cache.json")
_LOCK = threading.Lock()

# 各类型 TTL（秒）：个股/每日/选股 30 分钟，大盘 5 分钟
_TTL = {"daily": 1800, "screen": 1800, "position": 1800, "market": 300,
        "profile": 43200,  # 公司叙事变化慢，当日长缓存(12h)，跨日 key 自然失效
        "macro": 21600}    # 全球宏观 digest：当日 6h 缓存（一天算几次即可）

# 提示词只含公开资料、不注入画像/笔记/费率的类型：跨用户共用缓存（uid 段固定 "-"）。
# 其余 kind（daily/entry/market/position/screen）注入了个人数据，必须按 uid 隔离。
SHARED_KINDS = frozenset({"macro", "profile"})


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fingerprint(inputs: Any) -> str:
    """把输入规约成稳定字符串再 sha1（dict 排序、list 保序）。"""
    canon = json.dumps(inputs, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def _key(kind: str, inputs: Any) -> str:
    """缓存键。SHARED_KINDS 的 uid 段固定为 "-"（公开资料，跨人共用）；其余按当前用户隔离。"""
    uid = "-" if kind in SHARED_KINDS else (userctx.get_uid() or "-")
    return f"{kind}:{uid}:{_fingerprint(inputs)}:{_today()}"


def _load() -> dict[str, Any]:
    for path in (_CACHE_FILE, _LEGACY_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            continue
    return {}


def _save(data: dict[str, Any]) -> None:
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _CACHE_FILE)


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """只保留当日条目，保持文件恒小。"""
    today = _today()
    return {k: v for k, v in data.items() if k.rsplit(":", 1)[-1] == today}


def get(kind: str, inputs: Any) -> dict[str, Any] | None:
    """命中且未过期 → {result, ts, age_min, model}；否则 None。"""
    ttl = _TTL.get(kind, 1800)
    key = _key(kind, inputs)
    with _LOCK:
        entry = _load().get(key)
    if not entry:
        return None
    try:
        ts = datetime.fromisoformat(entry["ts"])
    except (KeyError, ValueError):
        return None
    age = (datetime.now() - ts).total_seconds()
    if age > ttl:
        return None
    return {"result": entry["result"], "ts": entry["ts"],
            "age_min": int(age // 60), "model": entry.get("model", "")}


def put(kind: str, inputs: Any, result: Any, model: str = "") -> str:
    """写入缓存，返回时间戳（ISO，精确到秒）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    key = _key(kind, inputs)
    with _LOCK:
        data = _prune(_load())
        data[key] = {"result": result, "ts": ts, "model": model, "kind": kind}
        try:
            _save(data)
        except OSError as e:
            logger.warning("ai_cache 写盘失败: %s", e)
    return ts
