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

import datasources as ds
import factor_lab
import universe
import universe_store

logger = logging.getLogger(__name__)

_SCREEN_CAP_TOTAL = 36  # 喂给 LLM 的候选总量上限（控 token 与时延）
_PRESCREEN = 600        # 全市场未指定板块时，均衡采样前先按流通市值预筛到这么多只
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
_kline_cache: dict[str, tuple[float, list]] = {}


def _safe_kline(code: str, num: int = 70) -> list[dict]:
    """sina_kline 的兜底包装 + 进程内 TTL 缓存（mirror `_safe_metrics`）。

    num=70：MA60 要 60 根，留 10 根余量给停牌/缺口。
    全市场池数据质量参差，单只异常不得拖垮整批（PITFALLS#11）。
    """
    hit = _kline_cache.get(code)
    if hit and time.time() - hit[0] < _KLINE_TTL:
        return hit[1]
    try:
        k = ds.sina_kline(code, num=num)
    except Exception as e:  # noqa: BLE001 兜底：宁可该股无K线，不可整批失败
        logger.warning("K线获取失败 %s（该股按无结构处理）: %s", code, e)
        return []
    _kline_cache[code] = (time.time(), k)
    return k


def _pa_score(m: dict) -> float | None:
    """形态初筛打分（0–100）。None = 形态不可分析，该股出局。

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
# 最初是拍的；2026-07-16 用 150 只 × 600 日 = 16,753 个观测事后核对 p5~p95：
#   vol (16.9, 88.9) / cum20 (-17.3, 27.7) / range_pos (0, 100) —— 与下方取值大致吻合。
# 若换市场环境或换抽样，应重跑核对（factor_lab.sample_codes + factors_at 逐日算即可）。
_FACTOR_RANGE = {"vol": (15.0, 110.0), "cum20": (-25.0, 35.0), "range_pos": (0.0, 100.0)}


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


def _screen_rows(capital: float, focus: str = "") -> list[dict]:
    """候选池行情 + 指标（按 focus 取数 + 负担得起优先 + 跨板块均衡采样）。

    focus 为板块名（一级/细分/概念）时只在该板块内选；为空则全市场。
    候选池来自 universe_store（全A ~4989 只 eligible），未回填时自动降级手工池。
    """
    codes = universe_store.codes_of(focus)
    quotes = ds.tencent_quote(codes)  # 自动分批：全池 4989 只 ≈1.7s
    if not quotes:
        return []
    # 先按 1 手成本可负担过滤（资金太小买不起任何 1 手则退回全池给参考）
    affordable = [c for c in codes if quotes.get(c, {}).get("lot_cost", 9e9) <= capital]
    pool = affordable or codes
    smap = universe_store.sectors_map(pool)  # 批量查板块，避免逐只 DB 往返
    subs_all = {s for subs in universe_store.taxonomy().values() for s in subs}
    # 池子够小（关注某板块，主路径）-> 对全部成分股算形态再按分排序，形态真正参与筛选。
    # 池子过大（全市场）-> 退回流通市值预筛 + 均衡采样：均衡采样按池内顺序取，
    # 5000 只不预筛会取到各板块代码号最小的股而非龙头，扩池反成选垃圾。
    if focus and len(pool) <= _PA_RANK_MAX:
        metrics = _metrics_of(pool)
        scored = [(c, metrics[c], _pa_score(metrics[c])) for c in pool]
        keep = [(c, m, s) for c, m, s in scored if s is not None]
        keep.sort(key=lambda x: x[2], reverse=True)
        chosen = keep[:_SCREEN_CAP_TOTAL]
        logger.info("形态初筛 focus=%s: 池 %d -> 可分析 %d -> 取前 %d",
                    focus, len(pool), len(keep), len(chosen))
        picked = [c for c, _, _ in chosen]
        mmap = {c: m for c, m, _ in chosen}
        score_map = {c: s for c, _, s in chosen}
    else:
        if not focus and len(pool) > _PRESCREEN:
            pool = sorted(pool, key=lambda c: quotes.get(c, {}).get("float_mcap_yi", 0),
                          reverse=True)[:_PRESCREEN]
            smap = universe_store.sectors_map(pool)
        cap_per_sub = 6 if focus and focus in subs_all else 3
        picked = _balanced_pick(pool, _SCREEN_CAP_TOTAL, cap_per_sub, smap)
        mmap = _metrics_of(picked)
        score_map = {c: _pa_score(mmap[c]) for c in picked}
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
