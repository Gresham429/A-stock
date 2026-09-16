"""gunicorn 入口。

    gunicorn -c deploy/gunicorn.conf.py wsgi:application

为什么要单独一个入口：app.py 的 `if __name__ == "__main__"` 块里会拉起
新闻回填、全市场池预热、agent 调度和复盘调度这四个后台线程。用 gunicorn
起多个 worker 时，每个 worker 都会各跑一份——重复写库、重复调 LLM、
同一个 agent 被并发跑两次。所以 web 进程只负责处理请求，所有定时任务
交给独立的 scheduler.py 进程（systemd 里是两个 service）。
"""
from __future__ import annotations

import app as _app

application = _app.app
