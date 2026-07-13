"""L1：AI 输出短期缓存 —— 智能命中（输入指纹 + 当日）+ 时间戳，落盘避免重复慢调用。

- 存储 `ai_cache.json`；key = f"{kind}:{input_hash}:{date}"，跨交易日自然失效。
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

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_cache.json")
_LOCK = threading.Lock()

# 各类型 TTL（秒）：个股/每日/选股 30 分钟，大盘 5 分钟
_TTL = {"daily": 1800, "screen": 1800, "position": 1800, "market": 300}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fingerprint(inputs: Any) -> str:
    """把输入规约成稳定字符串再 sha1（dict 排序、list 保序）。"""
    canon = json.dumps(inputs, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def _key(kind: str, inputs: Any) -> str:
    return f"{kind}:{_fingerprint(inputs)}:{_today()}"


def _load() -> dict[str, Any]:
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
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
