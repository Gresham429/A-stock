#!/usr/bin/env python3
"""把单人版的现有数据迁到「你」名下。在仓库根目录运行：

    python3 deploy/migrate_to_multiuser.py <你的用户名>

做的事：把 watchlist.json / portfolio.json 和 5 个个人 sqlite 复制到
data/users/<你的用户名>/ 下。原文件一个都不删——确认一切正常之后你自己删。

公共数据（news.db / universe.db / factors.db / templates.db / data/review/）
留在原地不动，所有人共享。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import userctx  # noqa: E402

# (源文件相对仓库根的路径, 目标文件名)
PERSONAL = [
    ("watchlist.json", "watchlist.json"),
    ("portfolio.json", "portfolio.json"),
    ("data/notes.db", "notes.db"),
    ("data/paper.db", "paper.db"),
    ("data/rules.db", "rules.db"),
    ("data/agents.db", "agents.db"),
    ("data/profiles.db", "profiles.db"),
]


def _copy_db(src: str, dst: str) -> str:
    """用 sqlite 的在线备份接口复制库。

    直接 cp 一个开着 WAL 的库会漏掉 -wal 里尚未 checkpoint 的事务，
    复制出来的是个旧快照。backup() 接口会把 WAL 一并并进去，拿到的是
    完整且一致的副本——这也是 backup.sh 用同一套做法的原因。
    """
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return f"{os.path.getsize(dst) / 1024:.0f} KB"


def main() -> None:
    ap = argparse.ArgumentParser(description="单人数据 -> 多用户目录")
    ap.add_argument("uid", help="你的用户名（要和 astockctl.py adduser 建的那个一致）")
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    a = ap.parse_args()

    if not userctx.valid_uid(a.uid):
        sys.exit("用户名只能是小写字母/数字/下划线/连字符，2-32 位")

    os.chdir(ROOT)
    dest = userctx.user_dir(a.uid)
    print(f"迁移目标：{dest}\n")

    moved = skipped = 0
    for rel, name in PERSONAL:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(dest, name)
        if not os.path.exists(src):
            print(f"  －  {rel:<22} 不存在，跳过")
            continue
        if os.path.exists(dst) and not a.force:
            print(f"  !   {rel:<22} 目标已存在，跳过（要覆盖加 --force）")
            skipped += 1
            continue
        if rel.endswith(".db"):
            size = _copy_db(src, dst)
            print(f"  [ok] {rel:<22} -> {name}  ({size})")
        else:
            shutil.copy2(src, dst)
            print(f"  [ok] {rel:<22} -> {name}")
        moved += 1

    print(f"\n完成：复制 {moved} 个，跳过 {skipped} 个。原文件全部保留。")
    if skipped and not moved:
        sys.exit("目标文件已全部存在，一个都没复制。若它们是服务先起来时自动建出的空库"
                 "（建完账号先登录过、或调度进程先跑过），加 --force 重跑覆盖：\n"
                 f"    python3 deploy/migrate_to_multiuser.py {a.uid} --force")
    print("\n验收步骤：")
    print("  1. 启动服务，用这个账号登录，确认自选股/持仓/画像/agent 都在")
    print("  2. 确认无误后，再手动删掉仓库根目录的 watchlist.json、portfolio.json")
    print("     和 data/ 下那 5 个个人 db（它们已经不会再被读到了）")
    print("  3. 给朋友建账号：python3 astockctl.py adduser <名字>")
    print("     新账号是干净的——自选股会用 store.py 里的默认 6 只初始化")


if __name__ == "__main__":
    main()
