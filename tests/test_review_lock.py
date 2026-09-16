"""复盘跨进程文件锁单测（零依赖离线，临时目录，不打网络）。

D7：生产拓扑是 2 个 gunicorn worker + 1 个 scheduler，进程内的 app._review_job 字典互不可见，
所以互斥只能靠 data/review/.running-<date>（O_CREAT|O_EXCL）。钉住：
第二次 acquire False、is_running、release 后可再获取、超过 30 分钟的锁可覆盖、
内容损坏时按 mtime 判陈旧、run_review 拿不到锁返回 running、异常也释放锁。

跑：python3 tests/test_review_lock.py
"""
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review import pipeline as P  # noqa: E402
from review import store  # noqa: E402

TMP = tempfile.mkdtemp()
store.REVIEW_DIR = TMP        # pipeline 运行时读 store.REVIEW_DIR，不在 import 时绑定
D = "20260916"
LOCK = os.path.join(TMP, f".running-{D}")
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _write_lock(content: str) -> None:
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(content)


def test_acquire_twice():
    """首次 acquire True 且写入 pid/time；第二次 False；is_running 随之变化；别的日期不受影响。"""
    ck(P.is_running(D) is False, "初始不应 running")
    ck(P.acquire_lock(D) is True, "首次应拿到锁")
    ck(os.path.exists(LOCK), "锁文件应存在")
    pid, ts = open(LOCK, encoding="utf-8").read().split()
    ck(int(pid) == os.getpid(), f"锁内容应是本进程 pid: {pid}")
    ck(abs(float(ts) - time.time()) < 5, f"锁内容时间应近似 now: {ts}")
    ck(P.is_running(D) is True, "持锁时应 running")
    ck(P.acquire_lock(D) is False, "第二次应拿不到锁")
    ck(P.acquire_lock("20260915") is True, "其它日期应独立")
    P.release_lock("20260915")
    ck(not os.path.exists(os.path.join(TMP, ".running-20260915")), "release 应删文件")


def test_release_then_reacquire():
    """release 后文件消失、is_running False、可再获取；重复 release 不报错。"""
    P.release_lock(D)
    ck(not os.path.exists(LOCK), "release 后文件应消失")
    ck(P.is_running(D) is False, "release 后不应 running")
    ck(P.acquire_lock(D) is True, "release 后应可再获取")
    P.release_lock(D)
    P.release_lock(D)
    ck(not os.path.exists(LOCK), "重复 release 应无异常且文件不存在")


def test_stale_lock_overridable():
    """伪造 40 分钟前的锁：is_running False、acquire 覆盖并 warning、内容换成新 pid。"""
    _write_lock(f"99999 {time.time() - 40 * 60:.0f}")
    ck(P.is_running(D) is False, "陈旧锁不应算 running")
    with mock.patch.object(P.logger, "warning") as w:
        ck(P.acquire_lock(D) is True, "陈旧锁应可覆盖")
        ck(w.called, "覆盖前应 warning")
    ck(int(open(LOCK, encoding="utf-8").read().split()[0]) == os.getpid(), "覆盖后应是新 pid")
    ck(P.acquire_lock(D) is False, "覆盖后的新锁应再次互斥")
    P.release_lock(D)


def test_fresh_lock_not_overridable():
    """29 分钟前的锁未过阈值：不可覆盖、仍 running。"""
    _write_lock(f"99999 {time.time() - 29 * 60:.0f}")
    ck(P.acquire_lock(D) is False, "未过 30 分钟不应覆盖")
    ck(P.is_running(D) is True, "未过 30 分钟应算 running")
    P.release_lock(D)


def test_corrupt_lock_uses_mtime():
    """内容损坏（解析不出时间）按 mtime：新鲜则不可覆盖；mtime 调旧则可覆盖。"""
    for content in ("garbage", "", "123"):
        _write_lock(content)
        ck(P.acquire_lock(D) is False, f"损坏锁(新鲜) 不应覆盖: {content!r}")
        ck(P.is_running(D) is True, f"损坏锁(新鲜) 应算 running: {content!r}")
        old = time.time() - 40 * 60
        os.utime(LOCK, (old, old))
        ck(P.is_running(D) is False, f"损坏锁(mtime 陈旧) 不应算 running: {content!r}")
        ck(P.acquire_lock(D) is True, f"损坏锁(mtime 陈旧) 应可覆盖: {content!r}")
        P.release_lock(D)


def test_lock_age_missing_file():
    """文件不存在：_lock_age None、is_running False、release 不报错。"""
    ck(P._lock_age(LOCK) is None, "不存在应 None")
    ck(P.is_running(D) is False, "不存在不应 running")
    P.release_lock(D)
    ck(True, "")


def test_run_review_respects_lock():
    """run_review：已有存档 already 不拿锁；有锁 running；异常与正常结束都释放锁。"""
    env = {"target_date": D, "metrics": {"breadth": {"zt_count": 3}}}
    with mock.patch.object(P.fetch, "resolve_trade_date", return_value=D):
        with mock.patch.object(store, "load", return_value=env):
            r = P.run_review(D)
            ck(r["status"] == "already", f"已有存档应 already: {r}")
            ck(not os.path.exists(LOCK), "already 路径不应留下锁")
        with mock.patch.object(store, "load", return_value=None):
            P.acquire_lock(D)
            r = P.run_review(D)
            ck(r["status"] == "running" and r["target_date"] == D, f"有锁应 running: {r}")
            ck(os.path.exists(LOCK), "拿不到锁的一方不应删别人的锁")
            P.release_lock(D)
            with mock.patch.object(P, "_run_locked", side_effect=RuntimeError("boom")):
                try:
                    P.run_review(D)
                    ck(False, "异常应向外抛")
                except RuntimeError:
                    ck(True, "")
                ck(not os.path.exists(LOCK), "异常后锁应已释放")
            with mock.patch.object(P, "_run_locked", return_value={"status": "done"}) as rl:
                r = P.run_review(D, with_ai=False)
                ck(rl.called and r["status"] == "done", "无锁应进入 _run_locked")
                ck(not os.path.exists(LOCK), "正常结束锁应已释放")
        with mock.patch.object(store, "load", return_value=env):
            with mock.patch.object(P, "_run_locked", return_value={"status": "done"}) as rl:
                r = P.run_review(D, force=True)
                ck(rl.called and r["status"] == "done", "force 应绕过存档检查")
    with mock.patch.object(P.fetch, "resolve_trade_date", return_value=""):
        ck(P.run_review(D)["status"] == "error", "定不了交易日应 error")


def test_status_export():
    """包级导出 is_running 与 pipeline 同一函数；锁目录就是 store.REVIEW_DIR。"""
    import review
    ck(review.is_running is P.is_running, "review.is_running 应导出 pipeline.is_running")
    ck(P._lock_path(D) == LOCK, f"锁路径应在 store.REVIEW_DIR 下: {P._lock_path(D)}")


if __name__ == "__main__":
    for fn in (test_acquire_twice, test_release_then_reacquire, test_stale_lock_overridable,
               test_fresh_lock_not_overridable, test_corrupt_lock_uses_mtime,
               test_lock_age_missing_file, test_run_review_respects_lock, test_status_export):
        fn()   # 有顺序依赖（锁状态），不按字母序
    print(f"OK — test_review_lock 全过（{_n} 断言）")
