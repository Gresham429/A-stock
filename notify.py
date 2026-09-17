"""提醒发送层：把一个账号的提醒推到它配的**多台手机**上。

渠道四个，都是零 SDK、纯 urllib：

| 渠道 | 地址填什么 | 说明 |
|---|---|---|
| `bark` | device key，或 `自建地址|key` | iPhone 最省事；key 在 Bark App 里 |
| `wecom` | 企业微信群机器人 webhook | 国内稳定，群里几个人就几台手机 |
| `dingtalk` | `webhook` 或 `webhook|加签密钥` | 有加签密钥时自动算 HMAC 签名 |
| `log` | 不用填 | 只写日志，用来验证触发逻辑，不推到手机 |

**永不抛异常**：提醒失败不能影响调度循环或选股。返回值统一 `(ok, 说明)`，失败原因写日志。
地址是用户自己填的（Bark key、机器人 webhook），属于本人自有数据，源码里没有任何硬编码密钥。

「一个账号多台手机」的做法就是 targets 表里的多行，`send_all` 逐条发、逐条记结果，
一台失败不影响其余几台（用户 2026-09-17：我们自己罗列就行）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BARK_BASE = "https://api.day.app"
GROUP = "A股观察台"          # 手机端按这个分组折叠，避免和别的推送混在一起
UA = "Mozilla/5.0 (AStockWatchdesk alert)"
TIMEOUT = 10


def _post(url: str, payload: dict) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "ignore")[:200]
            return (200 <= resp.status < 300), f"{resp.status} {body}"
    except Exception as e:  # noqa: BLE001 提醒失败不能影响调用方
        return False, f"{type(e).__name__}: {e}"


def send(channel: str, address: str, title: str, body: str) -> tuple[bool, str]:
    """发一条。返回 (是否成功, 说明)。未知渠道按失败处理、不猜。"""
    channel = (channel or "").strip().lower()
    if channel == "log":
        logger.info("提醒（log 渠道）%s | %s", title, body.replace("\n", " / "))
        return True, "log"
    if channel == "bark":
        if not address:
            return False, "缺 device key"
        if address.startswith("http"):
            if "|" not in address:
                return False, "自建 Bark 要写成 地址|device key"
            base, key = address.split("|", 1)
        else:
            base, key = BARK_BASE, address
        return _post(f"{base.rstrip('/')}/push",
                     {"device_key": key, "title": title, "body": body, "group": GROUP})
    if channel == "wecom":
        if not address:
            return False, "缺 webhook"
        return _post(address, {"msgtype": "text", "text": {"content": f"{title}\n{body}"}})
    if channel == "dingtalk":
        if not address:
            return False, "缺 webhook"
        url = address
        if "|" in address:
            hook, secret = address.split("|", 1)
            ts = str(round(time.time() * 1000))
            digest = hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"),
                              hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
            sep = "&" if "?" in hook else "?"
            url = f"{hook}{sep}timestamp={ts}&sign={sign}"
        return _post(url, {"msgtype": "text", "text": {"content": f"{title}\n{body}"}})
    return False, f"未知渠道 {channel!r}"


def send_all(targets: list[dict], title: str, body: str) -> dict:
    """逐台发送。返回 {sent, failed, total, detail:[{label, ok, msg}]}，一台失败不拖累其余。"""
    detail = []
    sent = failed = 0
    for t in targets:
        ok, msg = send(t.get("channel", ""), t.get("address", ""), title, body)
        label = t.get("label") or t.get("channel") or "?"
        detail.append({"label": label, "channel": t.get("channel"), "ok": ok, "msg": msg})
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        if not ok:
            logger.warning("提醒发送失败 [%s/%s]: %s", t.get("channel"), label, msg)
    return {"sent": sent, "failed": failed, "total": len(targets), "detail": detail}
