"""密码哈希、口令强度、登录失败锁定——auth.py 拆出来的纯逻辑部分。

这里不持有数据库连接和锁，锁定相关函数都以「已打开的 sqlite 连接」为参数，
由 auth.py 决定在什么锁、什么事务里调用。这样本模块零依赖 auth，不会
循环导入，也能单独离线测。

# 锁定键

login_fails 表的主键叫 uid，但实际存的是「锁定键」：
  - 用户名本身（如 "xiaoming"）：挡对单个账号的慢速撞库；
  - "ip:<来源IP>"：挡同一来源换着用户名撞，以及用不存在的用户名探测。
两种键共用同一阈值，任意一个到阈值就拒绝登录。用户名和 "ip:" 前缀不可能
撞车，因为合法用户名不含冒号。
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time

logger = logging.getLogger(__name__)

# 登录失败节流：同一个键连续失败 N 次后锁 M 秒。挡的是慢速撞库，不影响手滑。
MAX_FAILS = int(os.environ.get("ASTOCK_MAX_LOGIN_FAILS", "5"))
LOCK_SECONDS = int(os.environ.get("ASTOCK_LOGIN_LOCK_SEC", "300"))

SCRYPT_N, SCRYPT_R, SCRYPT_P, DK_LEN = 2 ** 14, 8, 1, 32
SALT_LEN = 16

# 用户不存在 / 已停用时也走一遍同样开销的哈希用的固定盐，
# 避免用响应时间探测哪些用户名存在。
_DUMMY_SALT = b"0" * SALT_LEN


# 密码


def hash_password(password: str, salt: bytes) -> bytes:
    """scrypt 派生密钥。内存硬，比 pbkdf2 抗 GPU 爆破；标准库自带，不引第三方。"""
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_LEN)


def burn_hash(password: str) -> None:
    """只为耗时、不用结果：让「用户不存在」分支和正常校验分支耗时一致。"""
    hash_password(password, _DUMMY_SALT)


def check_password_strength(pw: str) -> str:
    """返回空串表示通过，否则返回给人看的原因。

    只挡最糟的情况（太短、纯数字、常见弱口令）。规则定得太狠会逼人把密码
    写在便签上，反而更差。
    """
    if len(pw) < 10:
        return "密码至少 10 位"
    if pw.isdigit():
        return "不能是纯数字"
    weak = {"1234567890", "password12", "qwertyuiop", "0123456789", "1111111111"}
    if pw.lower() in weak:
        return "这个密码太常见了"
    return ""


# 登录失败锁定


def ip_key(ip: str) -> str:
    """来源 IP 对应的锁定键；ip 为空返回空串（调用方应跳过）。"""
    ip = (ip or "").strip()
    return f"ip:{ip[:64]}" if ip else ""


def locked_for(c: sqlite3.Connection, key: str) -> int:
    """某个键还要锁多少秒；未锁返回 0。"""
    if not key:
        return 0
    r = c.execute("SELECT locked_until FROM login_fails WHERE uid=?", (key,)).fetchone()
    if not r:
        return 0
    return max(0, int(r["locked_until"] - time.time()))


def note_fail(c: sqlite3.Connection, key: str) -> int:
    """记一次失败；到阈值就锁定。返回累计失败次数。调用方负责持锁。"""
    if not key:
        return 0
    r = c.execute("SELECT fails, locked_until FROM login_fails WHERE uid=?", (key,)).fetchone()
    fails = r["fails"] if r else 0
    if r and r["locked_until"] and r["locked_until"] <= time.time():
        fails = 0   # 上一次锁定已到期：从零重计，每次锁定都要重新攒满 MAX_FAILS 次
    fails += 1
    until = time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0
    c.execute("INSERT INTO login_fails(uid,fails,locked_until) VALUES(?,?,?)"
              " ON CONFLICT(uid) DO UPDATE SET fails=excluded.fails,"
              " locked_until=excluded.locked_until", (key, fails, until))
    if until:
        logger.warning("键 %s 连续登录失败 %d 次，锁定 %d 秒", key, fails, LOCK_SECONDS)
    return fails


def clear_fails(c: sqlite3.Connection, *keys: str) -> None:
    """登录成功后清掉相关键的计数。调用方负责持锁。"""
    for k in keys:
        if k:
            c.execute("DELETE FROM login_fails WHERE uid=?", (k,))
