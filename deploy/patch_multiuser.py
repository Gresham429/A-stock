#!/usr/bin/env python3
"""把单人版改造成多用户版的补丁脚本（在仓库根目录运行）。

    python3 deploy/patch_multiuser.py --check    # 只看会改什么，不落盘
    python3 deploy/patch_multiuser.py            # 真的改

改完请务必 `git diff` 过一遍再提交。

# 它做了什么

1. 个人数据的 5 个 sqlite（notes/paper/rules/agents/profiles）和 2 个 json
   （watchlist/portfolio）：库路径从「模块常量」改成「按当前登录用户解析」，
   落到 data/users/<用户名>/ 下。一行 SQL 都没动，函数签名也没动。
2. 公共数据的 4 个 sqlite（news/universe/templates/factors）：路径不变，但
   连接改走 userctx.open_db 以开启 WAL——多进程读写同一个 sqlite 的必备。
3. app.py：挂上登录闸门和限流，把个人库的建表推迟到首个请求，
   把 agent 调度从 web 进程里摘掉（交给 scheduler.py），
   把 threading.Thread 换成会传递用户上下文的 userctx.Thread。
4. llm.py：顺手记下每次调用的真实 token 消耗，方便你看钱花在谁身上。

# 设计上的一个取舍

个人数据用「一人一个 sqlite 文件」而不是「一张表加 user_id 列」。后者要改
每一条 SQL、每一个函数签名、每一个调用点，在 15 万行的既有代码上风险很高；
前者只动路径解析。在几个人的规模下前者还顺带是优点：互不锁表、单独备份、
删人就是删一个目录。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 个人数据（每人一份）：模块名 -> 库文件名
PERSONAL_DBS = {
    "notes_store": "notes.db",
    "paper_store": "paper.db",
    "rules_store": "rules.db",
    "agent_store": "agents.db",
    "profile_store": "profiles.db",
}
# 公共数据（全站一份）：只改连接方式开 WAL，路径不动
SHARED_DBS = ["news_store", "universe_store", "template_store", "factor_lab"]

MARK = "import userctx"          # 幂等标记：已含此 import 的文件视为已打过补丁
_changed: list[str] = []
_skipped: list[str] = []


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _write(name: str, src: str, check: bool) -> None:
    _changed.append(name)
    if check:
        return
    with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
        f.write(src)


def _once(src: str, old: str, new: str, what: str) -> str:
    """精确替换且只替换一次。锚点对不上就直接报错——宁可不改，不能改错。"""
    n = src.count(old)
    if n != 1:
        raise SystemExit(
            f"✗ 锚点 [{what}] 在文件里出现 {n} 次（期望 1 次）。\n"
            f"  说明源文件和补丁预期的版本不一致，已中止，什么都没改。\n"
            f"  锚点内容：{old.strip().splitlines()[0][:80]}…")
    return src.replace(old, new)


def _add_import(src: str) -> str:
    """在 logger 定义之前插入 import userctx。"""
    anchor = "logger = logging.getLogger(__name__)"
    return _once(src, anchor, "import userctx\n\n" + anchor, "import userctx")


_CONN_RE = re.compile(
    r"def _conn\(\) -> sqlite3\.Connection:\n"
    r"    os\.makedirs\(_DIR, exist_ok=True\)\n"
    r"    conn = sqlite3\.connect\((?P<path>[\w()]+), check_same_thread=False, "
    r"timeout=(?P<to>\d+)\)\n"
    r"    conn\.row_factory = sqlite3\.Row\n"
    r"    return conn")


def _patch_conn(src: str, mod: str) -> str:
    """把 _conn 改成走 userctx.open_db（统一开 WAL + busy_timeout）。"""
    def repl(m: re.Match) -> str:
        return (f"def _conn() -> sqlite3.Connection:\n"
                f"    # 统一走 userctx.open_db：开 WAL，让多 worker 并发读写不互相阻塞\n"
                f"    return userctx.open_db({m.group('path')}, timeout={m.group('to')})")
    src, n = _CONN_RE.subn(repl, src)
    if n != 1:
        raise SystemExit(f"✗ {mod}.py 里没找到预期形状的 _conn()（匹配 {n} 次），已中止")
    return src


def patch_personal_db(mod: str, dbname: str, check: bool) -> None:
    """个人库：路径改成按当前用户解析，同时保留 DB_PATH 作为覆盖钩子。

    保留 DB_PATH 不是为了兼容好看——tests/ 里 test_agent_memory、test_paper_store
    就是靠 `agent_store.DB_PATH = 临时目录` 来隔离的。若直接删掉这个常量，那些
    测试会变成「设了一个没人读的属性」，然后静默地往真实数据目录里写，表面上
    还是绿的。留下钩子，既保住测试的隔离能力，也多一个应急的逃生口。
    """
    fn = f"{mod}.py"
    src = _read(fn)
    if MARK in src:
        _skipped.append(fn)
        return
    src = _add_import(src)
    # 先换引用点，再换定义行——否则新写进去的 DB_PATH 会被这一步顺手改掉
    src = src.replace("DB_PATH", "_db()")
    src = _once(
        src,
        f'_db() = os.path.join(_DIR, "{dbname}")',
        f'_DB_NAME = "{dbname}"\n'
        f'# 测试钩子：设成某个绝对路径即全局覆盖（tests/ 里用它做隔离）。\n'
        f'# 留空＝按当前登录用户解析，这是生产时唯一该走的分支。\n'
        f'DB_PATH = ""\n\n\n'
        f'def _db() -> str:\n'
        f'    """当前登录用户的库路径。个人数据按人隔离到 data/users/<用户名>/，\n'
        f'    见 userctx.py 里关于「为什么用路径隔离而不是加 user_id 列」的说明。"""\n'
        f'    return DB_PATH or userctx.user_path(_DB_NAME)\n\n',
        f"{mod} 的 DB_PATH")
    src = _patch_conn(src, mod)
    _write(fn, src, check)


def patch_shared_db(mod: str, check: bool) -> None:
    fn = f"{mod}.py"
    src = _read(fn)
    if MARK in src:
        _skipped.append(fn)
        return
    src = _add_import(src)
    src = _patch_conn(src, mod)
    _write(fn, src, check)


def patch_json_store(fn: str, const: str, filename: str, check: bool) -> None:
    """watchlist.json / portfolio.json 同样按人隔离。"""
    src = _read(fn)
    if MARK in src:
        _skipped.append(fn)
        return
    src = _add_import(src)
    # 同样：先换引用点，再换定义行
    src = src.replace(const, "_path()")
    src = _once(
        src,
        f'_path() = Path(__file__).parent / "{filename}"',
        f'# 测试钩子：设成某个 Path 即全局覆盖（tests/test_portfolio.py 用它做隔离）。\n'
        f'# 留 None ＝按当前登录用户解析，生产时走的是这一支。\n'
        f'{const}: Path | None = None\n\n\n'
        f'def _path() -> Path:\n'
        f'    """当前登录用户的 {filename}（多用户隔离，见 userctx.py）。"""\n'
        f'    return {const} or Path(userctx.user_path("{filename}"))\n',
        f"{fn} 的 {const}")
    _write(fn, src, check)


# ── app.py ────────────────────────────────────────────────────────────────────

APP_IMPORTS_OLD = "import websearch\n"
APP_IMPORTS_NEW = """import websearch

# ── 多用户改造引入的三个模块（见 README-deploy.md）──
import auth        # 登录闸门：默认全关，白名单极短
import ratelimit   # 按人限流 + AI 日预算：保护 DeepSeek 余额
import userctx     # 当前用户上下文 + 个人数据目录解析
"""

APP_INIT_OLD = """app = Flask(__name__)
news_store.init()  # 确保 news.db 表存在（廉价，幂等）
notes_store.init()  # 私域笔记表
rules_store.init()  # 交易规则库（首次灌入蒸馏种子）
paper_store.init()  # 模拟交易存档
profile_store.init()  # 本地多档投资画像（现金本金→按总资产分级玩法）
agent_store.init()  # agent 配置/日志/教训/条件单（表是增量加的，靠 IF NOT EXISTS 自愈）
template_store.init()  # 提示词模板版本化
factor_lab.init()  # 因子 IC 回测
"""

APP_INIT_NEW = '''app = Flask(__name__)
# 本项目的登录会话在 auth 的服务端 token 表里，不用 Flask 自带的签名 cookie
# session；secret_key 只是留个底，以免将来用到 flash/session 时踩空。
app.secret_key = os.environ.get("ASTOCK_SECRET_KEY") or secrets.token_hex(32)
# 请求体上限：本项目最大的写入是一条笔记，1MB 绰绰有余。不设上限的话，
# 一个几百 MB 的 POST 就能把内存吃光。
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# ── 公共数据：进程起来建一次表，所有人共用 ──
news_store.init()      # 确保 news.db 表存在（廉价，幂等）
template_store.init()  # 提示词模板版本化
factor_lab.init()      # 因子 IC 回测

# ── 个人数据：每人一个目录，建表必须等到知道「当前是谁」 ──
_user_inited: set[str] = set()
_user_init_lock = threading.Lock()


def ensure_user_stores(uid: str) -> None:
    """首次见到某个用户时，在他自己的目录里建好个人库的表。

    这几个 init() 原来是模块级调用的（单人本地跑，全局一份库）。多用户后库
    路径依赖「当前是谁」，模块级调用时还没有用户，只能推迟到该用户的第一个
    请求。用集合缓存，之后每个请求只多一次 set 查找。
    """
    if uid in _user_inited:
        return
    with _user_init_lock:
        if uid in _user_inited:      # 双检：并发的第一个请求只建一次
            return
        with userctx.as_user(uid):
            notes_store.init()    # 私域笔记表
            rules_store.init()    # 交易规则库（首次灌入蒸馏种子）
            paper_store.init()    # 模拟交易存档
            profile_store.init()  # 多档投资画像
            agent_store.init()    # agent 配置/日志/教训/条件单
        _user_inited.add(uid)
        logger.info("已为用户 %s 初始化个人数据目录", uid)


auth.init_app(app)        # 登录闸门 —— 必须先注册，后面的钩子依赖它设的 g.uid
ratelimit.init_app(app)   # 按人限流 + AI 日预算


@app.before_request
def _ensure_stores():  # noqa: ANN202
    """登录用户的个人库懒建表。auth 闸门已经放行才会走到这里。"""
    uid = getattr(g, "uid", "")
    if uid:
        ensure_user_stores(uid)


'''

APP_SCHED_OLD = """        # 启动即跑一次 + 之后每 5 分钟探一次（长期挂机也能每桶自动跑，见
        # plan/2026-07-17-intraday-agent-scheduler-design.md）。守护线程，不阻塞预热。
        threading.Thread(target=_agent_scheduler, daemon=True).start()
"""

APP_SCHED_NEW = """        # agent 日循环调度已搬到独立的 scheduler.py 进程。原因：gunicorn 起多个
        # worker 时，每个 worker 都会各起一份调度器，同一个 agent 会被并发决策、
        # 重复下单、重复写教训。这是正确性问题不是性能问题，所以必须单实例。
        # 下面这两个函数（_agent_tick / _agent_scheduler）保留，手动触发仍可用。
"""

# _universe_boot 里混着两行个人库的调用（agent_store 是每人一份的）。在「无用户」
# 的预热上下文里调它会直接抛异常——fail-closed 的设计本来就该把这种混用顶出来。
APP_UBOOT_OLD = """        template_store.init()
        template_store.purge()   # 按日累积的表一律配清理
        agent_store.init()
        agent_store.purge()
        factor_lab.init()
"""

APP_UBOOT_NEW = """        template_store.init()
        template_store.purge()   # 按日累积的表一律配清理
        # agent_store 是个人库（data/users/<用户名>/agents.db），不能在这里的
        # 「无当前用户」上下文里建表或清理。建表由 ensure_user_stores 在该用户的
        # 首个请求时完成；按日清理由 scheduler.py 遍历每个账号各做一次。
        factor_lab.init()
"""

APP_MAIN_OLD = '''if __name__ == "__main__":
    if news_store.stats()["total"] == 0:  # 首次运行：后台一次性回填新闻库(不阻塞启动)
        threading.Thread(target=news_store.backfill, daemon=True).start()
        logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
    threading.Thread(target=_universe_boot, daemon=True).start()
    threading.Thread(target=_review_boot, daemon=True).start()   # 复盘：首启回填情绪周期 + 每日自动调度
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
'''

APP_MAIN_NEW = '''if __name__ == "__main__":
    # 仅供本机开发。服务器上用：
    #   gunicorn -c deploy/gunicorn.conf.py wsgi:application   （web）
    #   python3 scheduler.py                                    （定时任务）
    # Flask 自带的开发服务器没有并发控制、没有请求超时、会泄漏版本号，
    # 不能对外提供服务——哪怕只对着几个朋友。
    if os.environ.get("ASTOCK_ENV") == "production":
        raise SystemExit(
            "生产环境请用 gunicorn 启动，不要直接跑 app.py：\\n"
            "  gunicorn -c deploy/gunicorn.conf.py wsgi:application")

    if news_store.stats()["total"] == 0:  # 首次运行：后台一次性回填新闻库(不阻塞启动)
        userctx.Thread(target=news_store.backfill, daemon=True).start()
        logger.info("首次运行：后台回填新闻库…（1–2 季度，约几分钟）")
    userctx.Thread(target=_universe_boot, daemon=True).start()
    userctx.Thread(target=_review_boot, daemon=True).start()
    logger.warning("开发模式：仅监听 127.0.0.1，定时任务请另跑 scheduler.py")
    logger.info("A股观察台启动 -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
'''


def patch_app(check: bool) -> None:
    src = _read("app.py")
    if MARK in src:
        _skipped.append("app.py")
        return
    src = _once(src, APP_IMPORTS_OLD, APP_IMPORTS_NEW, "app.py 导入块")
    src = _once(src, "import logging\nimport os\n", "import logging\nimport os\nimport secrets\n",
                "app.py import secrets")
    src = _once(src, "from flask import Flask, jsonify, render_template, request",
                "from flask import Flask, g, jsonify, render_template, request",
                "app.py flask 导入")
    src = _once(src, APP_INIT_OLD, APP_INIT_NEW, "app.py 建表块")
    src = _once(src, APP_SCHED_OLD, APP_SCHED_NEW, "app.py agent 调度启动")
    src = _once(src, APP_UBOOT_OLD, APP_UBOOT_NEW, "app.py 预热里的个人库调用")
    src = _once(src, APP_MAIN_OLD, APP_MAIN_NEW, "app.py __main__ 块")
    # 剩下所有后台线程都换成会带用户上下文的版本（参数写法完全不变）
    n = src.count("threading.Thread(")
    src = src.replace("threading.Thread(", "userctx.Thread(")
    print(f"  app.py: {n} 处 threading.Thread → userctx.Thread")
    _write("app.py", src, check)


LLM_OLD = """        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = data["choices"][0]"""

LLM_NEW = """        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _record_usage(data.get("usage") or {})
        choice = data["choices"][0]"""

LLM_HELPER = '''

def _record_usage(usage: dict) -> None:
    """把这次调用真实消耗的 token 记到当前用户名下。

    限额本身是按「次数」封顶的（次数才挡得住失控的循环），这里记 token 是为了
    让账单可归因——月底看到余额掉得快时，能查出是谁、哪天、哪个模型花的。
    记账失败绝不能影响正常功能，所以整段吞掉异常。
    """
    try:
        import ratelimit
        import userctx
        uid = userctx.get_uid()
        if uid and usage:
            ratelimit.record_llm_tokens(uid, usage.get("prompt_tokens", 0),
                                        usage.get("completion_tokens", 0))
    except Exception:  # noqa: BLE001 记账是旁路，出错静默
        pass
'''


def patch_llm(check: bool) -> None:
    src = _read("llm.py")
    if "_record_usage" in src:
        _skipped.append("llm.py")
        return
    src = _once(src, LLM_OLD, LLM_NEW, "llm.py 记账点")
    src = _once(src, "logger = logging.getLogger(__name__)",
                "logger = logging.getLogger(__name__)" + LLM_HELPER, "llm.py 记账函数")
    _write("llm.py", src, check)


# ── tests ─────────────────────────────────────────────────────────────────────

TEST_OLD = '''def test_claim_slot_is_exclusive():
    """同一 (agent, 日, 桶) 只能被抢到一次；不同桶互不影响。"""
    agent_store.init()'''

TEST_NEW = '''def test_claim_slot_is_exclusive():
    """同一 (agent, 日, 桶) 只能被抢到一次；不同桶互不影响。"""
    # 多用户改造后个人库按「当前登录用户」解析，而测试里没有当前用户，
    # 所以用 DB_PATH 钩子指到临时库（和 test_agent_memory 里一个做法）。
    # 顺带修掉一个原有副作用：这个用例过去是直接往真实 data/agents.db 里
    # 写 agent_id=-12345 的哨兵行的。
    agent_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "agents.db")
    agent_store.init()'''


def patch_tests(check: bool) -> None:
    """修一处会被多用户改造打破的测试（它原本依赖全局唯一的 agents.db）。"""
    fn = "tests/test_agent_gates.py"
    if not os.path.exists(os.path.join(ROOT, fn)):
        return
    src = _read(fn)
    if "tempfile" in src:
        _skipped.append(fn)
        return
    src = _once(src, "import os\nimport sys\n", "import os\nimport sys\nimport tempfile\n",
                "test_agent_gates 的 import")
    src = _once(src, TEST_OLD, TEST_NEW, "test_claim_slot_is_exclusive")
    _write(fn, src, check)


# ── 前端：加一个「当前账号 / 今日 AI 余额 / 退出」的角标 ──────────────────────

UI_SNIPPET = """      <!-- 多用户：当前账号 + 今日 AI 余额 + 退出 -->
      <span class="chip" id="mechip" title="当前登录账号">…</span>
      <span class="chip" id="quotachip" title="今日 AI 分析剩余次数">…</span>
      <a class="btn" href="/logout" title="退出登录" style="text-decoration:none">退出</a>
"""

UI_SCRIPT = """<script>
// 多用户改造：在页头显示当前账号和今天还剩多少次 AI 分析。
// 把余额显式摆出来，是为了让「今天的 AI 用完了」变成看得见的状态，
// 而不是等到点下去才收到一个 429。
(function () {
  const put = (id, txt, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    if (cls) el.classList.add(cls);
  };
  fetch('/api/me').then(r => r.ok ? r.json() : null).then(d => {
    if (d) put('mechip', '👤 ' + (d.display_name || d.uid));
  }).catch(() => {});
  const refreshQuota = () => fetch('/api/usage').then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const left = Math.max(0, d.ai_limit - d.ai_used);
    put('quotachip', `AI ${left}/${d.ai_limit}`, left > 0 ? 'ok' : null);
  }).catch(() => {});
  refreshQuota();
  setInterval(refreshQuota, 60000);
})();
</script>
"""


def patch_index(check: bool) -> None:
    """给看板页头加上账号角标。两个页面（index/review）都加。"""
    for fn, anchor in (
            ("templates/index.html", '      <span class="chip" id="aichip">🤖 …</span>\n'),
            ("templates/review.html", '      <span class="chip">🤖 DeepSeek v4-pro</span>\n')):
        path = os.path.join(ROOT, fn)
        if not os.path.exists(path):
            continue
        src = _read(fn)
        if "mechip" in src:
            _skipped.append(fn)
            continue
        src = _once(src, anchor, UI_SNIPPET + anchor, f"{fn} 页头工具条")
        src = _once(src, "</body>", UI_SCRIPT + "</body>", f"{fn} 结尾脚本")
        _write(fn, src, check)


def main() -> None:
    ap = argparse.ArgumentParser(description="单人版 → 多用户版补丁")
    ap.add_argument("--check", action="store_true", help="只检查不落盘")
    a = ap.parse_args()

    os.chdir(ROOT)
    if not os.path.exists("app.py"):
        sys.exit("请在仓库根目录运行这个脚本")
    if not os.path.exists("userctx.py"):
        sys.exit("缺少 userctx.py —— 请先把新增的几个文件放进仓库根目录再打补丁")

    print(f"{'【预演】' if a.check else '【执行】'} 多用户改造补丁  仓库：{ROOT}\n")

    for mod, db in PERSONAL_DBS.items():
        patch_personal_db(mod, db, a.check)
    patch_json_store("store.py", "WATCHLIST_PATH", "watchlist.json", a.check)
    patch_json_store("portfolio.py", "PORTFOLIO_PATH", "portfolio.json", a.check)
    for mod in SHARED_DBS:
        patch_shared_db(mod, a.check)
    patch_app(a.check)
    patch_llm(a.check)
    patch_tests(a.check)
    patch_index(a.check)

    print(f"\n{'将要修改' if a.check else '已修改'} {len(_changed)} 个文件：")
    for f in _changed:
        print(f"  · {f}")
    if _skipped:
        print(f"\n跳过 {len(_skipped)} 个（看起来已经打过补丁）：{', '.join(_skipped)}")
    if not a.check:
        print("\n下一步：")
        print("  git diff                              # 逐处过一遍改动")
        print("  python3 deploy/migrate_to_multiuser.py <你的用户名>   # 把现有数据迁到你名下")
        print("  python3 -m pytest tests -q            # 跑一遍原有测试")


if __name__ == "__main__":
    main()
