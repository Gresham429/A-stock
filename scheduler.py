"""独立调度进程：web 进程外的定时任务都在这里跑。

    python3 scheduler.py

# 为什么必须拆出来

原来 app.py 在 __main__ 里拉起几个后台线程（新闻回填、全市场池预热、
复盘、agent 日循环）。本地单进程跑没问题，但上 gunicorn 之后每个 worker
都会各起一份：同一天的复盘生成两遍、universe 回填互相抢锁、同一个 agent
被并发决策两次。这不是性能问题，是正确性问题——会产生重复的模拟委托和
自相矛盾的教训记录。

拆开之后：web 进程无状态、可以随便重启和加 worker；调度进程单实例、
崩了 systemd 拉起来、日志独立。

# 这里跑什么

公共任务（全站只跑一次，跟谁登录没关系）：新闻库回填（自选股取全站并集）、
全市场池刷新、板块归属回填、因子 IC 回测、每日复盘。

每日 housekeeping：过期会话、旧用量记录，以及舰队库（站长目录里的
agents.db）按日累积表的清理——只在站长上下文里做一次，不遍历账号。

# agent 不自动跑（默认）

舰队全站只有一套，归站长。服务器上保留「不开 app 就不炒股」：agent 只在
站长手动触发（/api/agents/run_all）时跑。设 ASTOCK_AGENT_AUTO=1 才让本进程
每隔 ASTOCK_AGENT_TICK_SEC 秒在站长上下文里探一轮 run_all(require_open=True)；
预算由 llm 调用点的 ratelimit.allow_llm 管，这里不预扣。默认关时主循环只是
空转等待，systemd 以进程存活判定。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

# 先把日志配好，再 import app（app 里也会 basicConfig，先到的生效）
logging.basicConfig(
    level=os.environ.get("ASTOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scheduler")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_loop          # noqa: E402
import agent_store         # noqa: E402
import app as web          # noqa: E402  复用 app 里已有的预热/复盘逻辑，不重复实现
import auth                # noqa: E402
import news_store          # noqa: E402
import ratelimit           # noqa: E402
import userctx             # noqa: E402

AGENT_AUTO = os.environ.get("ASTOCK_AGENT_AUTO", "0") == "1"
AGENT_TICK_SEC = int(os.environ.get("ASTOCK_AGENT_TICK_SEC", "300"))
_tick_lock = threading.Lock()


def _shared_boot() -> None:
    """公共数据预热：新闻库 + 全市场池 + 因子 + 复盘。全站只跑一次。"""
    try:
        if news_store.stats()["total"] == 0:
            logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
            userctx.spawn(news_store.backfill)   # 无用户上下文，自选股走全站并集
    except Exception as e:  # noqa: BLE001
        logger.warning("新闻库检查失败（不影响其余）：%s", e)

    # _universe_boot 内部已做好异常兜底，且不会起 agent 调度器（见 app.py 里的说明）
    userctx.spawn(web._universe_boot)
    userctx.spawn(web._review_boot)


def _run_fleet_agents() -> None:
    """在站长上下文里跑一轮 agent 日循环。全站只有这一套舰队。"""
    fleet = userctx.fleet_uid()
    if not fleet:
        logger.warning("agent 自动跑已开但尚未设置舰队站长（ASTOCK_FLEET_OWNER 或首个管理员），跳过")
        return
    with userctx.as_fleet():
        try:
            web.ensure_user_stores(fleet)
            if not agent_store.list_agents(active_only=True):
                return
            for r in agent_loop.run_all(require_open=True):
                if r.get("skipped"):
                    logger.info("agent %s: %s", r.get("agent", "-"), r["skipped"])
                elif r.get("ok"):
                    logger.info("agent %s [%s]: 挂出 %d / 成交 %d / 教训 %d",
                                r.get("agent"), r.get("slot", "-"),
                                len(r.get("placed") or []), len(r.get("filled") or []),
                                len(r.get("lessons") or []))
        except Exception as e:  # noqa: BLE001 单轮异常不能让调度进程停摆
            logger.warning("agent 轮次异常（下次心跳继续）：%s", e)


def _agent_tick() -> None:
    """一次心跳：站长上下文里跑一轮。

    单飞锁：上一轮没跑完就跳过本次。20 个 agent、workers=3 可能七八分钟，
    绝不能让 5 分钟的心跳叠出并发轮次。
    """
    if not _tick_lock.acquire(blocking=False):
        logger.info("agent 调度：上一轮未结束，跳过本次心跳")
        return
    try:
        _run_fleet_agents()
    finally:
        _tick_lock.release()


def _agent_loop_forever() -> None:
    """ASTOCK_AGENT_AUTO=1 时的主循环：先跑一次，之后每 AGENT_TICK_SEC 秒探一次。"""
    _agent_tick()
    while True:
        time.sleep(AGENT_TICK_SEC)
        try:
            _agent_tick()
        except Exception as e:  # noqa: BLE001 调度器绝不能因单次心跳异常停摆
            logger.warning("agent 调度心跳异常（下次继续）：%s", e)


def _housekeeping_forever() -> None:
    """每天清一次过期会话和旧用量记录，顺带清舰队库里按日累积的表。"""
    while True:
        try:
            n = auth.purge_sessions()
            m = ratelimit.purge()
            if n or m:
                logger.info("清理：过期会话 %d，旧用量记录 %d", n, m)
            # agent 库（决策日志、条件单、占位）按日累积，要定期清。舰队全站只有
            # 站长这一套，所以只在站长上下文里做一次；站长未设置时个人库会抛
            # RuntimeError，按「还没有舰队」跳过。站长目录里还没有 agents.db（建了
            # 管理员但 migrate 还没跑）也跳过：open_db 会凭空建一个空库，之后
            # migrate 看到「目标已存在」就不复制了，舰队数据会被这个空库顶掉。
            try:
                fleet = userctx.fleet_uid()
                if fleet and not os.path.exists(userctx.user_path("agents.db", fleet)):
                    logger.info("站长 %s 目录里还没有 agents.db（先跑 migrate），跳过 agent 库清理", fleet)
                else:
                    with userctx.as_fleet():
                        agent_store.purge()
            except RuntimeError:
                logger.info("尚未设置舰队站长，跳过 agent 库清理")
            except Exception as e:  # noqa: BLE001
                logger.warning("agent 库清理失败：%s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("清理任务异常：%s", e)
        time.sleep(86400)


def main() -> None:
    auth.init()
    ratelimit.init()
    n = len(auth.list_uids())
    logger.info("调度进程启动：%d 个账号，舰队站长 %s，agent 自动跑 %s，全站 AI 日预算 %d 次",
                n, userctx.fleet_uid() or "未设置", "开" if AGENT_AUTO else "关",
                ratelimit.AI_GLOBAL_DAY)
    if n == 0:
        logger.warning("还没有任何账号。先跑：python3 astockctl.py adduser <用户名> --admin")

    _shared_boot()
    userctx.spawn(_housekeeping_forever)
    if AGENT_AUTO:
        _agent_loop_forever()      # 前台阻塞，systemd 以此判断进程存活
    else:
        logger.info("agent 自动跑已关（ASTOCK_AGENT_AUTO=1 开启），主循环等待")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
