"""博查（Bocha）联网搜索——B 方案：可选的通用网页搜索 + key 健康检测。

配置 config.BOCHA_API_KEY(或 BOCHAAI_API_KEY) 才启用；未配置时安全返回空。
文档：https://open.bochaai.com  端点 POST /v1/web-search（Bearer 鉴权）。

health 状态用于「到期/失效提醒」：任何一次调用都会更新 _STATUS，
前端据此在看板弹出「key 无效/过期/余额不足」的提醒。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import config

logger = logging.getLogger(__name__)

# key 健康状态（最近一次调用的结果）：ok=None 表示尚未调用过
_STATUS: dict[str, Any] = {"ok": None, "reason": "", "http": None, "checked_at": None}


def _set_status(ok: bool, reason: str = "", http: int | None = None) -> None:
    _STATUS.update({"ok": ok, "reason": reason, "http": http,
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


def _classify(http_code: int, body: str) -> str:
    """把错误归类为对用户可读的「到期提醒」文案。"""
    b = body.lower()
    if http_code == 401 or "invalid api key" in b or "expired" in b or "过期" in b:
        return ("博查 API Key 无效或已过期 —— 请到 open.bochaai.com 重新生成，"
                "并更新 .env 的 BOCHA_API_KEY（或 BOCHAAI_API_KEY），然后重启后端")
    if http_code in (402, 403) or any(k in b for k in ("余额", "欠费", "balance", "quota", "insufficient")):
        return "博查账户余额不足/欠费 —— 请到 open.bochaai.com 充值后继续使用联网搜索"
    if http_code == 429 or "rate" in b or "frequent" in b:
        return "博查请求过于频繁（限流）—— 稍后会自动恢复，无需处理"
    return f"博查联网搜索出错（HTTP {http_code}）：{body[:120]}"


def bocha_search(query: str, count: int = 8, freshness: str = "oneMonth",
                 timeout: int = 15) -> list[dict[str, Any]]:
    """博查网页搜索。返回 [{title, url, site, snippet}]；未配置/出错返回 []（并记录健康状态）。"""
    if not config.bocha_enabled():
        return []
    payload = json.dumps({"query": query, "freshness": freshness,
                          "summary": True, "count": max(1, min(count, 50))})
    req = urllib.request.Request(
        f"{config.BOCHA_BASE_URL}/v1/web-search",
        data=payload.encode("utf-8"),
        headers={"Authorization": f"Bearer {config.BOCHA_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        reason = _classify(e.code, body)
        _set_status(False, reason, e.code)
        logger.warning("博查搜索失败 HTTP %s: %s", e.code, reason)
        return []
    except (OSError, ValueError) as e:
        _set_status(False, f"博查连接失败：{e}", None)
        logger.warning("博查搜索连接失败: %s", e)
        return []
    # 业务层错误码（HTTP 200 但 code 非 200，如余额不足有时走这里）
    code = str(d.get("code", "200"))
    if code not in ("200", "0", "None"):
        reason = _classify(int(code) if code.isdigit() else 0,
                           str(d.get("message", d.get("msg", ""))))
        _set_status(False, reason, 200)
        return []
    root = d.get("data", d)
    values = ((root.get("webPages") or {}).get("value")) or []
    _set_status(True, "", 200)
    return [{
        "title": v.get("name", ""),
        "url": v.get("url", ""),
        "site": v.get("siteName", ""),
        "snippet": (v.get("summary") or v.get("snippet") or "")[:300],
    } for v in values]


def search_digest(query: str, count: int = 6) -> str:
    """把搜索结果压成喂给 LLM 的紧凑文本；无结果返回空串。"""
    rows = bocha_search(query, count=count)
    lines = [f"- {r['title']}（{r['site']}）：{r['snippet']}" for r in rows if r.get("title")]
    return "\n".join(lines)


def status() -> dict[str, Any]:
    """返回最近一次调用的健康状态（不发起新请求）。"""
    return dict(_STATUS)


def probe() -> dict[str, Any]:
    """主动做一次最小搜索以检测 key 是否有效（用于「测试联网/到期提醒」）。"""
    if not config.bocha_enabled():
        return {"configured": False}
    bocha_search("测试", count=1)
    return {"configured": True, **status()}
