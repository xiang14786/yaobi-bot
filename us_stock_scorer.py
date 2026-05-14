"""
us_stock_scorer.py
==================
美股評分引擎

評分維度（共 100 分）：
  1. BB 壓縮   20分 — 布林通道收縮，即將爆發
  2. 量能階梯   15分 — 量能逐步放大
  3. 動能指標   20分 — RSI + 均線排列 + 趨勢
  4. ATR 擴張  10分 — 波動率開始放大
  5. 空頭擠壓   15分 — 高空頭比例 + 技術轉強（軋空機會）
  6. 法人支撐   10分 — 高法人持股、低空頭
  7. OB+FVG    10分 — 訂單塊 + 公允價值缺口支撐

使用方式：
    from us_stock_scorer import fetch_all_us_metrics, UsStockMetrics
    metrics = await fetch_all_us_metrics(top_n=50)
"""

import math
import logging
from dataclasses import dataclass, field
from us_data_fetcher import UsStockData, fetch_all_us_stocks

log = logging.getLogger("us_scorer")


# ──────────────────────────────────────────────
#  評分結果結構
# ──────────────────────────────────────────────
@dataclass
class UsStockMetrics:
    # 原始資料（直接從 UsStockData 繼承）
    ticker:       str
    name:         str   = ""
    sector:       str   = ""
    close:        float = 0.0
    change_pct:   float = 0.0
    volume:       int   = 0
    avg_volume:   int   = 0
    volume_ratio: float = 0.0
    market_cap:   float = 0.0
    beta:         float = 1.0
    inst_pct:     float = 0.0
    short_float:  float = 0.0
    short_ratio:  float = 0.0
    week52_high:  float = 0.0
    week52_low:   float = 0.0
    data_date:    str   = ""
    # 分項分數
    bb_score:       float = 0.0
    vol_score:      float = 0.0
    momentum_score: float = 0.0
    atr_score:      float = 0.0
    short_squeeze_score: float = 0.0
    institution_score:   float = 0.0
    ob_fvg_score:   float = 0.0
    # 合計
    total_score:  float = 0.0
    early_score:  float = 0.0   # 領先指標分（bb + vol + atr）
    confidence:   float = 0.0   # 0~1
    direction:    str   = "⚪ 觀望"
    position_pct: int   = 0     # 建議倉位 %
    # 輔助
    support:      float = 0.0
    resistance:   float = 0.0
    triggers:     list[str] = field(default_factory=list)
    tags:         list[str] = field(default_factory=list)
    # RSI, MA
    rsi:          float = 50.0
    ma20:         float = 0.0
    ma50:         float = 0.0
    dist_52w_high_pct: float = 0.0  # 距 52 週高點 %


# ──────────────────────────────────────────────
#  技術指標輔助
# ──────────────────────────────────────────────
def _sma(data: list[float], n: int) -> float:
    if len(data) < n:
        return 0.0
    return sum(data[-n:]) / n

def _ema(data: list[float], n: int) -> float:
    if len(data) < n:
        return 0.0
    k = 2 / (n + 1)
    ema = sum(data[:n]) / n
    for v in data[n:]:
        ema = v * k + ema * (1 - k)
    return ema

def _stddev(data: list[float], n: int) -> float:
    if len(data) < n:
        return 0.0
    sl = data[-n:]
    mean = sum(sl) / n
    return math.sqrt(sum((x - mean) ** 2 for x in sl) / n)

def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        (gains if d > 0 else losses).append(abs(d))
    if not gains:
        return 0.0
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period if losses else 1e-9
    rs = ag / al
    return 100 - 100 / (1 + rs)

def _atr(highs, lows, closes, n: int = 14) -> float:
    if len(closes) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i]  - closes[i-1]))
        trs.append(tr)
    return sum(trs[-n:]) / n if trs else 0.0

def _bb_width(closes: list[float], n: int = 20) -> tuple[float, float, float]:
    """回傳 (upper, lower, width%) """
    if len(closes) < n:
        return 0.0, 0.0, 0.0
    ma   = _sma(closes, n)
    std  = _stddev(closes, n)
    upper = ma + 2 * std
    lower = ma - 2 * std
    width = (upper - lower) / ma * 100 if ma > 0 else 0.0
    return upper, lower, width

def _find_ob(highs, lows, closes, lookback=20):
    """找最近的看漲訂單塊（OB）"""
    if len(closes) < lookback:
        return 0.0
    for i in range(len(closes) - 2, max(len(closes) - lookback, 1), -1):
        if closes[i] > closes[i-1] and closes[i-1] < lows[i]:
            return lows[i-1]
    return 0.0


# ──────────────────────────────────────────────
#  各維度評分
# ──────────────────────────────────────────────
def score_bb(d: UsStockData) -> tuple[float, list[str]]:
    """BB 壓縮評分（0~20）"""
    if len(d.closes) < 20:
        return 0.0, []
    triggers = []

    upper, lower, width = _bb_width(d.closes, 20)

    # 寬度百分位（歷史最近 20 日與 60 日比較）
    widths_60 = []
    for i in range(20, len(d.closes)):
        sl = d.closes[i-20:i]
        ma = sum(sl) / 20
        sd = math.sqrt(sum((x-ma)**2 for x in sl)/20)
        widths_60.append((4*sd/ma*100) if ma > 0 else 0)
    widths_60.append(width)

    if not widths_60:
        return 0.0, []

    rank = sum(1 for w in widths_60 if w <= width) / len(widths_60)
    score = 0.0

    if rank <= 0.15:
        score = 20.0
        triggers.append("🔵 BB 極致壓縮（歷史低點）")
    elif rank <= 0.30:
        score = 15.0
        triggers.append("🔵 BB 明顯壓縮")
    elif rank <= 0.50:
        score = 8.0
        triggers.append("🔵 BB 壓縮中")

    # 價格在帶內位置（靠近下軌加分）
    if lower > 0 and upper > lower:
        pos = (d.close - lower) / (upper - lower)
        if 0.2 <= pos <= 0.5 and score > 0:
            score = min(20, score + 3)

    return score, triggers


def score_volume(d: UsStockData) -> tuple[float, list[str]]:
    """量能評分（0~15）"""
    if len(d.volumes) < 10:
        return 0.0, []
    triggers = []
    score = 0.0

    # 量比（今日量 vs 均量）
    if d.volume_ratio >= 3.0:
        score += 10; triggers.append("📈 爆量（量比 3x+）")
    elif d.volume_ratio >= 2.0:
        score += 7;  triggers.append("📈 大量（量比 2x+）")
    elif d.volume_ratio >= 1.5:
        score += 4;  triggers.append("📈 量能放大（1.5x）")

    # 量能趨勢（近 5 日平均量 vs 近 20 日平均量）
    avg5  = sum(d.volumes[-5:])  / 5
    avg20 = sum(d.volumes[-20:]) / 20 if len(d.volumes) >= 20 else avg5
    if avg20 > 0 and avg5 / avg20 >= 1.3:
        score += 5; triggers.append("📈 量能持續擴大")
    elif avg20 > 0 and avg5 / avg20 >= 1.1:
        score += 2

    return min(score, 15.0), triggers


def score_momentum(d: UsStockData) -> tuple[float, list[str]]:
    """動能評分：RSI + 均線排列（0~20）"""
    if len(d.closes) < 20:
        return 0.0, []
    triggers = []
    score = 0.0

    rsi = _rsi(d.closes)
    ma20 = _sma(d.closes, 20)
    ma50 = _sma(d.closes, min(50, len(d.closes)))

    # RSI 評分
    if 55 <= rsi <= 75:
        score += 8; triggers.append(f"💹 RSI 健康牛區（{rsi:.0f}）")
    elif 50 <= rsi < 55:
        score += 4
    elif rsi > 75:
        score += 2  # 過熱，給少分
    elif 40 <= rsi < 50:
        score += 1  # 弱

    # 均線排列
    if d.close > ma20 > 0:
        score += 4
        if d.close > ma50 > 0 and ma20 > ma50:
            score += 4; triggers.append("📊 多頭排列（價>MA20>MA50）")
        else:
            triggers.append("📊 站上 MA20")

    # 趨勢強度（價格距 MA20 的偏離）
    if ma20 > 0:
        dev = (d.close - ma20) / ma20 * 100
        if 1 <= dev <= 8:
            score += 4   # 剛突破，適合追
        elif dev > 8:
            score += 1   # 漲多，稍微給分

    return min(score, 20.0), triggers


def score_atr(d: UsStockData) -> tuple[float, list[str]]:
    """ATR 波動擴張（0~10）"""
    if len(d.closes) < 20:
        return 0.0, []
    triggers = []

    atr_now  = _atr(d.highs, d.lows, d.closes, 7)
    atr_prev = _atr(d.highs[:-5], d.lows[:-5], d.closes[:-5], 7) if len(d.closes) > 12 else atr_now
    if atr_now <= 0 or atr_prev <= 0:
        return 0.0, []

    ratio = atr_now / atr_prev
    if ratio >= 1.6:
        triggers.append("🌊 波動率大幅擴張")
        return 10.0, triggers
    elif ratio >= 1.3:
        triggers.append("🌊 波動率擴張中")
        return 7.0, triggers
    elif ratio >= 1.1:
        return 4.0, triggers
    return 0.0, []


def score_short_squeeze(d: UsStockData) -> tuple[float, list[str]]:
    """
    空頭擠壓潛力（0~15）
    高空頭比例 + 股價技術轉強 = 軋空機會
    """
    triggers = []
    score = 0.0

    sf = d.short_float   # 0~1

    # 高空頭比例本身就是潛在引爆點
    if sf >= 0.25:
        score += 8; triggers.append(f"🎯 超高空頭比 {sf:.0%}（軋空風險）")
    elif sf >= 0.15:
        score += 5; triggers.append(f"🎯 高空頭比 {sf:.0%}")
    elif sf >= 0.08:
        score += 2

    # days to cover（越高越容易軋空）
    if d.short_ratio >= 5:
        score += 4; triggers.append(f"⏳ 回補需 {d.short_ratio:.0f} 天")
    elif d.short_ratio >= 3:
        score += 2

    # 空頭比高 AND 股價反彈 → 強軋空訊號
    if sf >= 0.15 and d.change_pct >= 2:
        score += 3; triggers.append("🚀 空頭回補中（股價反彈）")

    return min(score, 15.0), triggers


def score_institution(d: UsStockData) -> tuple[float, list[str]]:
    """法人持股評分（0~10）"""
    triggers = []
    score = 0.0

    ip = d.inst_pct   # 0~1
    if ip >= 0.80:
        score = 10; triggers.append(f"🏦 法人高持股 {ip:.0%}")
    elif ip >= 0.60:
        score = 7;  triggers.append(f"🏦 法人持股 {ip:.0%}")
    elif ip >= 0.40:
        score = 4
    elif ip >= 0.20:
        score = 2

    return score, triggers


def score_ob_fvg(d: UsStockData) -> tuple[float, list[str]]:
    """OB + FVG 支撐結構（0~10）"""
    if len(d.closes) < 20:
        return 0.0, []
    triggers = []
    score = 0.0

    ob_level = _find_ob(d.highs, d.lows, d.closes, lookback=20)
    if ob_level > 0:
        dist = (d.close - ob_level) / d.close
        if 0 <= dist <= 0.03:
            score = 10; triggers.append(f"🏦 在 Bullish OB 支撐區（${ob_level:,.2f}）")
        elif 0 <= dist <= 0.06:
            score = 6;  triggers.append(f"🏦 接近 Bullish OB（${ob_level:,.2f}）")

    # 52 週低點附近加分（長線支撐）
    if d.week52_low > 0 and d.close > 0:
        dist_low = (d.close - d.week52_low) / d.close
        if 0 <= dist_low <= 0.05:
            score = max(score, 8)
            triggers.append(f"🏦 接近 52 週低點（${d.week52_low:,.2f}）")

    return min(score, 10.0), triggers


# ──────────────────────────────────────────────
#  綜合評分
# ──────────────────────────────────────────────
def score_us_stock(d: UsStockData) -> UsStockMetrics:
    """對單一美股進行完整評分，回傳 UsStockMetrics"""
    m = UsStockMetrics(
        ticker=d.ticker, name=d.name or d.ticker,
        sector=d.sector,
        close=d.close, change_pct=d.change_pct,
        volume=d.volume, avg_volume=d.avg_volume, volume_ratio=d.volume_ratio,
        market_cap=d.market_cap, beta=d.beta,
        inst_pct=d.inst_pct, short_float=d.short_float, short_ratio=d.short_ratio,
        week52_high=d.week52_high, week52_low=d.week52_low,
        data_date=d.data_date,
    )

    if not d.fetch_ok or d.close <= 0:
        return m

    all_triggers = []

    m.bb_score,            t1 = score_bb(d)
    m.vol_score,           t2 = score_volume(d)
    m.momentum_score,      t3 = score_momentum(d)
    m.atr_score,           t4 = score_atr(d)
    m.short_squeeze_score, t5 = score_short_squeeze(d)
    m.institution_score,   t6 = score_institution(d)
    m.ob_fvg_score,        t7 = score_ob_fvg(d)

    for t in [t1, t2, t3, t4, t5, t6, t7]:
        all_triggers.extend(t)

    m.total_score = (
        m.bb_score + m.vol_score + m.momentum_score + m.atr_score +
        m.short_squeeze_score + m.institution_score + m.ob_fvg_score
    )
    m.early_score = m.bb_score + m.vol_score + m.atr_score
    m.confidence  = min(m.total_score / 100, 1.0)

    # 技術輔助資料
    m.rsi  = _rsi(d.closes)
    m.ma20 = _sma(d.closes, 20)
    m.ma50 = _sma(d.closes, min(50, len(d.closes)))
    if d.week52_high > 0:
        m.dist_52w_high_pct = (d.close - d.week52_high) / d.week52_high * 100

    # 方向判斷
    bull_signals = m.momentum_score >= 10 and m.total_score >= 45
    squeeze_play = m.short_squeeze_score >= 8 and m.bb_score >= 8
    if bull_signals or squeeze_play:
        m.direction = "🚀 多頭蓄勢"
    elif m.bb_score >= 12 and m.vol_score >= 6:
        m.direction = "🔵 蓄勢待發"
    elif m.momentum_score >= 8:
        m.direction = "📈 趨勢向上"
    else:
        m.direction = "⚪ 觀望"

    # 支撐壓力
    ob = _find_ob(d.highs, d.lows, d.closes)
    m.support    = ob if ob > 0 else m.ma20
    m.resistance = d.week52_high if d.week52_high > d.close else 0.0

    # 倉位建議
    c = m.confidence
    if   c < 0.40: m.position_pct = 0
    elif c < 0.55: m.position_pct = 5
    elif c < 0.70: m.position_pct = 10
    elif c < 0.85: m.position_pct = 15
    else:          m.position_pct = 20

    # 標籤
    if m.total_score >= 65: m.tags.append("💎 強訊號")
    if m.short_float >= 0.20: m.tags.append("🎯 軋空候選")
    if m.volume_ratio >= 2: m.tags.append("🔥 爆量")
    if m.dist_52w_high_pct >= -3: m.tags.append("📌 近 52W 高")

    m.triggers = all_triggers[:5]
    return m


# ──────────────────────────────────────────────
#  篩選函式
# ──────────────────────────────────────────────
DEFAULT_US_FILTERS = {
    "min_score":      45,    # 總分上限 100，45% 以上才進
    "min_early":      18,    # 早分上限約 45（BB20+量15+ATR10），18 ≈ 40%
    "min_market_cap": 5e9,   # 50 億美金以上（排除小型股）
    "max_change_pct": 10.0,  # S&P500 單日超過 10% 通常是異常事件
}

def find_us_squeeze(metrics: list[UsStockMetrics], top_n=10) -> list[UsStockMetrics]:
    """BB 壓縮股：蓄勢待發"""
    r = [m for m in metrics if m.bb_score >= 10 and m.close > 0]
    return sorted(r, key=lambda x: x.bb_score + x.vol_score, reverse=True)[:top_n]

def find_us_short_squeeze(metrics: list[UsStockMetrics], top_n=10) -> list[UsStockMetrics]:
    """空頭擠壓候選：高空頭比 + 技術轉強"""
    r = [m for m in metrics if m.short_float >= 0.10 and m.momentum_score >= 6]
    return sorted(r, key=lambda x: x.short_squeeze_score + x.momentum_score, reverse=True)[:top_n]

def find_us_momentum(metrics: list[UsStockMetrics], top_n=10) -> list[UsStockMetrics]:
    """動能股：RSI + 均線排列強"""
    r = [m for m in metrics if m.momentum_score >= 10]
    return sorted(r, key=lambda x: x.momentum_score + x.total_score, reverse=True)[:top_n]

def apply_us_filters(
    metrics: list[UsStockMetrics],
    filters: dict | None = None,
) -> list[UsStockMetrics]:
    f = {**DEFAULT_US_FILTERS, **(filters or {})}
    r = [
        m for m in metrics
        if m.total_score >= f["min_score"]
        and m.early_score >= f["min_early"]
        and m.market_cap  >= f["min_market_cap"]
        and abs(m.change_pct) <= f["max_change_pct"]
    ]
    return sorted(r, key=lambda x: x.total_score, reverse=True)


# ──────────────────────────────────────────────
#  主入口：取得所有評分
# ──────────────────────────────────────────────
async def fetch_all_us_metrics(
    tickers: list[str] | None = None,
    top_n: int = 60,
) -> list[UsStockMetrics]:
    """完整流程：抓資料 → 評分 → 回傳"""
    raw = await fetch_all_us_stocks(tickers=tickers, top_n=top_n)
    metrics = [score_us_stock(d) for d in raw]
    metrics.sort(key=lambda x: x.total_score, reverse=True)
    log.info(f"[US] 評分完成，{len(metrics)} 支")
    return metrics
