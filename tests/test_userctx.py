"""userctx 单测（零依赖离线，不打网络、不碰 data/）。钉住三件最容易静默出错的事：
  1. contextvars 不会自动跨线程——线程池任务必须走 ctx_map()，否则个人库读到「无用户」；
  2. user_path 是唯一把用户名变成磁盘路径的地方，路径穿越只能在这里挡；
  3. 舰队站长 fleet_uid() 的三级回退（环境变量 > set_fleet_uid > 空串）。

跑：python3 tests/test_userctx.py
"""
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import userctx  # noqa: E402

_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def test_as_user_nested_restores():
    """as_user 嵌套：内层退出回到外层，外层退出回到空。"""
    ck(userctx.get_uid() == "", "测试开始时不应有用户")
    with userctx.as_user("alice"):
        ck(userctx.get_uid() == "alice", "外层未切到 alice")
        with userctx.as_user("bob"):
            ck(userctx.get_uid() == "bob", "内层未切到 bob")
            ck(userctx.require_uid() == "bob", "require_uid 应返回当前用户")
        ck(userctx.get_uid() == "alice", "内层退出后未恢复为 alice")
    ck(userctx.get_uid() == "", "外层退出后未恢复为空")
    try:  # 异常路径也要恢复
        with userctx.as_user("carol"):
            raise KeyError("boom")
    except KeyError:
        pass
    ck(userctx.get_uid() == "", "as_user 内抛异常后未恢复")


def test_set_uid_and_require_uid():
    """set_uid 拒绝非法名；无用户时 require_uid 抛 RuntimeError 而不是静默落到共享文件。"""
    for bad in ("../etc", "Alice", "-x", "a", "a" * 33, "a/b"):
        try:
            userctx.set_uid(bad)
            ck(False, f"set_uid 应拒绝 {bad!r}")
        except ValueError:
            ck(True, "")
    ck(userctx.valid_uid("ab_c-9") and not userctx.valid_uid(""), "valid_uid 边界错")
    try:
        userctx.require_uid()
        ck(False, "无用户时 require_uid 应抛 RuntimeError")
    except RuntimeError:
        ck(True, "")


def test_ctx_map_propagates_uid_in_order():
    """ctx_map：池线程读到 uid、结果顺序同 items、确实在池线程里跑。"""
    main = threading.current_thread().name
    with userctx.as_user("alice"):
        with ThreadPoolExecutor(max_workers=3) as ex:
            out = userctx.ctx_map(
                ex, lambda i: (i, userctx.get_uid(), threading.current_thread().name),
                [4, 3, 2, 1, 0])
    ck([i for i, _, _ in out] == [4, 3, 2, 1, 0], f"顺序应同 items: {out}")
    ck(all(u == "alice" for _, u, _ in out), f"池线程未读到 uid: {out}")
    ck(all(t != main for _, _, t in out), "任务应在池线程里跑")
    # 对照：原生 ex.map 拿不到 uid——这正是 ctx_map 存在的理由
    with userctx.as_user("alice"):
        with ThreadPoolExecutor(max_workers=2) as ex:
            native = list(ex.map(lambda i: userctx.get_uid(), [1]))
    ck(native == [""], f"原生 ex.map 本应丢 uid（对照组）: {native}")


def test_ctx_map_raises():
    """任一任务异常向外抛，与 list(ex.map()) 语义一致。"""
    def boom(i):
        if i == 2:
            raise ValueError("x")
        return i
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            userctx.ctx_map(ex, boom, [1, 2, 3])
        ck(False, "ctx_map 应把任务异常抛出来")
    except ValueError:
        ck(True, "")


def test_ctx_map_isolates_context_per_task():
    """每个任务各拷一份上下文：任务内 set_uid 不泄漏到调用线程和其他任务。"""
    def mutate(i):
        userctx.set_uid(f"task{i}")
        return userctx.get_uid()
    with userctx.as_user("alice"):
        with ThreadPoolExecutor(max_workers=2) as ex:
            out = userctx.ctx_map(ex, mutate, [1, 2])
        ck(out == ["task1", "task2"], f"任务内 set_uid 应各自生效: {out}")
        ck(userctx.get_uid() == "alice", "任务内 set_uid 泄漏到了调用线程")


def test_spawn_and_thread_propagate():
    """spawn()/Thread() 替身把当前用户带进新线程。"""
    seen = []
    with userctx.as_user("bob"):
        userctx.spawn(lambda: seen.append(userctx.get_uid())).join()
        t = userctx.Thread(target=lambda: seen.append(userctx.get_uid()), daemon=True)
        t.start()
        t.join()
    ck(seen == ["bob", "bob"], f"新线程未读到 uid: {seen}")


def test_user_path_rejects_traversal():
    """user_path 拒绝含分隔符或以 . 开头的文件名；合法名落在 USERS_DIR/<uid>/ 下。"""
    old_users = userctx.USERS_DIR
    tmp = tempfile.mkdtemp()
    userctx.USERS_DIR = os.path.join(tmp, "users")
    try:
        for bad in ("a/b.db", "../x.db", ".hidden", "sub/../x", "/abs.db"):
            try:
                userctx.user_path(bad, uid="alice")
                ck(False, f"user_path 应拒绝 {bad!r}")
            except ValueError:
                ck(True, "")
        p = userctx.user_path("watchlist.json", uid="alice")
        ck(p == os.path.join(userctx.USERS_DIR, "alice", "watchlist.json"), f"路径错: {p}")
        ck(os.path.isdir(os.path.dirname(p)), "user_dir 应自动建目录")
        with userctx.as_user("bob"):
            ck(userctx.user_path("notes.db").endswith(os.path.join("bob", "notes.db")),
               "无 uid 参数时应按当前用户解析")
        try:
            userctx.user_dir("../evil")
            ck(False, "user_dir 应拒绝非法 uid")
        except ValueError:
            ck(True, "")
        ck(userctx.list_uids() == ["alice", "bob"], f"list_uids 应列出有目录的人: {userctx.list_uids()}")
    finally:
        userctx.USERS_DIR = old_users


def test_fleet_uid_three_level_fallback():
    """fleet_uid：环境变量 > set_fleet_uid > 空串；非法环境变量被忽略。"""
    saved = os.environ.pop("ASTOCK_FLEET_OWNER", None)
    try:
        userctx.set_fleet_uid("")
        ck(userctx.fleet_uid() == "", "都没有时应返回空串")
        userctx.set_fleet_uid("owner1")
        ck(userctx.fleet_uid() == "owner1", "应读到 set_fleet_uid 的值")
        os.environ["ASTOCK_FLEET_OWNER"] = "envowner"
        ck(userctx.fleet_uid() == "envowner", "环境变量应优先")
        os.environ["ASTOCK_FLEET_OWNER"] = "../Bad"
        ck(userctx.fleet_uid() == "owner1", "非法环境变量应被忽略、退回 set 值")
        try:
            userctx.set_fleet_uid("Bad Name")
            ck(False, "set_fleet_uid 应拒绝非法名")
        except ValueError:
            ck(True, "")
    finally:
        userctx.set_fleet_uid("")
        os.environ.pop("ASTOCK_FLEET_OWNER", None)
        if saved is not None:
            os.environ["ASTOCK_FLEET_OWNER"] = saved


def test_as_fleet_switch_and_restore():
    """as_fleet 切到站长，退出恢复；站长为空时 as_fleet 内 require_uid 抛 RuntimeError。"""
    saved = os.environ.pop("ASTOCK_FLEET_OWNER", None)
    try:
        os.environ["ASTOCK_FLEET_OWNER"] = "envowner"
        with userctx.as_user("friend"):
            with userctx.as_fleet() as u:
                ck(u == "envowner" and userctx.get_uid() == "envowner", "as_fleet 未切到站长")
            ck(userctx.get_uid() == "friend", "as_fleet 退出后未恢复原用户")
        ck(userctx.get_uid() == "", "退出后应为空")
        os.environ.pop("ASTOCK_FLEET_OWNER")
        userctx.set_fleet_uid("")
        with userctx.as_fleet():
            try:
                userctx.require_uid()
                ck(False, "站长为空时 as_fleet 内应 RuntimeError")
            except RuntimeError:
                ck(True, "")
    finally:
        os.environ.pop("ASTOCK_FLEET_OWNER", None)
        if saved is not None:
            os.environ["ASTOCK_FLEET_OWNER"] = saved


def test_fleet_flag_and_lazy_resolver():
    """as_fleet 置 in_fleet() 标志并随线程池传播、退出复位；
    站长为空时按注册的 resolver 惰性重查，TTL 内不重复查，找到后写入 set 值。"""
    saved = os.environ.pop("ASTOCK_FLEET_OWNER", None)
    try:
        os.environ["ASTOCK_FLEET_OWNER"] = "envowner"
        ck(userctx.in_fleet() is False, "默认不在舰队里")
        with userctx.as_fleet():
            ck(userctx.in_fleet() is True, "as_fleet 内 in_fleet 应为 True")
            with ThreadPoolExecutor(max_workers=2) as ex:
                got = userctx.ctx_map(ex, lambda _: (userctx.in_fleet(), userctx.get_uid()), [1, 2])
            ck(got == [(True, "envowner")] * 2, f"标志应随 ctx_map 传进线程池: {got}")
            with userctx.as_user("friend"):
                ck(userctx.in_fleet() is True, "as_fleet 里再 as_user 不清标志")
        ck(userctx.in_fleet() is False, "退出 as_fleet 后标志应复位")
        os.environ.pop("ASTOCK_FLEET_OWNER")

        calls = []
        answers = ["", "lazyowner"]
        userctx.set_fleet_uid("")
        userctx.set_fleet_resolver(lambda: (calls.append(1), answers[min(len(calls) - 1, 1)])[1])
        ck(userctx.fleet_uid() == "" and len(calls) == 1, "为空时应调 resolver 一次")
        ck(userctx.fleet_uid() == "" and len(calls) == 1, "TTL 内不应再查")
        userctx._fleet_resolved_at = 0.0
        ck(userctx.fleet_uid() == "lazyowner" and len(calls) == 2, "TTL 过后重查并拿到站长")
        ck(userctx.fleet_uid() == "lazyowner" and len(calls) == 2, "拿到后不再查")
        userctx.set_fleet_resolver(lambda: 1 / 0)
        userctx.set_fleet_uid("")
        ck(userctx.fleet_uid() == "", "resolver 抛异常按未设置处理")
    finally:
        userctx.set_fleet_resolver(None)
        userctx.set_fleet_uid("")
        os.environ.pop("ASTOCK_FLEET_OWNER", None)
        if saved is not None:
            os.environ["ASTOCK_FLEET_OWNER"] = saved


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
    print(f"OK — test_userctx 全过（{_n} 断言）")
