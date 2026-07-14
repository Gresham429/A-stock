"""交易规则库（SQLite，可增删改 + 启用停用）。

蒸馏自 Al Brooks 价格行为体系（PA_Agent），改写为 A股波段/持仓分析可用的框架规则。
启用中的规则由 `app._ai_web_context` 注入各 AI 分析提示词，让看板 AI 按此框架给意见。
data/rules.db（gitignore）。每条带 created_at/updated_at。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "rules.db")
_LOCK = threading.Lock()

CATEGORIES = ["总则纪律", "市场状态识别", "趋势与通道", "区间震荡",
              "突破与失败", "形态结构", "计数与结构", "K线信号", "止损止盈", "入场时机",
              "资金与周期", "风控执行", "A股制度特性"]

# 场景维度：本金档 + 周期。规则的 scenarios 标签命中当前场景(或为空=通用)才生效注入 AI。
CAPITAL_SCENARIOS = ["小", "中", "大"]
HORIZON_SCENARIOS = ["短线", "波段", "长线"]
DEFAULT_SCENARIO = "小,波段"  # 贴合用户 1 万本金画像

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, updated_at TEXT,
  category TEXT, title TEXT, content TEXT,
  enabled INTEGER DEFAULT 1, source TEXT, scenarios TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rules_cat ON rules(category);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# 部分规则的场景标签（其余为空=通用，任何场景都生效）。以标题为键。
_SCENARIO_TAGS = {
    "极速（尖峰级单向）": "短线", "Weak H1 慎做": "短线,波段",
    "H1/H2/L1/L2 计数": "短线,波段", "突破要测试": "短线,波段",
    "60/40 与盈亏比": "波段,长线", "二次入场优先": "波段,长线",
    "结构目标 Measured Move": "波段,长线",
    "小资金聚焦": "小", "大资金分批": "大", "短线快进快出": "短线",
    "波段持结构": "波段", "长线看基本面": "长线", "手续费意识": "短线",
    "打板与连板生态": "短线", "交易成本与印花税": "短线",
    "题材轮动与概念炒作": "短线,波段", "新股次新股炒作": "短线",
}

# 蒸馏种子：(分类, 标题, 要点)。改写自 PA_Agent，适配 A股波段分析。
_SEED: list[tuple[str, str, str]] = [
    ("总则纪律", "交易者方程", "只在『胜率×回报 > 败率×风险』时才动手；不满足就观望——不做也是一个合法决策。"),
    ("总则纪律", "概率管理者，不预测方向", "只评估概率分布，不断言涨跌；先判断市场处于什么状态，再选策略，最后才谈买卖。"),
    ("总则纪律", "禁止逆势", "不逆当前趋势/主方向操作；反转（MTR）只是诊断标签，不构成买入或卖出依据。"),
    ("总则纪律", "禁止追高潮", "出现衰竭信号（长尾线/小实体/反向棒）或明显买卖高潮后，禁止追原方向，只等回撤后顺势或观望。"),
    ("总则纪律", "惯性优先", "默认趋势延续；确认反转需『趋势线被突破 + 极点测试失败』双重证据，否则按延续处理。"),
    ("总则纪律", "看不懂就等", "信号不清晰、上下文矛盾时不动手，等下一根 K 线/下一个交易日给出更清楚的信号。"),

    ("市场状态识别", "先定状态再选策略", "市场位置是第一优先级：尖峰/极速/窄通道/常规通道/宽通道/趋势型区间/震荡区间/极端震荡——先归类，再路由策略。"),
    ("市场状态识别", "尖峰(Spike)", "连续同向强趋势棒、回撤极小；只顺势不追突破；连续 6 根以上或出现衰竭信号即警惕高潮，停止追单。"),
    ("市场状态识别", "通道是倾斜的区间", "窄通道更接近强趋势、宽通道更接近区间；有更高高点/更高低点不自动否定其区间属性，宽通道要按区间防假突破。"),
    ("市场状态识别", "嵌套思维", "用长程结构窗口定方向偏好，用即时信号窗口定入场时机；大中小周期一起看。"),
    ("市场状态识别", "Always In 方向", "问自己『此刻若只能持一个方向，该持多还是空』，顺这个 Always In 方向操作。"),

    ("趋势与通道", "上涨通道只做多", "上涨通道里只顺势做多（回撤、旗形、突破测试失败后进场）；禁止在通道顶部做空。下跌通道镜像做空。"),
    ("趋势与通道", "均线回撤 High1/High2", "价格回撤到 EMA20 附近、出现多头信号棒，是上涨通道里最可靠的顺势买点（H1 首次回撤、H2 二次回撤）。下跌通道对应 L1/L2。"),
    ("趋势与通道", "微型通道别硬追", "微型（极陡）通道回撤极浅，等反向假突破失败或浅回撤再顺势，别直接市价追。"),
    ("趋势与通道", "趋势末端警惕", "长趋势后出现横向重叠整理（10~20+ 根）＝最终旗形嫌疑，趋势可能衰竭，降低顺势追单意愿。"),

    ("区间震荡", "震荡区间→观望为主", "无明显方向的震荡区间：中部三分之一不交易；只在边界、且顺已判定方向时才考虑。"),
    ("区间震荡", "趋势型区间只做一侧", "trending_tr（偏多/偏空的区间）只顺方向一侧操作（偏多→下边界买）；禁止双边高抛低吸、禁止逆势。"),
    ("区间震荡", "区间里多数突破失败", "震荡区间中大多数突破会失败，不追突破（尤其不追高潮棒突破），等突破测试确认再说。"),

    ("K线信号", "好信号棒", "好信号棒：收盘接近极点、实体较大尾部短、长度不超均长 1.5 倍。十字星/长尾/超长实体都是差信号。"),
    ("K线信号", "信号需跟随确认", "信号棒之后必须有 K 线突破其极点才形成有效入场；没有跟随的信号不算高概率机会。"),
    ("K线信号", "入场质量决定交易质量", "强入场棒（大实体、收盘近极点）＝强确认；弱入场棒（小实体/十字星）风险大、宁可放弃。"),
    ("K线信号", "先上下文再形态", "同一 K 线形态在趋势/区间/突破后含义完全不同；先判上下文，再判形态，再判信号质量。"),

    ("止损止盈", "结构止损", "止损放在信号棒极点外 1 跳（宽通道/噪声大→放最近波段极点外 1 跳）；用结构位，不用固定跳数拍脑袋。"),
    ("止损止盈", "止损过大过滤器", "若结构止损超过 8 跳或信号棒高度 60%，判定止损过大→观望或放弃，别硬做。"),
    ("止损止盈", "定价顺序与 RR", "先定入场→保守目标 TP1（RR≥1）→更远结构目标 TP2→结构止损；RR<1 就收紧止损或调整入场，禁止为凑 RR 向外扩止损。"),
    ("止损止盈", "铁丝网/无交易环境", "紧密重叠、边界频繁假突破（铁丝网）时，禁止贴边界 1~2 跳止损，须更大结构缓冲，或直接观望。"),

    ("入场时机", "二次入场优先", "第一次突破/信号失败后的第二次尝试（H2/L2）通常比第一次更可靠，优先等二次入场。"),
    ("入场时机", "突破要测试", "突破后等『突破测试』（回踩不破前高/前低）再顺势，别追第一根突破棒。"),
    ("入场时机", "结构目标 Measured Move", "用等距投射估目标位：旗形/尖峰高度向外等距投射，作为止盈参考，而非拍价。"),
    ("入场时机", "MTR 只诊断不逆势", "主要趋势反转需『趋势线突破 + 更高高点/更低低点测试失败』；即便成立也只作诊断，本框架不据此逆势下单。"),

    # ── 扩充蒸馏（PA_Agent 文件13-28 + 二元决策）──
    ("总则纪律", "60/40 与盈亏比", "约 60% 交易是小盈亏相互抵消、20~30% 出大利润、10~20% 出大亏损；优势不在胜率，在于把小亏损压到≈0、让大利润远大于大亏损。"),
    ("总则纪律", "80-20 惯性法则", "区间中约 80% 突破会失败（默认不信突破）；趋势中约 80% 反转会失败（顺势突破更可信）。改变惯性需要强突破棒+跟随。"),
    ("总则纪律", "两阶段分析", "先『读懂市场』（判状态/方向/背景）再『单次决策』（给买卖倾向与价位框架）；状态没判清，不进入决策。"),

    ("市场状态识别", "通道三分类（按回撤%）", "以最近一波回撤幅度为主键：窄通道<30%（近尖峰、极强动量）/ 常规通道 30~50% / 宽通道 50~78.6%（K线重叠多、锯齿、近区间）。斜率只作辅助。"),
    ("市场状态识别", "20GB 与缺口棒", "连续约 20 根 K 线未触及 EMA20（20GB）＝极强趋势；整根 K 线在 EMA 另一侧（缺口棒）＝强趋势方向过滤，别逆势。"),
    ("市场状态识别", "极速（尖峰级单向）", "极速上涨/下跌是尖峰级单向运动：只顺势、禁止追突破(SCS)、禁止逆势；一旦出现衰竭信号即防高潮。"),

    ("趋势与通道", "常规通道只顺势", "30~50% 回撤的常规通道只做顺势，禁止在通道边界逆势刮头皮（反手/MTR 只作诊断）。"),
    ("趋势与通道", "宽通道防假突破", "50~78.6% 回撤、K线重叠多、锯齿运动的宽通道按区间处理：只顺 Always In 一侧，防边界假突破。"),

    ("突破与失败", "普通突破不追", "普通突破（1~2 根刺破关键位、影线明显、收盘未在极点、无跟随）默认不追；顺方向边界回撤优于追突破。"),
    ("突破与失败", "尖峰级突破等回踩", "尖峰级突破（3~5 根强趋势棒、收盘近极点、重叠少）也别追第一棒，等回踩/突破测试后顺势。"),
    ("突破与失败", "失败的失败", "假突破被反向打回后再次反向（失败的失败）常给出高质量顺结构机会，比追首次突破可靠。"),
    ("突破与失败", "磁力位", "失败信号极点、入场价、止损堆积区会像磁铁吸引价格回测；靠近磁力位胜率趋近 50-50，不追价、可作止盈/失效参考。"),
    ("突破与失败", "被套交易者", "突破方向被套的一方被迫止损离场会加速反向；分析时点明哪一方被套，据此判断回测/反转动能。"),
    ("突破与失败", "第一次多为陷阱", "第一次反转/突破常是机构洗盘陷阱，第一个信号经常被测试并失效；优先等第二次确认。"),

    ("形态结构", "楔形＝三推", "楔形是三推形态：价格三次同向推进、幅度递减、趋势线与通道线收敛＝动能衰减；第三推后易反转，但须突破+测试+跟随确认，别凭第三推直接反手。"),
    ("形态结构", "末端楔形只诊断", "与主趋势同向的收敛三推/末端楔形（≥20 棒）意味反转可能：禁止追顺势、也别逆势反手，观望等确认。"),
    ("形态结构", "最终旗形（FF）", "长趋势末端出现横向重叠整理（10~20+ 棒）＝最终旗形；FF 突破经常失败并反转，识别到就降低追顺势意愿。"),
    ("形态结构", "双顶双底", "两次测试相近极点、第二次未创新高/新低＝反转可能（仅诊断）；若第二次大幅创新高/新低，则只是趋势延续。"),
    ("形态结构", "三角形", "上升三角（上平下抬，偏上突破）/ 下降三角（下平上降，偏下）/ 对称三角（收敛，方向不定别提前赌）；突破须收盘+跟随确认，目标＝三角最大宽度投影。"),
    ("形态结构", "扩张三角不交易", "扩张三角（喇叭口、波动放大、边界外扩）默认不交易——噪声大、方向不明。"),
    ("形态结构", "MTR 四组件", "主要趋势反转需四要件缺一不可：①原趋势清晰 ②趋势线/通道线被收盘突破 ③趋势恢复失败 ④前极点测试失败；完整 MTR 首次成功率也仅约 35~40%。"),

    ("计数与结构", "H1/H2/L1/L2 计数", "上涨回撤中第一根突破前棒高点＝H1、第二次＝H2（下跌对应 L1/L2）；以『突破前一棒极点』的入场棒计数，每腿要对得上具体 K 线。"),
    ("计数与结构", "Weak H1 慎做", "回撤后第一根突破但实体弱/上影长＝Weak H1，除非 Always In 明确+窄通道+强入场棒，否则等 H2 再说。"),
    ("计数与结构", "H3/L3＝楔形旗", "第三次同向计数（H3/L3）常演变成楔形牛旗/熊旗（三推），别盲目当延续追单。"),
    ("计数与结构", "顺 Always In 入场", "顺 AIL/AIS 方向，即使信号不完美也可评估回撤（H1/H2）入场；逆 Always In 的首个反转只作诊断、不下单。"),

    # ── 资金与周期（场景相关，配合『当前场景』灵活启用）──
    ("资金与周期", "小资金聚焦", "本金小（如≤2万）：聚焦 1~2 手买得起的中低价股，别过度分散；单手成本必须 ≤ 可用资金。"),
    ("资金与周期", "大资金分批", "本金较大：分批建仓、不一次打满，降低择时风险与冲击成本。"),
    ("资金与周期", "短线快进快出", "短线：只做强趋势/尖峰顺势，破位即走、不扛亏、不恋战；重时机、轻基本面。"),
    ("资金与周期", "波段持结构", "波段：以趋势/通道/结构为主线，持到结构被破坏或到结构目标位；容忍日内噪声。"),
    ("资金与周期", "长线看基本面", "长线：以业绩增速+估值为主，技术只用于择时；忽略日内波动，重仓须基本面支撑。"),
    ("资金与周期", "仓位与风险敞口", "单一标的敞口有度：波动越大、确定性越低，投入越少；别把小本金全押在一只高波动股上。"),

    # ── 风控执行（模拟交易/换手的风险技术）──
    ("风控执行", "单笔风险控制", "单笔潜在亏损（入场−止损）控制在本金的 1~2% 以内，据此反推该买多少手；风险越大买越少。"),
    ("风控执行", "手续费意识", "频繁换手侵蚀收益：佣金≥5元 + 卖出印花税0.05% + 过户费；别为几个点反复买卖，一进一出成本要覆盖得住。"),
    ("风控执行", "分批建仓", "不一次打满：首仓试探，趋势/信号确认后再加，降低择时错误代价。"),
    ("风控执行", "分批止盈", "到第一目标 TP1 先减一部分锁定利润，剩余跟随趋势或移动止损，让利润奔跑。"),
    ("风控执行", "别追涨杀跌", "涨停不追、跌停不割在最恐慌处；按计划与规则执行，不被情绪牵着走。"),
    ("风控执行", "换手要有理由", "每次买卖都要有明确理由（逻辑变化/触发规则），不为交易而交易；没理由就持有或空仓。"),

    # ── A股独有制度与市场特性（T+1 / 涨跌停 / 集合竞价 / 打板生态 / 情绪市），把制度约束写成可执行纪律 ──
    ("A股制度特性", "T+1 交收锁定", "当天买入次日才能卖：日内看错无法当日止损、只能扛隔夜。→ 别满仓、留子弹；入场前先想『错了扛一晚能否承受』；尾盘追高风险大（次日才能处置）。"),
    ("A股制度特性", "换手率的 T+1 含义", "T+1 下当日成交≈旧筹码卖给今日新买家，新筹码当日被锁、次日才可抛。放量高换手（尤其涨停放量）=分歧大、次日高波动/易获利了结；缩量=惜售、抛压轻。换手率要结合价格方向与位置读，别单看高低。"),
    ("A股制度特性", "涨跌停板制度", "价格笼子：主板±10%、ST±5%、科创/创业±20%、北交所±30%；新股上市首日及科创创业前5日无限制。评估上行空间/下行风险要按该股的涨跌停幅度，别用统一比例。"),
    ("A股制度特性", "涨跌停的流动性陷阱", "封涨停买不到（挂涨停价按时间优先排队）、封跌停卖不出（要挂跌停价排队）。追涨停先看封单强度（弱封易炸板）；跌停别装死，能挂单出就出，别赌次日反弹。"),
    ("A股制度特性", "开盘集合竞价定价", "9:15–9:25 集合竞价定开盘价：9:20–9:25 不可撤单（真实意图），9:25 按『最大成交量成交』原则撮合出开盘价。高开/低开反映隔夜情绪与消息；大幅高开抢筹或低开砸盘是当日情绪的第一信号。"),
    ("A股制度特性", "收盘集合竞价", "14:57–15:00 集合竞价定收盘价（防尾盘操纵）。尾盘拉抬/砸盘常在此发生；收盘价是次日成本与涨跌停的基准，尾盘异动要警惕次日方向。"),
    ("A股制度特性", "一字板与封板", "集合竞价即封死涨停=一字板，盘中买不进、只能竞价排队。连板高度、封板资金、炸板率反映情绪强弱；一字板次日溢价看『昨涨停今表现』的晋级率与赚钱效应。"),
    ("A股制度特性", "打板与连板生态", "首板→连板→高标是情绪接力：连板高度=情绪高度，炸板率升高=情绪退潮。打板是高风险短线（T+1 次日才能兑现），务必非满仓、严设次日止损、退潮即离场。"),
    ("A股制度特性", "龙虎榜披露", "涨跌幅/换手/振幅达标触发龙虎榜，暴露游资与机构席位。知名游资进场是情绪信号但非买入依据；机构专用席位的大买/大卖看净额与方向，别盲目跟风。"),
    ("A股制度特性", "北向资金（陆股通）", "外资经沪深股通买卖，持续净流入的白马常获估值溢价。北向盘中披露已收紧，权威看日度统计；看趋势不看单日，北向非万能指标、退潮时同样砸盘。"),
    ("A股制度特性", "ST 与退市风险回避", "ST/*ST 涨跌停±5%、退市新规趋严（财务类/交易类/面值退市）。ST 是高风险博弈、非价值标的；本金小尤应回避退市与面值（股价<1元）风险。"),
    ("A股制度特性", "交易成本与印花税", "卖出单边印花税 0.05% + 佣金（约万2.5、最低5元）+ 过户费。高频进出被成本侵蚀，A股鼓励低频；短线一进一出要先算成本能否覆盖。"),
    ("A股制度特性", "散户情绪市特性", "A股散户成交占比高、情绪化、追涨杀跌、题材轮动快：短期趋势与情绪常大于基本面（尤其小盘/题材股）。顺情绪但不接最后一棒，高潮/冰点是反身点。"),
    ("A股制度特性", "题材轮动与概念炒作", "热点（AI/机器人/低空/固态电池…）脉冲式轮动、持续性差。热点强弱看龙头是否连板、成交能否持续放量；龙头炸板或板块缩量=退潮信号，即离场不恋战。"),
    ("A股制度特性", "停牌复牌与解禁抛压", "重大重组停牌、复牌方向不定（可能连续涨停或跌停）；大小非解禁（限售股上市流通）是潜在抛压。买入前查解禁日历，解禁前后与停牌股谨慎。"),
    ("A股制度特性", "除权除息与填权", "分红送转除权后股价下调形成缺口，需『填权』方向才赚钱；高送转是题材、不改变公司价值。除权后价格/换手率要按除权口径看，别被名义低价误导。"),
    ("A股制度特性", "新股次新股炒作", "次新股（上市≈1年内）无套牢盘、易被资金炒作；但科创/创业上市首日无涨跌幅限制、波动极大。次新是高波动短线标的，非稳健配置，仓位从严。"),
]


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(rules)").fetchall()}
        if "scenarios" not in cols:  # 老库迁移：补 scenarios 列
            c.execute("ALTER TABLE rules ADD COLUMN scenarios TEXT DEFAULT ''")
    seed()


def seed() -> int:
    """加性灌种子：补缺失的 (分类,标题) + 回填场景标签到已存在但无标签的种子规则。返回新增条数。"""
    existing = {(r["category"], r["title"]): r for r in list_rules()}
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    with _LOCK, _conn() as c:
        for cat, title, content in _SEED:
            scen = _SCENARIO_TAGS.get(title, "")
            row = existing.get((cat, title))
            if row is None:
                c.execute(
                    "INSERT INTO rules(created_at,updated_at,category,title,content,enabled,source,scenarios)"
                    " VALUES(?,?,?,?,?,1,'PA_Agent',?)", (now, now, cat, title, content, scen))
                added += 1
            elif scen and not (row.get("scenarios") or ""):
                c.execute("UPDATE rules SET scenarios=? WHERE id=?", (scen, row["id"]))
    if added:
        logger.info("规则库种子补入 %d 条", added)
    return added


def get_meta(k: str) -> str:
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else ""


def set_meta(k: str, v: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def get_scenario() -> str:
    return get_meta("scenario") or DEFAULT_SCENARIO


def set_scenario(v: str) -> None:
    set_meta("scenario", v)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    d["enabled"] = bool(d.get("enabled"))
    return d


def list_rules(category: str = "", enabled_only: bool = False) -> list[dict[str, Any]]:
    where, args = [], []
    if category:
        where.append("category = ?"); args.append(category)
    if enabled_only:
        where.append("enabled = 1")
    sql = "SELECT * FROM rules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, id"
    with _conn() as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def add(category: str, title: str, content: str, source: str = "user",
        scenarios: str = "") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO rules(created_at,updated_at,category,title,content,enabled,source,scenarios)"
            " VALUES(?,?,?,?,?,1,?,?)", (now, now, category, title, content, source, scenarios))
        return cur.lastrowid


def update(rule_id: int, **fields: Any) -> None:
    allowed = ("category", "title", "content", "enabled", "scenarios")
    sets = [f"{k}=?" for k in fields if k in allowed]
    if not sets:
        return
    args = [int(fields[k]) if k == "enabled" else fields[k] for k in fields if k in allowed]
    args.append(datetime.now().isoformat(timespec="seconds"))
    args.append(rule_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE rules SET {','.join(sets)}, updated_at=? WHERE id=?", args)


def delete(rule_id: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def _rule_tags(r: dict[str, Any]) -> set[str]:
    return {t.strip() for t in (r.get("scenarios") or "").split(",")
            if t.strip() and t.strip() != "通用"}


def is_active(r: dict[str, Any], scenario: set[str]) -> bool:
    """规则对当前场景是否生效：无场景标签(通用) 或 标签命中当前场景。"""
    tags = _rule_tags(r)
    return not tags or bool(tags & scenario)


def for_ai(scenario: str | None = None, limit: int = 100) -> str:
    """当前场景下、启用中的规则拼成提示词块（按分类分组），供 AI 遵循。空则返回空串。"""
    scen = {t.strip() for t in (scenario if scenario is not None else get_scenario()).split(",")
            if t.strip()}
    rows = [r for r in list_rules(enabled_only=True) if is_active(r, scen)]
    if not rows:
        return ""
    by_cat: dict[str, list[str]] = {}
    for r in rows[:limit]:
        by_cat.setdefault(r["category"], []).append(f"[R{r['id']}] {r['title']}：{r['content']}")
    parts = []
    for cat in CATEGORIES:
        if by_cat.get(cat):
            parts.append(f"【{cat}】\n" + "\n".join(f"- {x}" for x in by_cat[cat]))
    for cat, items in by_cat.items():  # 自定义分类兜底
        if cat not in CATEGORIES:
            parts.append(f"【{cat}】\n" + "\n".join(f"- {x}" for x in items))
    return "\n".join(parts)


def active_rules(scenario: str | None = None) -> list[dict[str, Any]]:
    """当前场景下、启用中的规则列表（与 for_ai 注入集一致）。"""
    scen = {t.strip() for t in (scenario if scenario is not None else get_scenario()).split(",")
            if t.strip()}
    return [r for r in list_rules(enabled_only=True) if is_active(r, scen)]


def active_rule_map(scenario: str | None = None) -> dict[int, str]:
    """本次注入规则 {id: title}，供 provenance.verify_basis 校验 AI 引用的规则 ID。"""
    return {r["id"]: r["title"] for r in active_rules(scenario)}


def signature() -> str:
    """当前场景 + 生效规则集合的短哈希，供 AI 缓存指纹（改场景/改规则即失效重算）。"""
    scen = get_scenario()
    active = {t.strip() for t in scen.split(",") if t.strip()}
    ids = sorted(r["id"] for r in list_rules(enabled_only=True) if is_active(r, active))
    return hashlib.sha1((scen + ":" + ",".join(map(str, ids))).encode()).hexdigest()[:8]


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
