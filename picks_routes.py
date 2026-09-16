"""选股与观点账本的接口。Blueprint 挂在 auth 闸门之后，自动受登录保护。"""
from __future__ import annotations

import logging
import threading
from typing import Any

from flask import Blueprint, g, jsonify, request

import auth
import picks_pipeline
import picks_store
import userctx

logger = logging.getLogger(__name__)
bp = Blueprint("picks", __name__, url_prefix="/api/picks")

_state: dict[str, Any] = {"public_running": False, "watchlist_running": {}, "last": {}}
_lock = threading.Lock()


def _can_manage() -> bool:
    uid = getattr(g, "uid", "")
    return bool((auth.get_user(uid) or {}).get("is_admin")) or uid == userctx.fleet_uid()


@bp.get("/public")
def api_public():  # noqa: ANN202
    """三周期各自的最新一轮（`latest_run`），不是「全表当前 open」——09:05 只刷短线时，
    中线/长线的最新一轮仍是昨天 16:00 那批，用 `current()` 会把它们和当天刚出的短线
    混在一起、误当成同一轮生成时间。"""
    picks_store.init("public")
    h = request.args.get("horizon", "")
    rows = [r for r in picks_store.latest_run("public") if not h or r["horizon"] == h]
    gen = max((r["created_at"] for r in rows), default=None)
    return jsonify({"calls": rows, "generated_at": gen})


@bp.get("/watchlist")
def api_watchlist():  # noqa: ANN202
    picks_store.init("watchlist")
    return jsonify({"calls": picks_store.current("watchlist")})


@bp.get("/chain/<code>")
def api_chain(code: str):  # noqa: ANN202
    scope = request.args.get("scope", "watchlist")
    if scope not in picks_store.SCOPES:
        return jsonify({"error": "scope 只能是 watchlist 或 public"}), 400
    picks_store.init(scope)
    return jsonify({"chain": picks_store.chain(scope, code)})


def _spawn(kind: str, fn, uid: str = "") -> dict[str, str]:
    with _lock:
        running = _state["public_running"] if kind == "public" else _state["watchlist_running"].get(uid)
        if running:
            return {"status": "running"}
        if kind == "public":
            _state["public_running"] = True
        else:
            _state["watchlist_running"][uid] = True

    def _run() -> None:
        try:
            r = fn()
            with _lock:
                _state["last"][kind if kind == "public" else f"watchlist:{uid}"] = r
        finally:
            with _lock:
                if kind == "public":
                    _state["public_running"] = False
                else:
                    _state["watchlist_running"][uid] = False

    try:
        userctx.Thread(target=_run, daemon=True).start()
    except Exception:  # noqa: BLE001 起线程失败要把标记复位后原样抛出，不能吞
        with _lock:
            if kind == "public":
                _state["public_running"] = False
            else:
                _state["watchlist_running"][uid] = False
        raise
    return {"status": "started"}


@bp.post("/run")
def api_run():  # noqa: ANN202
    uid = getattr(g, "uid", "")
    return jsonify(_spawn("watchlist", lambda: picks_pipeline.run_watchlist(_market_ctx()), uid))


@bp.post("/run_public")
def api_run_public():  # noqa: ANN202
    if not _can_manage():
        return jsonify({"error": "需要管理员或站长", "msg": "只有管理员或站长能跑公共三周期"}), 403
    # 公共三周期是全站结论，费用记 fleet（只受全站预算），不吃管理员个人的日额度；
    # 与调度器里 tick() 的做法一致。
    def _run_as_fleet() -> dict:
        with userctx.as_fleet():
            return picks_pipeline.run_public(_market_ctx())

    return jsonify(_spawn("public", _run_as_fleet))


@bp.get("/status")
def api_status():  # noqa: ANN202
    """在跑状态取「进程内标记 或 锁文件」——同一台机器上 web 与 scheduler 是两个进程，
    scheduler 起的批跑只落了锁文件，web 进程里的 `_state` 字典看不到，只查 `_state`
    会漏报「实际在跑但本进程不知道」。"""
    uid = getattr(g, "uid", "")
    with _lock:
        last = {k: v for k, v in _state["last"].items() if k == "public" or k == f"watchlist:{uid}"}
        pub_flag = bool(_state["public_running"])
        wl_flag = bool(_state["watchlist_running"].get(uid))
    pub_running = pub_flag or picks_pipeline.is_running("public")
    wl_running = wl_flag or picks_pipeline.is_running("watchlist")
    return jsonify({"public_running": pub_running, "watchlist_running": wl_running, "last": last})


def _market_ctx() -> dict[str, Any] | None:
    """大盘研判结论：延迟 import 避免和 app 循环依赖；取不到就不注入。"""
    try:
        import app as web  # noqa: WPS433
        return (web._market_overview_payload() or {}).get("ai")
    except Exception as e:  # noqa: BLE001
        logger.debug("picks: 大盘结论不可用: %s", e)
        return None
