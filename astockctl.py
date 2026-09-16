#!/usr/bin/env python3
"""管理命令行：建账号、改密码、停用、看用量。

    python3 astockctl.py adduser <你的用户名> --admin
    python3 astockctl.py adduser xiaoming --name 小明
    python3 astockctl.py users
    python3 astockctl.py usage --days 7
    python3 astockctl.py passwd xiaoming
    python3 astockctl.py disable xiaoming
    python3 astockctl.py kick xiaoming

设计取向：不做网页版用户管理界面。几个人的规模下，"注册入口"是纯粹的
攻击面——没有注册页，就没有人能自己给自己开号。你 ssh 上去敲一条命令
就完事，也不用担心管理界面本身有洞。
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config      # noqa: E402,F401  先加载 .env，否则 usage/whoami 打印的是代码默认值
import auth        # noqa: E402
import ratelimit   # noqa: E402
import userctx     # noqa: E402


def _gen_password(n: int = 16) -> str:
    """生成一个好念也好打的随机密码。去掉了容易看混的 0O1lI。"""
    alphabet = (string.ascii_lowercase.replace("l", "").replace("o", "")
                + string.ascii_uppercase.replace("O", "").replace("I", "")
                + "23456789" + "-_")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _ask_password(prompt: str = "密码") -> str:
    """交互式输密码；直接回车则随机生成一个并打印出来。"""
    pw = getpass.getpass(f"{prompt}（直接回车＝自动生成）: ")
    if not pw:
        pw = _gen_password()
        print(f"\n  自动生成的密码： {pw}")
        print("  请立刻用安全的方式发给对方（不要走微信明文），本命令不会再显示第二次。\n")
        return pw
    again = getpass.getpass("再输一次: ")
    if pw != again:
        sys.exit("两次输入不一致")
    why = auth.check_password_strength(pw)
    if why:
        sys.exit(f"密码不合要求：{why}")
    return pw


def cmd_adduser(a: argparse.Namespace) -> None:
    pw = _ask_password()
    auth.create_user(a.uid, pw, display_name=a.name or "", is_admin=a.admin)
    print(f"已创建 {a.uid}"
          f"{'（管理员）' if a.admin else ''}，数据目录：{userctx.user_dir(a.uid)}")


def cmd_passwd(a: argparse.Namespace) -> None:
    pw = _ask_password("新密码")
    auth.set_password(a.uid, pw)
    print(f"{a.uid} 的密码已更新，其所有登录会话已失效（需要重新登录）")


def _require_user(uid: str) -> None:
    """auth.set_disabled 对不存在的 uid 是空 UPDATE、不报错，这里先查一次，别假成功。"""
    if auth.get_user(uid) is None:
        raise ValueError(f"用户 {uid} 不存在")


def cmd_disable(a: argparse.Namespace) -> None:
    _require_user(a.uid)
    auth.set_disabled(a.uid, True)
    print(f"{a.uid} 已停用，在线会话已踢掉。数据目录保留，用 enable 可随时恢复")


def cmd_enable(a: argparse.Namespace) -> None:
    _require_user(a.uid)
    auth.set_disabled(a.uid, False)
    print(f"{a.uid} 已启用")


def cmd_kick(a: argparse.Namespace) -> None:
    """只踢会话不停号——朋友手机丢了但人还在用的场景。"""
    n = auth.kick(a.uid)
    print(f"已踢掉 {a.uid} 的 {n} 个登录会话")


def cmd_users(a: argparse.Namespace) -> None:
    rows = auth.list_users()
    if not rows:
        print("还没有任何账号。先建一个：python3 astockctl.py adduser <用户名> --admin")
        return
    print(f"{'用户名':<16}{'昵称':<12}{'管理员':<8}{'状态':<8}{'在线':<6}{'最后登录'}")
    print("─" * 72)
    for r in rows:
        print(f"{r['uid']:<16}{(r['display_name'] or '')[:10]:<12}"
              f"{'是' if r['is_admin'] else '':<8}"
              f"{'已停用' if r['disabled'] else '正常':<8}"
              f"{r['sessions']:<6}{(r['last_login'] or '从未')[:16]}")


def cmd_usage(a: argparse.Namespace) -> None:
    rows = ratelimit.all_usage(a.days)
    if not rows:
        print("这段时间没有用量记录")
        return
    print(f"最近 {a.days} 天用量（AI 每人日限 {ratelimit.AI_PER_USER_DAY}，"
          f"全站日限 {ratelimit.AI_GLOBAL_DAY}）")
    print(f"{'日期':<12}{'用户':<14}{'AI':>6}{'重任务':>8}{'输入token':>12}{'输出token':>12}")
    print("─" * 66)
    for r in rows:
        print(f"{r['day']:<12}{r['uid']:<14}{r['ai']:>6}{r['heavy']:>8}"
              f"{r['prompt']:>12,}{r['completion']:>12,}")


def cmd_whoami(a: argparse.Namespace) -> None:
    """打印当前配置概览，部署完自检用。"""
    print(f"数据目录      : {userctx.DATA_DIR}")
    print(f"用户数据目录  : {userctx.USERS_DIR}")
    print(f"账号数        : {len(auth.list_users())}")
    print(f"磁盘上的用户  : {', '.join(userctx.list_uids()) or '（无）'}")
    print(f"舰队站长      : {auth.fleet_uid_hint() or '未设置'}"
          "  （ASTOCK_FLEET_OWNER 优先，否则最早创建的管理员）")
    print(f"会话有效期    : {auth.SESSION_DAYS} 天")
    print(f"Secure cookie : {auth.COOKIE_SECURE}  （走 https 时必须为 True）")
    print(f"AI 每人/天    : {ratelimit.AI_PER_USER_DAY}")
    print(f"AI 全站/天    : {ratelimit.AI_GLOBAL_DAY}")
    print(f"AI 最小间隔   : {ratelimit.AI_MIN_INTERVAL}s")


def main() -> None:
    auth.init()
    ratelimit.init()

    p = argparse.ArgumentParser(description="A股观察台 · 账号与用量管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("adduser", help="新建账号")
    s.add_argument("uid", help="用户名（小写字母/数字/_-，2-32 位）")
    s.add_argument("--name", default="", help="显示昵称")
    s.add_argument("--admin", action="store_true", help="设为管理员")
    s.set_defaults(func=cmd_adduser)

    s = sub.add_parser("passwd", help="改密码（并踢掉该用户所有会话）")
    s.add_argument("uid")
    s.set_defaults(func=cmd_passwd)

    s = sub.add_parser("disable", help="停用账号")
    s.add_argument("uid")
    s.set_defaults(func=cmd_disable)

    s = sub.add_parser("enable", help="启用账号")
    s.add_argument("uid")
    s.set_defaults(func=cmd_enable)

    s = sub.add_parser("kick", help="踢掉某人所有登录会话（不停号）")
    s.add_argument("uid")
    s.set_defaults(func=cmd_kick)

    s = sub.add_parser("users", help="列出所有账号")
    s.set_defaults(func=cmd_users)

    s = sub.add_parser("usage", help="查看 AI 用量")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_usage)

    s = sub.add_parser("status", help="打印当前配置概览（部署自检用）")
    s.set_defaults(func=cmd_whoami)

    a = p.parse_args()
    try:
        a.func(a)
    except ValueError as e:
        sys.exit(f"错误：{e}")


if __name__ == "__main__":
    main()
