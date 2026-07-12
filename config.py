"""配置加载：从 .env 读取 DeepSeek 密钥/模型（零第三方依赖）。

密钥只存在 .env（已 gitignore），源码不硬编码任何 key。
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"


def _load_env() -> None:
    """把 .env 里的键值加载进 os.environ（不覆盖已存在的系统环境变量）。"""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_BASE_URL: str = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def llm_enabled() -> bool:
    """是否已配置可用的 DeepSeek key。"""
    return bool(DEEPSEEK_API_KEY and DEEPSEEK_API_KEY.startswith("sk-"))
