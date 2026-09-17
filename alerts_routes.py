"""提醒配置与手动检查的接口。Blueprint 挂在 auth 闸门之后，只能操作自己的提醒。

四个动作：读配置（目标 + 手动点位 + 今日已发）、存配置（罗列式整表替换）、
发测试、立即检查一次（force 跳过交易时段）。
"""
from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

import alerts
import alerts_store
import picks_pipeline

logger = logging.getLogger(__name__)
bp = Blueprint("alerts", __name__, url_prefix="/api/alerts")


def _lines(payload: dict, key: str) -> list[str]:
    """接受字符串（textarea，按行拆）或 JSON 数组两种入参，方便页面与脚本两用。"""
    v = payload.get(key)
    if isinstance(v, list):
        return [str(x) for x in v]
    return str(v if v is not None else "").splitlines()


@bp.get("")
def api_get():  # noqa: ANN202
    alerts_store.init()
    return jsonify({
        "targets": alerts_store.format_targets(),
        "points": alerts_store.format_points(),
        "effective": alerts.effective_levels(),
        "sent_today": [{"code": k[0], "kind": k[1], "price": v["price"],
                        "at": v["created_at"]} for k, v in alerts_store.sent_today().items()],
        "status": alerts_store.status(),
        "horizons": picks_pipeline.visible_horizons(),
        "approach_pct": alerts.APPROACH_PCT,
        "tick_sec": alerts.TICK_SEC,
        "note": "点位默认取 AI 分析（观点账本），在下面的框里写一只就覆盖那一只",
    })


@bp.post("/targets")
def api_targets():  # noqa: ANN202
    r = alerts_store.replace_targets(_lines(request.get_json(silent=True) or {}, "targets"))
    return jsonify(r), (200 if r["ok"] else 400)


@bp.post("/points")
def api_points():  # noqa: ANN202
    r = alerts_store.replace_points(_lines(request.get_json(silent=True) or {}, "points"))
    return jsonify(r), (200 if r["ok"] else 400)


@bp.post("/test")
def api_test():  # noqa: ANN202
    return jsonify(alerts.notify_test())


@bp.post("/check")
def api_check():  # noqa: ANN202
    uid = getattr(g, "uid", "")
    dry = bool((request.get_json(silent=True) or {}).get("dry_run"))
    return jsonify(alerts.check(uid, force=True, dry_run=dry))
