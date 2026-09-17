"""复盘 AI 整块降级：不落占位、可识别、可重试（零依赖离线，不打网络/不调 LLM）。

**为什么有这个文件**：预算门拒绝或网络故障时，五路分析师各自降级成一行 stub 文本，
而 `pipeline` 原来的判据是 `if focus or art or analysts`——analysts 是「每角色一条」的
非空列表，于是**全失败也算有 AI**：一份只有占位文本的复盘被当成 done 落盘，当天到点调度
再也不会重跑，第二天你看到的是「昨天生成成功但全是失败占位」。

修法：
- `llm.LLMError` 带 kind（budget 表示今天重试也没用）；
- 分析师条目带 failed 标记，pipeline 据此区分「整块降级」与「只是某个角色缺席」；
- 整块降级时 ai 记 None、envelope 打 ai_degraded / ai_error / ai_error_kind，硬指标照常保留；
- 自动重试只针对可重试类（非 budget），且有 20 分钟最小间隔。

跑法：python3 tests/test_review_degrade.py
"""
import datetime
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm  # noqa: E402
import ratelimit  # noqa: E402
from review import llm_review as LR  # noqa: E402
from review import pipeline as P  # noqa: E402
from review import store  # noqa: E402

TMP = tempfile.mkdtemp()
store.REVIEW_DIR = TMP
store._LATEST = os.path.join(TMP, "latest.json")      # 这三个常量都是 import 时绑定的，测试要一起改
store.HISTORY_FILE = os.path.join(TMP, "history.json")
D = "20260916"
_n = 0


def ck(cond, msg):
    global _n
    _n += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


@contextmanager
def patched(mapping: dict):
    """按点号路径临时替换模块属性，退出时倒序还原。"""
    saved = []
    try:
        for dotted, value in mapping.items():
            parts = dotted.split(".")
            obj = globals()[parts[0]]
            for p in parts[1:-1]:
                obj = getattr(obj, p)
            saved.append((obj, parts[-1], getattr(obj, parts[-1])))
            setattr(obj, parts[-1], value)
        yield
    finally:
        for obj, attr, old in reversed(saved):
            setattr(obj, attr, old)


def _stub_fetch():
    return {
        "P.fetch.resolve_trade_date": lambda d=None: D,
        "P.fetch.to_dash": lambda d: "2026-09-16",
        "P.fetch.zt_pool": lambda d: [{"code": "600000", "name": "x", "limit_days": 2}],
        "P.fetch.zb_pool": lambda d: [],
        "P.fetch.dt_pool": lambda d: [],
        "P.fetch.yzt_pool": lambda d: [],
        "P.fetch.theme_reasons": lambda d: [],
        "P.fetch.dragon_tiger": lambda d: [],
        "P.fetch.sector_flow": lambda *a, **k: [],
        "P.metrics.breadth": lambda zt, zb, dt: {"zt_count": len(zt), "max_height": 2,
                                                              "break_rate": 0.1},
        "P.metrics.compute_all": lambda *a, **k: {"breadth": {"zt_count": 3}},
        "P.config.llm_enabled": lambda: True,
    }


def test_budget_error_carries_kind():
    """预算门拒绝必须标成 kind=budget，调用方才能决定「今天别再试」。"""
    with patched({"ratelimit.allow_llm": lambda uid: (False, "今天全站 AI 调用已达上限 150 次")}):
        try:
            llm._check_budget()
            ck(False, "预算门拒绝时 _check_budget 应抛 LLMError")
        except llm.LLMError as e:
            ck(getattr(e, "kind", "") == "budget", f"预算类错误应带 kind=budget: {e!r}")


def test_analyst_failure_is_marked():
    """单个分析师失败要留 failed 标记与错误类型，且 stub 文本不带装饰符号。"""
    def boom(*a, **k):
        raise llm.LLMError("模拟网络失败", kind="api")

    with patched({"LR.config.llm_enabled": lambda: True,
                  "LR.llm._chat": boom}):
        reports = LR.run_analysts({"breadth": {"zt_count": 1}}, {"zt": 1}, D)
    ck(len(reports) >= 4, f"应产出全部角色的条目: {len(reports)}")
    ck(all(r.get("failed") for r in reports), "每个角色都该被标成 failed")
    ck(all(r.get("error_kind") == "api" for r in reports), "错误类型应透传")
    ck(all("⚠️" not in r["report"] for r in reports), "运行时 stub 文本不该带装饰符号")


def test_degraded_run_keeps_metrics_and_marks():
    """整块降级：硬指标照留，ai 记 None 并打标记，不能伪装成一份完整复盘。"""
    with patched({**_stub_fetch(),
                  "P.llm_review.run_analysts": lambda *a, **k: [
                      {"key": "sentiment", "title": "情绪面", "report": "[情绪面分析失败：额度]",
                       "failed": True, "error_kind": "budget"}],
                  "P.llm_review.judge": lambda *a, **k: None,
                  "P.llm_review.article": lambda *a, **k: None}):
        r = P.run_review(D)
    ck(r["status"] == "done", f"硬指标有效仍应落盘: {r.get('status')}")
    ck(r.get("degraded") is True, f"应标记降级: {r}")
    ck(r.get("error_kind") == "budget", f"错误类型应透传: {r}")
    env = store.load(D)
    ck(env is not None, "存档应存在")
    ck(env.get("ai") is None, "整块降级时 ai 必须是 None，不能落占位文本")
    ck(env.get("ai_degraded") is True, "envelope 应带 ai_degraded")
    ck(bool((env.get("metrics") or {}).get("breadth")), "硬指标必须保留")


def test_retry_gate_rules():
    """重试门槛：预算类不重试，可重试类要过最小间隔，没降级不重试。"""
    fresh = (datetime.datetime.now() - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.datetime.now() - datetime.timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    ck(P._ai_retry_due({"ai_degraded": True, "ai_error_kind": "budget",
                        "generated_at": old}, True) is False, "预算类当天不重试")
    ck(P._ai_retry_due({"ai_degraded": True, "ai_error_kind": "api",
                        "generated_at": fresh}, True) is False, "刚失败过要等最小间隔")
    ck(P._ai_retry_due({"ai_degraded": True, "ai_error_kind": "api",
                        "generated_at": old}, True) is True, "可重试类且过了间隔应重试")
    ck(P._ai_retry_due({"ai_degraded": True, "ai_error_kind": "api",
                        "generated_at": old}, False) is False, "本次不带 AI 就不重试")
    ck(P._ai_retry_due({}, True) is False, "没降级不重试")


def test_transient_degrade_is_retried_end_to_end():
    """可重试的降级存档不该被当成 already 短路：下一次运行要能补齐 AI。"""
    old = (datetime.datetime.now() - datetime.timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    store.save({"target_date": D, "ai": None, "ai_degraded": True, "ai_error_kind": "api",
                "generated_at": old, "metrics": {"breadth": {"zt_count": 3}}})
    ok_report = [{"key": "sentiment", "title": "情绪面", "report": "有观点", "failed": False,
                  "error_kind": ""}]
    with patched({**_stub_fetch(),
                  "P.llm_review.run_analysts": lambda *a, **k: ok_report,
                  "P.llm_review.judge": lambda *a, **k: {"emotion_phase": "修复"},
                  "P.llm_review.article": lambda *a, **k: "文稿"}):
        r = P.run_review(D)
    ck(r["status"] == "done", f"应重跑而不是 already: {r}")
    ck(r.get("degraded") is False, f"这次不该再是降级: {r}")
    env = store.load(D)
    ck(env.get("ai") is not None, "补齐后 ai 应有内容")
    ck(env.get("ai_degraded") is False, "补齐后应清掉降级标记")


def test_budget_degrade_is_not_retried_end_to_end():
    """预算类降级：下一次运行直接 already，不再重取一轮数据。"""
    old = (datetime.datetime.now() - datetime.timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    store.save({"target_date": D, "ai": None, "ai_degraded": True, "ai_error_kind": "budget",
                "generated_at": old, "metrics": {"breadth": {"zt_count": 3}}})
    with patched(_stub_fetch()):
        r = P.run_review(D)
    ck(r["status"] == "already", f"预算类当天不该重跑: {r}")


if __name__ == "__main__":
    for fn in (test_budget_error_carries_kind, test_analyst_failure_is_marked,
               test_degraded_run_keeps_metrics_and_marks, test_retry_gate_rules,
               test_transient_degrade_is_retried_end_to_end,
               test_budget_degrade_is_not_retried_end_to_end):
        fn()
    print(f"OK — test_review_degrade 全过（{_n} 断言）")
