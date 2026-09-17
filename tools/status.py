#!/usr/bin/env python3
"""tools/status.py  仓库现状由脚本生成，不手写。

打印四块：git 位置、HTTP 路由数、超过 400 行的 .py 文件、离线测试逐个结果、
plan/ 下每份文档的第一行标题与「状态：」行。文档里凡是能从这里得到的数字，
一律不再手写，写「见 python3 tools/status.py」。

用法：
    python3 tools/status.py             # 全部，含跑一遍离线测试（约半分钟）
    python3 tools/status.py --no-tests  # 不跑测试
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTE_FILES = ("app.py", "auth.py", "picks_routes.py", "alerts_routes.py", "ratelimit.py")
ROUTE_RE = re.compile(r"^\s*@(?:app|bp)\.(?:route|get|post|put|delete)\(", re.M)
BIG_LINES = 400


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "?"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def routes() -> dict[str, int]:
    out: dict[str, int] = {}
    for name in ROUTE_FILES:
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            out[name] = len(ROUTE_RE.findall(_read(path)))
    return out


def big_files() -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for pattern in ("*.py", "review/*.py", "deploy/*.py", "static/*.js"):
        for path in glob.glob(os.path.join(ROOT, pattern)):
            n = _read(path).count("\n")
            if n > BIG_LINES:
                rows.append((n, os.path.relpath(path, ROOT)))
    return sorted(rows, reverse=True)


def run_tests() -> list[tuple[str, str]]:
    """逐个跑 tests/test_*.py，取最后一行输出作为结果（各测试自己打印断言数）。"""
    rows: list[tuple[str, str]] = []
    env = dict(os.environ, NO_PROXY="*", PYTHONDONTWRITEBYTECODE="1")
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
        name = os.path.basename(path)
        try:
            proc = subprocess.run([sys.executable, path], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=300)
            tail = (proc.stdout.strip().splitlines() or [""])[-1]
            status = "通过" if proc.returncode == 0 else f"失败(exit {proc.returncode})"
            if proc.returncode != 0:
                tail = (proc.stderr.strip().splitlines() or [tail])[-1]
        except subprocess.TimeoutExpired:
            status, tail = "超时", ""
        rows.append((name, f"{status}  {tail}".rstrip()))
    return rows


def plan_docs() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for path in sorted(glob.glob(os.path.join(ROOT, "plan", "*.md"))):
        title, status = "", ""
        for line in _read(path).splitlines():
            if not title and line.startswith("#"):
                title = line.lstrip("#").strip()
            elif line.startswith("状态："):
                status = line[3:].strip()
                break
        rows.append((os.path.basename(path), title, status))
    return rows


def main() -> int:
    no_tests = "--no-tests" in sys.argv[1:]
    print(f"git: {_git('rev-parse', '--abbrev-ref', 'HEAD')} @ {_git('rev-parse', '--short', 'HEAD')}"
          f"  未提交改动 {len(_git('status', '--porcelain').splitlines())} 处")

    r = routes()
    print(f"\n路由: 共 {sum(r.values())} 条  " + "  ".join(f"{k} {v}" for k, v in r.items()))

    print(f"\n超过 {BIG_LINES} 行的文件:")
    for n, path in big_files():
        print(f"  {n:5d}  {path}")

    tests = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    print(f"\n离线测试: {len(tests)} 个文件" + ("（--no-tests 未跑）" if no_tests else ""))
    if not no_tests:
        for name, result in run_tests():
            print(f"  {name:32s} {result}")

    print("\nplan/ 文档:")
    for name, title, status in plan_docs():
        print(f"  {name:55s} {status or '-':22s} {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
