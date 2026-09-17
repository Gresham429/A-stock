"""选股与形态初筛（从 app.py 抽出，2026-07-16 拆分）。

**为什么独立成模块**：这组函数既被 `app.py` 的 `/api/recommend/screen` 路由用，也被
`agent_loop` 的选股步用。原先都塞在 app.py 里，agent_loop 只能 `import app` 反向依赖
（循环）。抽出后 agent_loop 直接 `import screening`，循环依赖消除，app.py 也瘦身。

**只含纯逻辑，不含 Flask/路由**——可离线测（`tests/test_screen_branches.py`）。

职责：全市场/板块候选池 → 行情+指标 → 形态初筛打分 `_pa_score`（方向由
`factor_lab` 的 IC 回测驱动）→ 均衡采样 `_balanced_pick`。三条互斥分支见
`_screen_rows`（改一带必三条都测，PITFALLS#10）。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import cap_layers
import datasources as ds
import factor_lab
import universe
import universe_store

logger = logging.getLogger(__name__)

_SCREEN_CAP_TOTAL = 36  # 喂给 LLM 的候选总量上限（控 token 与时延）；全市场路径三层各分 12
_LAYER_DEPTH = 48       # 每层进打分的候选深度（成本界：全市场 3800 只逐只算形态要八分钟）
VOL_FLOOR = 15.0        # 波动率下限：低于此没有波段空间（用户偏好，非收益预测）
VOL_CEIL = 130.0        # 波动率上限：高于此风险失控

_PA_RANK_MAX = 200   # 板块内 ≤ 该只数时，对全部成分股算形态再排序（超过则退回市值预筛）
_METRIC_TTL = 900    # 形态指标进程内缓存秒数（同板块反复选股不重复取数）
_metric_cache: dict[str, tuple[float, dict]] = {}
_EMPTY_METRICS = {"vol": None, "range_pos": None, "cum20": None,
                  "net5": None, "net20": None, "series": []}


def _safe_metrics(code: str) -> dict:
    """sina_metrics 的兜底包装 + 进程内 TTL 缓存：单只异常不拖垮整批。

    全市场池数据质量参差（0 收盘价、空字段、退市残留），executor.map 里任一只抛异常
    都会让整个选股 500。指标缺失退化为 None，AI 侧本就按缺失处理。
    """
    hit = _metric_cache.get(code)
    if hit and time.time() - hit[0] < _METRIC_TTL:
        return hit[1]
    try:
        m = ds.sina_metrics(code)
    except Exception as e:  # noqa: BLE001 兜底：宁可该股无指标，不可整批失败
        logger.warning("指标计算失败 %s（跳过该股指标）: %s", code, e)
        return dict(_EMPTY_METRICS)
    _metric_cache[code] = (time.time(), m)
    return m


_KLINE_TTL = 900     # 日K进程内缓存秒数（12 个 agent 候选高度重叠，不缓存要打 240 次请求）
_kline_cache: dict[tuple[str, int], tuple[float, list]] = {}


def _safe_kline(code: str, num: int = 70) -> list[dict]:
    """sina_kline 的兜底包装 + 进程内 TTL 缓存（mirror `_safe_metrics`）。

    num=70：MA60 要 60 根，留 10 根余量给停牌/缺口。缓存键带 num：三周期选股要 90 根算长窗口
    因子，若只按代码缓存，先来的 70 根会把后来的 90 根请求挡成短序列（长窗口因子静默缺样本）。
    全市场池数据质量参差，单只异常不得拖垮整批（PITFALLS#11）。
    """
    hit = _kline_cache.get((code, num))
    if hit and time.time() - hit[0] < _KLINE_TTL:
        return hit[1]
    try:
        k = ds.sina_kline(code, num=num)
    except Exception as e:  # noqa: BLE001 兜底：宁可该股无K线，不可整批失败
        logger.warning("K线获取失败 %s（该股按无结构处理）: %s", code, e)
        return []
    _kline_cache[(code, num)] = (time.time(), k)
    return k


def _pa_score(m: dict, dirs: dict | None = None) -> float | None:
    """形态初筛打分（0–100）。None = 形态不可分析，该股出局。

    `dirs`：因子方向字典（调用方一次算好传入，省每股一次 DB 读）。为空则取**全池**方向
    （向后兼容）。无 focus 全市场路径应传 `scoring_directions('large')`——打分对象是预筛后的
    大盘池、方向可与全池相反（见 factor_lab / PITFALLS#5b）。

    这是**粗筛**，只决定谁值得占用送进 AI 的 36 个名额；真正的 PA 判断由 AI 依
    rules_store 的规则库做。

    **方向由 162,014 个样本的 IC 回测驱动，不再是我拍的先验**（见 factor_lab）：
      · 每个因子的方向取 `factor_lab.direction()` —— 近 60 日 |t|>2 用近期方向
        （regime 已切换），否则用全样本方向，两者都不显著则该因子**不参与打分**。
      · 权重保持等权（每个生效因子等分）——量化实证里过度优化的权重样本外
        常打不过等权，且频繁重拟合会让噪音驱动参数、在 regime 间来回甩。
      · 实测：全样本三因子皆反向(cum20 t=-8.16 反转效应)，但近 60 日全部符号反转
        (vol t=+6.96)，A股 2026 年从反转切向动量。静态权重会持续押错方向。

    **波动率的双重身份**：它既是收益预测因子（低波动异象），又是用户的风险偏好
    （要波动型科技股才有波段空间）。二者混淆会打架，故拆开——
      打分：按 IC 方向（预测）；过滤：VOL_FLOOR/CEIL 硬门（偏好与风控）。

    资金分量(net20)因 `sina_metrics` 只给 30 天历史，**无法回测方向**，故不打分、
    仅作展示。
    """
    vol = m.get("vol")
    if vol is None:  # 形态算不出来 -> 不进候选（次新/停牌/数据缺口）
        return None
    if not (VOL_FLOOR <= vol <= VOL_CEIL):  # 偏好+风控硬门，与预测无关
        return None
    if dirs is None:
        dirs = factor_lab.directions()
    live = [f for f in ("vol", "cum20", "range_pos")
            if dirs.get(f, {}).get("sign", 0) != 0 and m.get(f) is not None]
    if not live:  # 没有任何因子方向可信 -> 全体中性，交给 AI 判断
        return 50.0
    per = 100.0 / len(live)
    score = 0.0
    for f in live:
        sign = dirs[f]["sign"]
        pct = _factor_pct(f, m[f])          # 该值在历史分布中的位置 0..1
        score += per * (pct if sign > 0 else (1.0 - pct))
    return round(score, 1)


# 因子取值的分位锚点（把原始值映射到 0..1，避免量纲差异主导打分）。
# 2026-09-17 重测（`factor_lab.sample_codes(160)` 市值分层抽样 × 600 日 = 86,797 个观测，
# 脚本 `.claude/docs-tidy/factor_range_check.py`），实测 p2.5 / p97.5：
#   vol 15.1/105.9 · cum5 −12.6/18.2 · cum20 −23.1/39.0 · cum60 −31.4/79.3 · cum120 −36.0/133.8
#   观测数：cum60 81,197、cum120 71,650（窗口不足的日子按列缺样本）
# 锚点取得比实测分位略宽，避免两端饱和后失去区分度。换抽样或换市场环境要重跑核对。
_FACTOR_RANGE = {"vol": (15.0, 110.0), "cum5": (-15.0, 20.0), "cum20": (-25.0, 40.0),
                 "cum60": (-35.0, 85.0), "cum120": (-40.0, 140.0), "range_pos": (0.0, 100.0)}


def _factor_pct(f: str, v: float) -> float:
    lo, hi = _FACTOR_RANGE[f]
    return min(max((v - lo) / (hi - lo), 0.0), 1.0) if hi > lo else 0.5


def _balanced_pick(codes: list[str], cap_total: int, cap_per_sub: int,
                   smap: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """跨一级板块均衡采样：按一级轮询取，每个细分最多 cap_per_sub 只，总量 ≤ cap_total。

    smap 为批量板块映射（全市场池 ~5000 只逐只查 DB 会退化，必须批量传入）；
    不传则回退到手工池的内存查询，保持旧行为。

    **只在全市场路径用**（focus 为空、或板块池 >_PA_RANK_MAX）；关注板块时走
    形态排序分支，不经过这里。两条分支必须分别测——本函数曾因只测了形态分支
    而被误删都没发现（NameError 直到 agent 跑全市场才炸）。
    """
    look = (lambda c: smap[c]) if smap else universe.sector_of
    by_primary: dict[str, list[str]] = {}
    for c in codes:
        primary, _ = look(c)
        by_primary.setdefault(primary, []).append(c)
    queues = list(by_primary.values())
    cursor = [0] * len(queues)
    per_sub: dict[str, int] = {}
    picked: list[str] = []
    progressed = True
    while len(picked) < cap_total and progressed:
        progressed = False
        for qi, q in enumerate(queues):
            while cursor[qi] < len(q):
                c = q[cursor[qi]]
                cursor[qi] += 1
                _, sub = look(c)
                if per_sub.get(sub, 0) < cap_per_sub:
                    per_sub[sub] = per_sub.get(sub, 0) + 1
                    picked.append(c)
                    progressed = True
                    break  # 取一只后轮到下一个一级板块
            if len(picked) >= cap_total:
                break
    return picked


def _stride(codes: list[str], n: int) -> list[str]:
    """按等距抽样取 n 只（不足则全取）。跨层等距，避免只取每层市值头部。"""
    if len(codes) <= n:
        return list(codes)
    step = len(codes) / n
    return [codes[int(i * step)] for i in range(n)]


def _screen_rows(capital: float, focus: str = "") -> list[dict]:
    """候选池行情 + 指标 + 分数（三层各留名额；指定板块时退化为板块内排序）。

    硬门（先过滤掉不能买或不该买的）：1 手买得起、非 ST/停牌/北交所（`codes_of` 的 eligible
    已排除后三者）、流通市值 ≥ `cap_layers.SMALL_YI`（30 亿，进出太薄的股不参与）。

    全市场路径（focus 为空）按流通市值分三层，每层等距抽 `_LAYER_DEPTH` 只算形态与分数，
    再用 `cap_layers.select` 给每层留 12 个名额（同申万二级最多 2 只）。**这一步替代了原先
    「市值前 600 再板块均衡抽 36」**——那个 600 硬上限把 88% 的市场挡在门外，且分数不决定谁
    进候选（设计第二节问题 1、2）。等距而不是取头部：只取每层市值头部等于在「大市值内部再排
    一次」，分层就白做了。

    focus 指定板块时不强制分层（板块内层分布本来就偏，银行全是大盘），按分数取前 36 只；
    板块成分股超过 `_PA_RANK_MAX` 时退回板块内均衡采样。三条分支互斥，改一带必三条都测。
    """
    codes = universe_store.codes_of(focus)
    quotes = ds.tencent_quote(codes)  # 自动分批：全池 4989 只 ≈1.7s
    if not quotes:
        return []
    # 硬门一：按 1 手成本可负担过滤（资金太小买不起任何 1 手则退回全池给参考）
    affordable = [c for c in codes if quotes.get(c, {}).get("lot_cost", 9e9) <= capital]
    pool = affordable or codes
    # 硬门二：流通市值 ≥ 30 亿（`layer_of` 对 30 亿以下返回 None）。板块路径同样适用。
    mcap_yi = {c: (quotes.get(c) or {}).get("float_mcap_yi") for c in pool}
    pool = [c for c in pool if cap_layers.layer_of(mcap_yi.get(c))]
    if not pool:
        return []
    smap = universe_store.sectors_map(pool)  # 批量查板块，避免逐只 DB 往返
    subs_all = {s for subs in universe_store.taxonomy().values() for s in subs}
    if focus and len(pool) <= _PA_RANK_MAX:
        # 池子够小（关注某板块，主路径）-> 对全部成分股算形态再按分排序，形态真正参与筛选。
        metrics = _metrics_of(pool)
        dirs_full = factor_lab.directions()   # focus=某板块：成分股混市值，用全池方向
        scored = [(c, metrics[c], _pa_score(metrics[c], dirs_full)) for c in pool]
        keep = [(c, m, s) for c, m, s in scored if s is not None]
        keep.sort(key=lambda x: x[2], reverse=True)
        chosen = keep[:_SCREEN_CAP_TOTAL]
        logger.info("形态初筛 focus=%s: 池 %d -> 可分析 %d -> 取前 %d",
                    focus, len(pool), len(keep), len(chosen))
        picked = [c for c, _, _ in chosen]
        mmap = {c: m for c, m, _ in chosen}
        score_map = {c: s for c, _, s in chosen}
    elif focus:
        # 大板块（成分股 >200）：分层名额不适用（板块内部层分布偏），退回板块内均衡采样。
        cap_per_sub = 6 if focus in subs_all else 3
        picked = _balanced_pick(pool, _SCREEN_CAP_TOTAL, cap_per_sub, smap)
        mmap = _metrics_of(picked)
        score_map = {c: _pa_score(mmap[c], factor_lab.directions()) for c in picked}
    else:
        # 全市场：分三层、每层等距抽候选、按**该层**的因子方向打分、逐层给名额。
        by_layer: dict[str, list[str]] = {l: [] for l in cap_layers.LAYERS}
        for c in sorted(pool, key=lambda x: mcap_yi.get(x) or 0, reverse=True):
            by_layer[cap_layers.layer_of(mcap_yi.get(c))].append(c)
        cand = [c for l in cap_layers.LAYERS for c in _stride(by_layer[l], _LAYER_DEPTH)]
        metrics = _metrics_of(cand)
        per_layer = max(1, _SCREEN_CAP_TOTAL // len(cap_layers.LAYERS))
        scored_rows = []
        for l in cap_layers.LAYERS:
            dirs = factor_lab.scoring_directions(f"layer_{l}")   # 层可用数据不足时自动回退全池
            for c in cand:
                if cap_layers.layer_of(mcap_yi.get(c)) != l:
                    continue
                s = _pa_score(metrics[c], dirs)
                if s is None:
                    continue
                primary, sub = smap.get(c) or universe_store.sector_of(c)
                scored_rows.append({"code": c, "mcap_yi": mcap_yi.get(c), "sub": sub,
                                    "primary": primary, "score": s})
        scored_rows.sort(key=lambda r: r["score"], reverse=True)
        picked = cap_layers.select(scored_rows, per_layer=per_layer)
        logger.info("全市场分层初筛：池 %d（分层 %s）-> 候选 %d -> 取 %d（每层 %d，层内同二级 ≤%d）",
                    len(pool), cap_layers.layer_counts(
                        [{"mcap_yi": v} for v in mcap_yi.values()]),
                    len(cand), len(picked), per_layer, cap_layers.SUB_CAP)
        mmap = {c: metrics[c] for c in picked}
        score_map = {r["code"]: r["score"] for r in scored_rows}
    rows = []
    for c in picked:
        q, m = quotes.get(c, {}), mmap.get(c, _EMPTY_METRICS)
        primary, sub = smap.get(c) or universe_store.sector_of(c)
        rows.append({"code": c, "name": q.get("name", c),
                     "primary": primary, "sub": sub,
                     "price": q.get("price"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                     "vol": m.get("vol"), "cum20": m.get("cum20"),
                     "range_pos": m.get("range_pos"), "net20": m.get("net20"),
                     "pa_score": score_map.get(c),
                     "turnover": q.get("turnover"),
                     "lot_cost": q.get("lot_cost")})
    return rows


def _metrics_of(codes: list[str]) -> dict[str, dict]:
    """并发拉一批股票的形态指标（带进程内 TTL 缓存）。"""
    if not codes:
        return {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        return dict(zip(codes, executor.map(_safe_metrics, codes)))
