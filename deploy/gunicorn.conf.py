"""gunicorn 配置。启动：gunicorn -c deploy/gunicorn.conf.py wsgi:application"""
import multiprocessing
import os

# 只监听回环地址。对外由 tailscale serve（或 nginx）终止 TLS 再转进来——
# 应用进程永远不直接面对网络，这样即使安全组配错了也不至于裸奔。
bind = os.environ.get("ASTOCK_BIND", "127.0.0.1:5000")

# gthread 而不是默认的 sync：本项目几乎每个请求都在等外部 HTTP（腾讯/新浪/
# 东财/DeepSeek），是 I/O 密集而非 CPU 密集。线程能在等待时让出，sync worker
# 会被一个 40 秒的 AI 请求整个堵死。
worker_class = "gthread"
workers = int(os.environ.get("ASTOCK_WORKERS", "2"))
threads = int(os.environ.get("ASTOCK_THREADS", "8"))

# 每日推荐要跑 20~40 秒（v4-pro 是推理模型），全市场选股更久。默认 30 秒
# 会把这些请求直接杀掉，所以放宽到 5 分钟。
timeout = int(os.environ.get("ASTOCK_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5

# 预加载：主进程 import 完再 fork，几个 worker 共享代码页，也保证公共库的
# 建表只发生一次。各 store 都是用时才开 sqlite 连接、不在模块级持有句柄，
# 所以 fork 是安全的。
preload_app = True

# 收紧请求头，挡掉畸形请求打头的一类探测
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190

# 日志走 stdout/stderr，由 systemd 收进 journald（journalctl -u astock-web -f）
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("ASTOCK_LOG_LEVEL", "info").lower()
# 访问日志带上响应耗时(%(D)s 微秒)，排查「哪个接口变慢了」时有用
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus "%(a)s"'

# 定期回收 worker：本项目长跑会攒下线程池和缓存，定期换一批更稳。
# jitter 避免几个 worker 同时重启造成请求空档。
max_requests = 2000
max_requests_jitter = 200

proc_name = "astock-web"
