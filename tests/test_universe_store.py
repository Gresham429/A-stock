"""universe_store 纯逻辑回归测试（零依赖，离线）。

跑法：python3 tests/test_universe_store.py
（项目无 pytest；用 plain assert，装了 pytest 也能直接跑。）

只测不打网络的部分：板块标签解析 + 代码分类。取数/落库走冒烟测试。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import universe_store as us  # noqa: E402

# 东财 slist 真实返回样本（2026-07-15 实测）
_CASES = [
    # 带 Ⅱ/Ⅲ 后缀冲突 —— 曾导致 sw1 标记被 concept 抢占去重位而丢失
    (["银行Ⅱ", "股份制银行Ⅲ", "银行", "广东板块", "HS300_", "机构重仓"], "银行", "股份制银行"),
    (["综合Ⅱ", "综合Ⅲ", "综合", "广东板块", "超级电容", "充电桩"], "综合", "综合"),
    (["航空装备Ⅲ", "航空装备Ⅱ", "国防军工", "山东板块", "航天航空", "军工"], "国防军工", "航空装备"),
    # 无后缀、层级顺序各异
    (["电池", "锂电池", "电力设备", "福建板块", "新能源车"], "电力设备", "电池"),
    (["集成电路制造", "电子", "半导体", "AH股", "上证50_"], "电子", "集成电路制造"),
    (["铜", "有色金属", "工业金属", "福建板块"], "有色金属", "铜"),
]


def test_parse_tags_primary_and_sub():
    for tags, exp_sw1, exp_sub in _CASES:
        sw1, sub, _ = us.parse_tags(tags)
        assert (sw1, sub) == (exp_sw1, exp_sub), f"{tags[:3]} -> {sw1}/{sub}, 期望 {exp_sw1}/{exp_sub}"


def test_parse_tags_sw1_kind_not_stolen_by_suffix():
    """'银行Ⅱ' 去后缀后与 '银行' 同名，sw1 标记不能被 concept 抢占（回归）。"""
    for tags, exp_sw1, _ in _CASES:
        _, _, pairs = us.parse_tags(tags)
        kinds = dict(pairs)
        assert kinds.get(exp_sw1) == "sw1", f"{exp_sw1} 的 kind={kinds.get(exp_sw1)}，应为 sw1"


def test_parse_tags_dedup():
    for tags, _, _ in _CASES:
        _, _, pairs = us.parse_tags(tags)
        names = [s for s, _ in pairs]
        assert len(names) == len(set(names)), f"板块名重复: {names}"


def test_parse_tags_kinds():
    _, _, pairs = us.parse_tags(["银行Ⅱ", "银行", "广东板块", "HS300_", "机构重仓"])
    kinds = dict(pairs)
    assert kinds["广东板块"] == "region"
    assert kinds["HS300_"] == "index"
    assert kinds["机构重仓"] == "concept"


def test_parse_tags_empty_falls_back_to_other():
    sw1, sub, pairs = us.parse_tags([])
    assert (sw1, sub) == (us.OTHER, us.OTHER)
    assert pairs == []


def test_is_bj_covers_920_segment():
    """920xxx 是北交所新号段；ds.market_prefix 把 9 开头判为 sh，不能用它判北交所。"""
    for code in ("920000", "830799", "430047", "400001"):
        assert us.is_bj(code), f"{code} 应判为北交所"
    for code in ("600760", "000001", "300750", "688981"):
        assert not us.is_bj(code), f"{code} 不应判为北交所"


def test_board_of():
    cases = {"600760": "sh", "000001": "sz", "300750": "cyb",
             "688981": "kcb", "920000": "bj", "830799": "bj"}
    for code, exp in cases.items():
        assert us.board_of(code) == exp, f"{code} -> {us.board_of(code)}, 期望 {exp}"


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
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
