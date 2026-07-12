"""博查（Bocha）联网搜索——B 方案：可选的通用网页搜索。

配置了 config.BOCHA_API_KEY 才启用；未配置时所有函数安全返回空，不影响主流程。
文档：https://open.bochaai.com  端点 POST /v1/web-search（Bearer 鉴权）。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import config

logger = logging.getLogger(__name__)


def bocha_search(query: str, count: int = 8, freshness: str = "oneMonth",
                 timeout: int = 15) -> list[dict[str, Any]]:
    """博查网页搜索。返回 [{title, url, site, snippet}]；未配置 key 或出错时返回 []。

    freshness: noLimit / oneDay / oneWeek / oneMonth / oneYear。
    """
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
        logger.warning("博查搜索 HTTP %s: %s", e.code, e.read()[:200])
        return []
    except (OSError, ValueError) as e:
        logger.warning("博查搜索失败: %s", e)
        return []
    # 响应结构：data.webPages.value[] 或 webPages.value[]（两种口径都兼容）
    root = d.get("data", d)
    values = ((root.get("webPages") or {}).get("value")) or []
    out = []
    for v in values:
        out.append({
            "title": v.get("name", ""),
            "url": v.get("url", ""),
            "site": v.get("siteName", ""),
            "snippet": (v.get("summary") or v.get("snippet") or "")[:300],
        })
    return out


def search_digest(query: str, count: int = 6) -> str:
    """把搜索结果压成喂给 LLM 的紧凑文本；无结果返回空串。"""
    rows = bocha_search(query, count=count)
    if not rows:
        return ""
    lines = [f"- {r['title']}（{r['site']}）：{r['snippet']}" for r in rows if r.get("title")]
    return "\n".join(lines)
