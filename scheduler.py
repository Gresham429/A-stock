"""独立调度进程：所有定时任务都在这里跑，web 进程一个都不跑。

    python3 scheduler.py

# 为什么必须拆出来

原来 app.py 在 __main__ 里拉起四个后台线程（新闻回填、全市场池预热、
agent 日循环、复盘）。本地单进程跑没问题，但上 gunicorn 之后每个 worker
都会各起一份：同一个 agent 被并发决策两次、同一天的复盘生成两遍、
universe 回填互相抢锁。这不是性能问题，是正确性问题——会产生重复的
模拟委托和自相矛盾的教训记录。

拆开之后：web 进程无状态、可以随便重启和加 worker；调度进程单实例、
崩了 systemd 拉起来、日志独立。

# 公共任务 vs 每人任务

公共（全站只跑一次）：新闻库回填、全市场池刷新、板块归属回填、因子 IC
回测、每日复盘——这些是市场数据，跟谁登录没关系。

每人（遍历所有账号各跑一遍）：agent 日循环。每个人的 agent 配置、模拟
账户和教训库都在自己的目录里，必须切到那个人的上下文才跑得对。
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

AGENT_TICK_SEC = int(os.environ.get("ASTOCK_AGENT_TICK_SEC", "300"))
_tick_lock = threading.Lock()


def _shared_boot() -> None:
    """公共数据预热：新闻库 + 全市场池 + 因子 + 复盘。全站只跑一次。"""
    try:
        if news_store.stats()["total"] == 0:
            logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
            userctx.spawn(news_store.backfill)
    except Exception as e:  # noqa: BLE001
        logger.warning("新闻库检查失败（不影响其余）：%s", e)

    # _universe_boot 内部已做好异常兜底；补丁已把它里面启动 agent 调度那行拿掉，
    # agent 调度改由本文件的 _agent_loop_forever 统一管（要遍历用户）。
    userctx.spawn(web._universe_boot)
    userctx.spawn(web._review_boot)


def _run_agents_for(uid: str) -> None:
    """在某个用户的上下文里跑一轮 agent 日循环。"""
    with userctx.as_user(uid):
        try:
            web.ensure_user_stores(uid)
            agents = agent_store.list_agents(active_only=True)
            if not agents:
                return
            # 自动跑同样要过全站 AI 日预算——定时任务比人更容易悄悄把余额跑光
            if not ratelimit.consume(uid, "ai", f"scheduler:agents({len(agents)})",
                                     n=len(agents)):
                logger.warning("[%s] 全站 AI 预算已用尽，本轮 agent 跳过", uid)
                return
            for r in agent_loop.run_all(require_open=True):
                if r.get("skipped"):
                    logger.info("[%s] agent %s: %s", uid, r.get("agent", "-"), r["skipped"])
                elif r.get("ok"):
                    logger.info("[%s] agent %s [%s]: 挂出 %d / 成交 %d / 教训 %d",
                                uid, r.get("agent"), r.get("slot", "-"),
                                len(r.get("placed") or []), len(r.get("filled") or []),
                                len(r.get("lessons") or []))
        except Exception as e:  # noqa: BLE001 一个人出错不能拖垮其他人
            logger.warning("[%s] agent 轮次异常（其他用户继续）：%s", uid, e)


def _agent_tick() -> None:
    """一次心跳：遍历所有启用的账号，各跑一轮。

    单飞锁：上一轮没跑完就跳过本次。一个人 20 个 agent、workers=3 就可能
    七八分钟，几个人串起来更久，绝不能让 5 分钟的心跳叠出并发轮次。
    """
    if not _tick_lock.acquire(blocking=False):
        logger.info("agent 调度：上一轮未结束，跳过本次心跳")
        return
    try:
        uids = auth.list_uids()
        if not uids:
            return
        logger.debug("agent 心跳：%d 个账号", len(uids))
        for uid in uids:
            _run_agents_for(uid)
    finally:
        _tick_lock.release()


def _agent_loop_forever() -> None:
    _agent_tick()
    while True:
        time.sleep(AGENT_TICK_SEC)
        try:
            _agent_tick()
        except Exception as e:  # noqa: BLE001 调度器绝不能因单次心跳异常停摆
            logger.warning("agent 调度心跳异常（下次继续）：%s", e)


def _housekeeping_forever() -> None:
    """每天清一次过期会话和旧用量记录。顺手做个体检日志。"""
    while True:
        try:
            n = auth.purge_sessions()
            m = ratelimit.purge()
            if n or m:
                logger.info("清理：过期会话 %d，旧用量记录 %d", n, m)
            # agent 库里按日累积的表（决策日志、条件单、占位）要定期清，
            # 这活原来挂在 app 的预热里，但那是「无用户」上下文——个人库
            # 必须逐个账号切进去做。
            for uid in auth.list_uids():
                with userctx.as_user(uid):
                    try:
                        agent_store.purge()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[%s] agent 库清理失败：%s", uid, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("清理任务异常：%s", e)
        time.sleep(86400)


def main() -> None:
    auth.init()
    ratelimit.init()
    n = len(auth.list_uids())
    logger.info("调度进程启动：%d 个账号，agent 心跳 %ds，"
                "全站 AI 日预算 %d 次", n, AGENT_TICK_SEC, ratelimit.AI_GLOBAL_DAY)
    if n == 0:
        logger.warning("还没有任何账号。先跑：python3 astockctl.py adduser <用户名> --admin")

    _shared_boot()
    userctx.spawn(_housekeeping_forever)
    _agent_loop_forever()      # 前台阻塞，systemd 以此判断进程存活


if __name__ == "__main__":
    main()
