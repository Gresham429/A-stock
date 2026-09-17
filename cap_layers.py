"""流通市值分层与名额（设计 `plan/2026-09-17-screening-factor-refactor-design.md` 第四节）。

**为什么单独成模块**：用户面全市场初筛（`screening`）与三周期候选池（`picks_pipeline`）都要按
大盘 / 中盘 / 小盘分层并给每层留名额，`factor_lab` 还要用同一套边界算分层 IC。边界放 `screening`
会让 `factor_lab` 反向依赖它（`screening` 已经 import `factor_lab`），放 `universe_store` 则把
选股口径混进池子存储。这里零依赖、零网络，只吃一个流通市值数字（亿元）。

边界按 2026-09-17 的全池快照定（5004 只 eligible）：大盘 356 只、中盘 1265、小盘 2250、
30 亿以下 1133。30 亿以下不纳入是因为成交太薄，进出都难，这条是纪律参数而非收益预测。
"""
from __future__ import annotations

LARGE_YI = 500.0   # 大盘下界
MID_YI = 100.0     # 中盘下界
SMALL_YI = 30.0    # 小盘下界，也是入池门槛
LAYERS = ("large", "mid", "small")
LAYER_NAMES = {"large": "大盘", "mid": "中盘", "small": "小盘"}
PER_LAYER = 4      # 每个周期每层名额（12 只 = 三层各 4，等额，跑一个季度后按实际表现再调）
SUB_CAP = 2        # 同一申万二级行业上限，防名单集中在一个细分里


def layer_of(mcap_yi: float | None) -> str | None:
    """流通市值（亿元）-> 层名。低于 30 亿、取不到值或非正数返回 None（不纳入候选）。"""
    try:
        v = float(mcap_yi or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= LARGE_YI:
        return "large"
    if v >= MID_YI:
        return "mid"
    if v >= SMALL_YI:
        return "small"
    return None


def layer_counts(rows) -> dict[str, int]:
    """统计一批候选的层分布（含 `none` = 不纳入），用于日志与「每层都留名额」的核对。"""
    out = {l: 0 for l in LAYERS}
    out["none"] = 0
    for r in rows:
        out[layer_of(r.get("mcap_yi")) or "none"] += 1
    return out


def select(rows, per_layer: int = PER_LAYER, sub_cap: int = SUB_CAP) -> list[str]:
    """分层名额选择：`rows` 已按分数降序，每层取前 `per_layer` 只，同申万二级最多 `sub_cap` 只。

    行业上限在**每层内**计数，跨层不累计：否则小盘里某个拥挤细分会挤掉大盘名额，
    与「每层都留名额」的初衷相反。`sub` 为空视作不受限，不参与计数（归属没回填的股不该
    因为「空行业」互相挤）。

    返回按层序（大盘、中盘、小盘）排列，层内保持输入顺序（即分数序）。取不满是正常结果
    （该层候选不足或行业上限挡住），不做补齐、不跨层借名额。
    """
    buckets: dict[str, list] = {l: [] for l in LAYERS}
    for r in rows:
        l = layer_of(r.get("mcap_yi"))
        if l:
            buckets[l].append(r)
    out: list[str] = []
    for l in LAYERS:
        used: dict[str, int] = {}
        took = 0
        for r in buckets[l]:
            if took >= per_layer:
                break
            code = str(r.get("code") or "")
            if not code:
                continue
            sub = str(r.get("sub") or "")
            if sub and used.get(sub, 0) >= sub_cap:
                continue
            if sub:
                used[sub] = used.get(sub, 0) + 1
            out.append(code)
            took += 1
    return out
