"""到点提醒（零依赖、离线、不打网络）。

**为什么有这个文件**：提醒的触发条件错一次，用户就会收到不该发的推送，或者该发的收不到，
而这两件事都不会报错。这里把三类规则钉死：点位文本的解析（含空位与整批拒绝）、
「已进入 / 接近」的边界算术、同一标的每天只发一次。

跑法：python3 tests/test_alerts.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts          # noqa: E402
import alerts_store    # noqa: E402
import notify          # noqa: E402

# setup_env 会把 notify.send_all 换成假的（它在 alerts 里就是同一个模块），
# 所以这里先留一份真的，给专门测发送层的那两个用例还原用。
_REAL_SEND_ALL = notify.send_all
_REAL_POST = notify._post      # 前面的用例会把 _post 换成假的，测真实现时要还原

N = [0]


def ck(cond, msg):
    N[0] += 1
    assert cond, msg


# ── 点位文本解析 ──────────────────────────────────────────────────────────
def test_parse_point_line_forms():
    r = alerts_store.parse_point_line("600519 1680 1700 1850 1880 1600 等回踩")
    ck((r["buy_lo"], r["buy_hi"], r["sell_lo"], r["sell_hi"], r["stop"]) == (1680, 1700, 1850, 1880, 1600),
       f"五个数字位应按买下买上卖下卖上止损解析: {r}")
    ck(r["note"] == "等回踩", f"备注没解析出来: {r['note']}")
    r2 = alerts_store.parse_point_line("600519 1680 - - - 1600")
    ck(r2["buy_lo"] == r2["buy_hi"] == 1680, "只给一个数应视作上下界相同")
    ck(r2["sell_lo"] is None and r2["sell_hi"] is None, "空位应保持 None")
    r3 = alerts_store.parse_point_line("600519 1680 1700 就这个价")   # 数字位用完后的都算备注
    ck(r3["note"] == "就这个价" and r3["buy_hi"] == 1700, f"数字位后的都算备注: {r3}")


def test_parse_point_line_rejects_bad_input():
    for line, why in [("60051 10 11", "代码不是 6 位"),
                      ("600519 1700 1680", "买点下界大于上界"),
                      ("600519 - - - - -", "一个点都没有"),
                      ("600519 abc", "数字位不是数字"),
                      ("600519 卖 12.5", "第一个数字位前出现非数字（顺序写错）")]:
        try:
            alerts_store.parse_point_line(line)
            ck(False, f"应报错：{why}")
        except ValueError:
            ck(True, "")


def test_replace_points_is_all_or_nothing():
    """有一行错就整批不落库：半套点位比没有更危险。"""
    alerts_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "alerts.db")
    alerts_store.init()
    r = alerts_store.replace_points(["600519 1680 1700 1850 1880 1600", "60051 错"])
    ck(r["ok"] is False and r["count"] == 0, f"有错行不该落库: {r}")
    ck(alerts_store.points() == [], "失败后库里应干净")
    ok = alerts_store.replace_points(["600519 1680 1700 1850 1880 1600 等回踩", "# 注释行"])
    ck(ok["ok"] and ok["count"] == 1, f"正常应落一行: {ok}")
    ck(alerts_store.format_points() == ["600519 1680 1700 1850 1880 1600 等回踩"],
       f"反解要与输入一致: {alerts_store.format_points()}")


def test_replace_targets_multi_phone():
    """一个账号多台手机：整表替换，渠道与地址都要留住。"""
    alerts_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "alerts.db")
    alerts_store.init()
    r = alerts_store.replace_targets([
        "bark mykey1 我的 iPhone",
        "wecom https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc 家用群",
        "log",
    ])
    ck(r["ok"] and r["count"] == 3, f"三条目标应全落: {r}")
    ck(alerts_store.status()["targets"] == 3, "目标数应记到 status")
    lines = alerts_store.format_targets()
    ck(lines[0] == "bark mykey1 我的 iPhone", f"反解丢失信息: {lines}")
    bad = alerts_store.replace_targets(["bark"])
    ck(bad["ok"] is False and alerts_store.status()["targets"] == 3,
       f"缺地址应整批拒绝且不动原配置: {bad}")


# ── 触发算术 ──────────────────────────────────────────────────────────────
def test_hit_inside_and_approach():
    row = {"buy_lo": 10.0, "buy_hi": 11.0, "sell_lo": 20.0, "sell_hi": 21.0, "stop": 9.0}
    ck(alerts._hit(row, 10.5)[0] == "buy", "区间内应触发买点")
    ck("已进入" in alerts._hit(row, 10.5)[1], "区间内措辞应是已进入")
    ck(alerts._hit(row, 11.05)[0] == "buy", "上沿 0.5% 内应算接近")
    ck("接近" in alerts._hit(row, 11.05)[1], "边界外措辞应是接近")
    ck(alerts._hit(row, 20.5)[0] == "sell", "卖点区间内应触发卖点")
    ck(alerts._hit(row, 9.05)[0] == "stop", "止损应触发")
    ck(alerts._hit(row, 15.0) is None, "区间的中间不该触发")


def test_stop_wins_over_buy():
    """同时摸到买点与止损时说止损（买点抄底 + 已破位是两件事，先说风险）。"""
    row = {"buy_lo": 10.0, "buy_hi": 10.5, "sell_lo": None, "sell_hi": None, "stop": 10.2}
    kind, wording = alerts._hit(row, 10.1)
    ck(kind == "stop" and "止损" in wording, f"止损应优先: {(kind, wording)}")


def test_approach_pct_is_configurable():
    row = {"buy_lo": 10.0, "buy_hi": 10.0, "sell_lo": None, "sell_hi": None, "stop": None}
    saved = alerts.APPROACH_PCT
    try:
        alerts.APPROACH_PCT = 0.0
        ck(alerts._hit(row, 10.2) is None, "0% 容差时超出区间不该触发")
        alerts.APPROACH_PCT = 5.0
        ck(alerts._hit(row, 10.2)[0] == "buy", "5% 容差时 10.2 应算接近")
    finally:
        alerts.APPROACH_PCT = saved


def test_compose_marks_source_and_disclaimer():
    row = {"code": "600519", "name": "贵州茅台", "horizon": "short", "source": "manual",
           "thesis": "回踩 20 日线"}
    title, body = alerts._compose(row, "buy", "已进入买点区间 1680.00 到 1700.00", 1690.0)
    ck("买点" in title and "600519" in title, f"标题应含类型与代码: {title}")
    ck("手动设置" in body and "不构成投资建议" in body, f"正文要标来源与免责: {body}")
    t2, _ = alerts._compose({"code": "600519", "name": "600519", "source": "manual"},
                            "buy", "已进入买点区间", 10.0)
    ck(t2 == "买点 600519", f"手设点位没名字时别写成 600519(600519): {t2}")


# ── 端到端检查 ────────────────────────────────────────────────────────────
def setup_env(public_rows, watch_rows, quotes):
    alerts_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "alerts.db")
    alerts_store.init()
    alerts_store.replace_targets(["log"])
    alerts.picks_store.init = lambda scope: None
    alerts.picks_store.latest_run = lambda scope: list(public_rows)
    alerts.picks_store.current = lambda scope, code="": list(watch_rows)
    alerts.picks_pipeline.visible_horizons = lambda: ["short"]
    alerts._market_open = lambda: True
    alerts.ds.tencent_quote = lambda codes: {c: quotes[c] for c in codes if c in quotes}
    sent = []
    alerts.notify.send_all = lambda tg, title, body: (sent.append((title, body)) or
                                                      {"sent": len(tg), "failed": 0,
                                                       "total": len(tg), "detail": []})
    return sent


def _ledger_row(code="600519", name="贵州茅台", horizon="short", **kw):
    row = {"code": code, "name": name, "horizon": horizon, "status": "open", "scope": "public",
           "created_at": "2026-01-05 16:00:00",
           "entry_lo": 10.0, "entry_hi": 10.5, "exit_lo": 20.0, "exit_hi": 21.0, "stop": 9.0,
           "thesis": "AI 依据", "valid_until": "2030-01-01"}
    row.update(kw)
    return row


def test_check_pushes_once_per_day():
    sent = setup_env([_ledger_row()], [], {"600519": {"price": 10.2}})
    r1 = alerts.check("u1")
    ck(r1["sent"] == 1 and len(sent) == 1, f"首次应推一条: {r1}")
    r2 = alerts.check("u1")
    ck(r2["sent"] == 0, f"同一天同一类不该重复推: {r2}")
    ck(len(alerts_store.sent_today()) == 1, "去重表应记一行")


def test_check_only_visible_horizons():
    """中长线在面板上还 mask 着时不能推手机（免得面板不显示、手机却响）。"""
    sent = setup_env([_ledger_row(horizon="long")], [], {"600519": {"price": 10.2}})
    r = alerts.check("u1")
    ck(r["sent"] == 0 and not sent, f"不可见周期不该触发: {r}")


def test_check_skips_rows_created_today():
    """AI 的价位当天不推（就贴着生成时的现价，推了等于复读面板），第二天起才生效。"""
    import datetime as dt
    today = dt.date.today().isoformat()
    sent = setup_env([_ledger_row(created_at=f"{today} 16:00:00")], [],
                     {"600519": {"price": 10.2}})
    r = alerts.check("u1")
    ck(r["sent"] == 0 and not sent, f"当天生成的 AI 点位不该推: {r}")
    ck(r["skipped"] == "没有有效期内的点位", f"当天生成的行应被整行过滤掉: {r}")


def test_manual_point_active_same_day():
    """手设点位不受「当天不推」约束：那是用户当场写下的意思。"""
    import datetime as dt
    today = dt.date.today().isoformat()
    setup_env([_ledger_row(created_at=f"{today} 16:00:00")], [], {"600519": {"price": 12.0}})
    alerts_store.replace_points(["600519 11.5 12.5 - - 8"])
    r = alerts.check("u1")
    ck(r["sent"] == 1 and r["hits"][0]["source"] == "manual", f"手设点位当天应生效: {r}")


def test_manual_point_overrides_ai():
    sent = setup_env([_ledger_row()], [], {"600519": {"price": 12.0}})
    ck(alerts.check("u1")["sent"] == 0, "12 元不在 AI 的买点区间内")
    alerts_store.replace_points(["600519 11.5 12.5 - - 8"])
    r = alerts.check("u1")
    ck(r["sent"] == 1 and r["hits"][0]["source"] == "manual", f"手改点位应生效: {r}")
    ck("手动设置" in sent[0][1], f"正文要说明点位来自手动: {sent[0][1]}")


def test_check_skips_without_targets_and_outside_session():
    setup_env([_ledger_row()], [], {"600519": {"price": 10.2}})
    alerts_store.replace_targets([])
    ck(alerts.check("u1")["skipped"] == "还没有配置提醒目标", "没配目标应跳过并说明")
    alerts_store.replace_targets(["log"])
    alerts._market_open = lambda: False
    ck(alerts.check("u1")["skipped"] == "非交易时段", "非交易时段应跳过")
    r = alerts.check("u1", force=True)
    ck(r["sent"] == 1, f"force 应绕过时段: {r}")


def test_dry_run_does_not_send_or_dedupe():
    sent = setup_env([_ledger_row()], [], {"600519": {"price": 10.2}})
    r = alerts.check("u1", dry_run=True)
    ck(r["sent"] == 1 and not sent, "dry_run 只报告不发送")
    ck(alerts_store.sent_today() == {}, "dry_run 不写去重表")


# ── 发送层 ────────────────────────────────────────────────────────────────
def test_notify_log_channel_and_unknown():
    ok, msg = notify.send("log", "", "t", "b")
    ck(ok and msg == "log", f"log 渠道应成功: {(ok, msg)}")
    ok2, msg2 = notify.send("telegram", "x", "t", "b")
    ck(not ok2 and "未知渠道" in msg2, f"未知渠道按失败处理、不猜: {(ok2, msg2)}")


def test_notify_payloads_per_channel():
    calls = []
    notify._post = lambda url, payload: (calls.append((url, payload)) or (True, "200 ok"))
    notify.send("bark", "mykey", "标题", "正文")
    ck(calls[-1][0] == "https://api.day.app/push", f"Bark 官方地址: {calls[-1][0]}")
    ck(calls[-1][1]["device_key"] == "mykey" and calls[-1][1]["group"], "Bark 载荷应带 key 与分组")
    notify.send("bark", "https://bark.example.com|k2", "t", "b")
    ck(calls[-1][0] == "https://bark.example.com/push", f"自建 Bark 地址要生效: {calls[-1][0]}")
    notify.send("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "标题", "正文")
    ck(calls[-1][1]["msgtype"] == "text", f"企业微信载荷: {calls[-1][1]}")
    notify.send("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=x|SEC", "标题", "正文")
    ck("timestamp=" in calls[-1][0] and "sign=" in calls[-1][0], f"加签应拼到 URL: {calls[-1][0]}")


def test_infer_channel_from_address():
    """只贴地址也能认出来（用户嫌填渠道繁琐）。裸串按 ntfy 主题处理。"""
    cases = [("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "wecom"),
             ("https://oapi.dingtalk.com/robot/send?access_token=x", "dingtalk"),
             ("ntfy.sh/astock-abc123", "ntfy"),
             ("https://ntfy.sh/astock-abc123", "ntfy"),
             ("https://api.day.app/mykey", "bark"),
             ("bark:mykey", "bark"),
             ("astock-7k3f9q2x", "ntfy"),      # 页面「生成主题」就是这种形状
             ("mytopic", ""),                   # 纯英文单词不当主题：宁可报错，别静默失效
             ("telegram", ""),                  # 打错的渠道名尤其不能当成主题收下
             ("barkk", "")]
    for addr, want in cases:
        ck(alerts_store.infer_target(addr)[0] == want, f"{addr} 应认成 {want}")
    ck(alerts_store.infer_target("随便写的一句话 这里有空格")[0] == "",
       "认不出要给空、不能瞎猜")


def test_replace_targets_accepts_bare_address():
    """一行只写地址也能存：识别成功就落库，识别失败整批拒绝并说明怎么改。"""
    alerts_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "alerts.db")
    alerts_store.init()
    r = alerts_store.replace_targets(["astock-abc123 我的手机",
                                      "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x 家用群"])
    ck(r["ok"] and r["count"] == 2, f"只写地址应能存: {r}")
    got = alerts_store.targets()
    ck({t["channel"] for t in got} == {"ntfy", "wecom"}, f"渠道应自动识别: {got}")
    ck(got[0]["label"] == "我的手机", "备注要留住")
    bad = alerts_store.replace_targets(["这不是地址 有空格且认不出"])
    ck(bad["ok"] is False and "认不出" in bad["errors"][0], f"认不出要报错: {bad}")


def test_ntfy_payload():
    """ntfy 用 JSON POST 到服务器根：中文标题走 body，不能塞 HTTP 头（latin-1 会乱码）。"""
    calls = []
    notify._post = lambda url, payload: (calls.append((url, payload)) or (True, "200"))
    notify.send("ntfy", "astock-abc", "买点 贵州茅台", "现价 1690")
    ck(calls[-1][0] == "https://ntfy.sh", f"公共 ntfy 应 POST 到根: {calls[-1][0]}")
    ck(calls[-1][1]["topic"] == "astock-abc" and calls[-1][1]["message"] == "现价 1690",
       f"载荷应带主题与正文: {calls[-1][1]}")
    ck(calls[-1][1]["title"] == "买点 贵州茅台", "中文标题要留在 JSON 里")
    notify.send("ntfy", "https://ntfy.mydomain.com/astock-abc", "t", "b")
    ck(calls[-1][0] == "https://ntfy.mydomain.com" and calls[-1][1]["topic"] == "astock-abc",
       f"自建 ntfy 要拆地址与主题: {calls[-1]}")


def test_bark_accepts_app_url():
    """Bark App 里复制出来的 https://api.day.app/<key> 必须能直接粘（用户就是这么配的）。"""
    calls = []
    notify._post = lambda url, payload, ok_field="", ok_value=200: (
        calls.append((url, payload)) or (True, "200"))
    notify.send("bark", "mykey", "t", "b")
    ck(calls[-1][0] == "https://api.day.app/push" and calls[-1][1]["device_key"] == "mykey",
       f"裸 key 要能发: {calls[-1]}")
    notify.send("bark", "https://api.day.app/AbCd1234", "t", "b")
    ck(calls[-1][0] == "https://api.day.app/push" and calls[-1][1]["device_key"] == "AbCd1234",
       f"粘官方 URL 要能发: {calls[-1]}")
    notify.send("bark", "https://bark.mydomain.com/K3y", "t", "b")
    ck(calls[-1][0] == "https://bark.mydomain.com/push" and calls[-1][1]["device_key"] == "K3y",
       f"自建 URL 要能发: {calls[-1]}")
    notify.send("bark", "https://api.day.app/k1|k2", "t", "b")
    ck(calls[-1][1]["device_key"] == "k2", f"地址|key 的老写法仍要支持: {calls[-1]}")
    ok, msg = notify.send("bark", "https://api.day.app/", "t", "b")
    ck(not ok and "没有 key" in msg, f"没有 key 要报错而不是瞎猜: {(ok, msg)}")


def test_pushplus_judges_business_code():
    """pushplus 的 HTTP 一律 200、成败在 body 的 code 里，不能只看 HTTP 状态。"""
    calls = []
    notify._post = lambda url, payload, ok_field="", ok_value=200: (
        calls.append((url, payload, ok_field)) or (ok_field == "code", "stub"))
    ok, _ = notify.send("pushplus", "mytoken", "标题", "正文")
    ck(calls[-1][0] == notify.PUSHPLUS_URL and calls[-1][1]["token"] == "mytoken",
       f"pushplus 载荷不对: {calls[-1]}")
    ck(calls[-1][1]["template"] == "txt", "pushplus 用 txt 模板")
    ck(calls[-1][2] == "code", "必须按 body 的 code 判成败")
    ck(ok is True, "stub 返回成功")
    ok2, msg2 = notify.send("pushplus", "", "t", "b")
    ck(not ok2 and "缺 token" in msg2, f"没 token 要说清楚: {(ok2, msg2)}")


def test_post_reads_business_code():
    """_post 的 ok_field 分支：HTTP 200 但 body code 不是 200 → 判失败。"""
    notify._post = _REAL_POST        # 还原真实现（同文件里别的用例会替换它）
    class FakeResp:
        status = 200
        def read(self): return b'{"code":401,"msg":"token error"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    orig = notify.urllib.request.urlopen
    notify.urllib.request.urlopen = lambda req, timeout=10: FakeResp()
    try:
        ok, msg = notify._post("https://example.invalid", {}, ok_field="code")
        ck(ok is False and "401" in msg, f"业务码 401 应判失败: {(ok, msg)}")
        ok2, _ = notify._post("https://example.invalid", {})
        ck(ok2 is True, "不看业务码时 HTTP 200 仍算成功")
    finally:
        notify.urllib.request.urlopen = orig


def test_notify_test_summarises_failure_reason():
    """发测试失败时要把原因汇总到 error：只读 error 的客户端也能知道为什么（真事：pushplus 要实名）。"""
    alerts_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "alerts.db")
    alerts_store.init()
    alerts_store.replace_targets(["pushplus 0000000000000000000000000000dead 我的微信"])
    notify.send_all = lambda tg, title, body: {
        "sent": 0, "failed": 1, "total": 1,
        "detail": [{"label": "我的微信", "channel": "pushplus", "ok": False,
                    "msg": '200 {"code":905,"msg":"账户未进行实名认证"}'}]}
    try:
        r = alerts.notify_test()
        ck(r["ok"] is False, "一台都没发出时 ok 必须是 False")
        ck("实名" in (r.get("error") or ""), f"error 要带上真实原因: {r.get('error')}")
        ck("我的微信" in (r.get("error") or ""), "error 要指明是哪台")
    finally:
        notify.send_all = _REAL_SEND_ALL


def test_send_all_isolates_failures():
    notify.send_all = _REAL_SEND_ALL        # 还原真实现（前面的用例把它换成了假的）

    def fake(channel, address, title, body):
        return (address != "bad"), "stub"
    notify.send = fake
    r = notify.send_all([{"channel": "log", "address": "ok", "label": "A"},
                         {"channel": "log", "address": "bad", "label": "B"}], "t", "b")
    ck(r["sent"] == 1 and r["failed"] == 1 and r["total"] == 2, f"一台失败不拖累另一台: {r}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过（{N[0]} 断言）")
    sys.exit(1 if failed else 0)
