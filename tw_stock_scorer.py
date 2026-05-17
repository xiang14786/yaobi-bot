"""
tw_stock_scorer.py
==================
台股版評分引擎

評分維度（共 8 維）：
  ── 通用指標（複用加密版）──
  [1] BB 壓縮          布林帶寬歷史低位 → 即將爆發        12%
  [2] 量能階梯放大      成交量逐步遞增                    8%
  [3] 沉睡甦醒          長期低波突然異動                  8%
  [4] 波動率擴張        ATR 連續上升                      7%
  [5] CVD 背離          主動買賣量 vs 價格背離            7%
  [6] OB+FVG 結構       機構訂單區/缺口                   8%

  ── 台股特有指標 ──
  [7] 法人動向          外資+投信+自營商淨買超            35% ← 最強（已升）
  [8] 融資融券          融資增加+融券回補 = 多頭信號      15%

輸出：
  TwStockMetrics 物件，供 tw_bot.py 格式化顯示
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

from tw_data_fetcher import TwStockData, fetch_all_tw_stocks

log = logging.getLogger("tw_scorer")


# ──────────────────────────────────────────────
#  輸出資料結構
# ──────────────────────────────────────────────
@dataclass
class TwStockMetrics:
    # 基本資訊
    stock_id:   str
    name:       str
    close:      float
    change_pct: float
    volume:     int
    trade_value: float

    # 各維度分數
    score_bb:          float = 0.0   # BB 壓縮
    score_vol_ladder:  float = 0.0   # 量能階梯
    score_sleep:       float = 0.0   # 沉睡甦醒
    score_atr:         float = 0.0   # 波動率擴張
    score_cvd:         float = 0.0   # CVD 背離
    score_ob_fvg:      float = 0.0   # OB+FVG 結構
    score_institution: float = 0.0   # 法人動向
    score_margin:      float = 0.0   # 融資融券
    ma20:              float = 0.0   # MA20 值（顯示用）
    ma60:              float = 0.0   # MA60 值（顯示用）
    ma_multiplier:     float = 1.0   # 趨勢修正乘數

    # 綜合分數
    total_score:  float = 0.0
    early_score:  float = 0.0   # 領先分（BB+沉睡+量能）
    confidence:   float = 0.0   # 信心度 0~1

    # 方向判斷
    direction:    str = "⚪ 觀望"
    position_pct: int = 0       # 建議倉位 %（台股不用槓桿）

    # 結構位
    support:     Optional[float] = None
    resistance:  Optional[float] = None

    # 觸發訊號列表
    triggers: list[str] = field(default_factory=list)
    tags:     list[str] = field(default_factory=list)

    # 法人原始數據（顯示用）
    foreign_net:      float = 0.0
    foreign_streak:   int   = 0
    institutional_net: float = 0.0
    margin_change_pct: float = 0.0
    short_change_pct:  float = 0.0


# ──────────────────────────────────────────────
#  技術指標計算工具
# ──────────────────────────────────────────────
def _sma(data: list[float], n: int) -> list[float]:
    if len(data) < n:
        return []
    return [sum(data[i:i+n]) / n for i in range(len(data) - n + 1)]

def _std(data: list[float], n: int) -> list[float]:
    sma = _sma(data, n)
    result = []
    for i, m in enumerate(sma):
        variance = sum((data[i+j] - m)**2 for j in range(n)) / n
        result.append(math.sqrt(variance))
    return result

def _atr(highs, lows, closes, n: int = 14) -> list[float]:
    if len(closes) < n + 1:
        return []
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i]  - closes[i-1]))
        trs.append(tr)
    return _sma(trs, n)

def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s)-1)]


# ──────────────────────────────────────────────
#  各維度評分函式
# ──────────────────────────────────────────────
def score_ma_trend(closes: list[float]) -> tuple[float, float, float]:
    """MA20/MA60 趨勢修正乘數 (0.82~1.12)，回傳 (multiplier, ma20, ma60)"""
    if len(closes) < 60:
        return 1.0, 0.0, 0.0
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    price = closes[-1]
    if price > ma20 > ma60:
        return 1.12, ma20, ma60
    elif price > ma60 and ma20 >= ma60:
        return 1.05, ma20, ma60
    elif price > ma20 and ma20 < ma60:
        return 1.02, ma20, ma60
    elif price < ma20 < ma60:
        return 0.82, ma20, ma60
    elif price < ma60:
        return 0.88, ma20, ma60
    else:
        return 1.0, ma20, ma60

def score_bb_squeeze(closes: list[float], n: int = 20, mult: float = 2.0) -> tuple[float, bool]:
    """布林帶壓縮評分 (0~100)，並回傳是否壓縮中"""
    if len(closes) < 40:
        return 0.0, False
    sma = _sma(closes, n)
    std = _std(closes, n)
    if not sma or not std:
        return 0.0, False
    widths = [2 * mult * s / m * 100 if m > 0 else 0
              for s, m in zip(std, sma)]
    if len(widths) < 20:
        return 0.0, False
    current   = widths[-1]
    threshold = _percentile(widths, 25)   # 歷史 25 分位
    is_squeeze = current <= threshold
    if is_squeeze:
        tightness = max(0, (threshold - current) / threshold)
        return min(100, 60 + tightness * 40), True
    return 0.0, False


def score_volume_ladder(volumes: list[int], n: int = 5) -> tuple[float, bool]:
    """量能階梯放大評分"""
    if len(volumes) < n:
        return 0.0, False
    recent = volumes[-n:]
    ladder = all(recent[i] >= recent[i-1] * 1.05 for i in range(1, n))
    if not ladder:
        # 寬鬆判定：至少 3/4 根遞增
        inc_count = sum(1 for i in range(1, n) if recent[i] > recent[i-1])
        if inc_count < n - 2:
            return 0.0, False
        return 50.0, True
    # 計算總放大倍率
    ratio = recent[-1] / recent[0] if recent[0] > 0 else 1
    return min(100, 60 + min(ratio - 1, 2) * 20), True


def score_sleeping_wake(volumes: list[int], closes: list[float],
                        sleep_n: int = 30, vol_mult: float = 2.0) -> tuple[float, bool]:
    """沉睡甦醒評分"""
    if len(volumes) < sleep_n + 5:
        return 0.0, False
    sleep_vols = volumes[-sleep_n-5:-5]
    recent_vol = volumes[-1]
    avg_sleep  = sum(sleep_vols) / len(sleep_vols) if sleep_vols else 1
    # 過去睡眠期量能穩定（最大不超過平均 1.5 倍）
    was_sleeping = max(sleep_vols) < avg_sleep * 1.5 if sleep_vols else False
    # 突然爆量
    waking = recent_vol > avg_sleep * vol_mult
    if was_sleeping and waking:
        ratio = recent_vol / avg_sleep
        return min(100, 60 + min(ratio - vol_mult, 3) * 10), True
    return 0.0, False


def score_atr_expansion(highs, lows, closes, n: int = 3) -> tuple[float, bool]:
    """ATR 連續擴張評分"""
    atrs = _atr(highs, lows, closes, 14)
    if len(atrs) < n + 1:
        return 0.0, False
    recent = atrs[-(n+1):]
    expanding = all(recent[i] > recent[i-1] for i in range(1, len(recent)))
    if not expanding:
        return 0.0, False
    acceleration = recent[-1] / recent[0] if recent[0] > 0 else 1
    return min(100, 50 + min(acceleration - 1, 1) * 50), True


def score_cvd_divergence(closes: list[float], volumes: list[int],
                         highs: list[float], lows: list[float]) -> tuple[float, str]:
    """
    CVD 背離評分。
    回傳 (分數, 方向: 'bull'/'bear'/'none')
    """
    if len(closes) < 20:
        return 0.0, "none"
    # 估算 CVD：(close-open)/(high-low) * volume
    # 用 close vs 前一根 close 估算 open
    cvd = []
    for i in range(1, len(closes)):
        rng = highs[i] - lows[i] if highs[i] > lows[i] else 0.001
        direction = (closes[i] - closes[i-1]) / rng
        cvd.append(direction * volumes[i])
    if len(cvd) < 10:
        return 0.0, "none"
    # 累積 CVD
    cum_cvd = []
    total = 0
    for v in cvd:
        total += v
        cum_cvd.append(total)
    # 最近 10 根的背離
    n = 10
    price_low  = min(closes[-n:])
    price_high = max(closes[-n:])
    cvd_low    = min(cum_cvd[-n:])
    cvd_high   = max(cum_cvd[-n:])
    curr_price = closes[-1]
    curr_cvd   = cum_cvd[-1]
    # 看多背離：價格低點 但 CVD 不創低
    if curr_price <= price_low * 1.005 and curr_cvd > cvd_low * 1.05:
        return 70.0, "bull"
    # 看空背離：價格高點 但 CVD 不創高
    if curr_price >= price_high * 0.995 and curr_cvd < cvd_high * 0.95:
        return 70.0, "bear"
    return 0.0, "none"


def score_ob_fvg(closes: list[float], highs: list[float],
                 lows: list[float]) -> tuple[float, Optional[float], Optional[float]]:
    """
    OB+FVG 結構評分。
    回傳 (分數, 最近支撐位, 最近阻力位)
    """
    if len(closes) < 20:
        return 0.0, None, None
    # 找 Bullish OB：下跌趨勢中最後一根紅 K（收>開），之後出現強勢上推
    support = None
    resistance = None
    score = 0.0
    n = min(len(closes), 30)
    for i in range(1, n - 2):
        idx = -(n) + i
        # Bullish OB 條件
        if closes[idx] > closes[idx-1]:   # 紅 K
            # 之後出現大幅上漲
            if closes[idx+1] > closes[idx] * 1.005:
                ob_level = (closes[idx] + lows[idx]) / 2
                if support is None or abs(closes[-1] - ob_level) < abs(closes[-1] - support):
                    support = ob_level
                    score = max(score, 60.0)
        # Bearish OB 條件
        if closes[idx] < closes[idx-1]:   # 綠 K（收<開）
            if closes[idx+1] < closes[idx] * 0.995:
                ob_level = (closes[idx] + highs[idx]) / 2
                if resistance is None or abs(closes[-1] - ob_level) < abs(closes[-1] - resistance):
                    resistance = ob_level
                    score = max(score, 60.0)
    # 價格在支撐 OB 附近加分
    if support and closes[-1] <= support * 1.02:
        score = min(100, score + 25)
    return score, support, resistance


# ──────────────────────────────────────────────
#  台股特有指標
# ──────────────────────────────────────────────
def score_institutional(
    foreign_net: float,
    trust_net: float,
    dealer_net: float,
    institutional_net: float,
    foreign_streak: int,
    trade_value: float,
) -> tuple[float, str]:
    """
    三大法人動向評分 (0~100)
    外資是最重要的指標，加上投信和自營商。
    """
    score = 0.0
    direction = "neutral"
    if trade_value <= 0:
        return 0.0, direction
    # 外資淨買超/成交金額 → 相對強度
    foreign_ratio = foreign_net / trade_value if trade_value > 0 else 0
    if foreign_ratio > 0.05:          # 外資大買
        score += 40
        direction = "bull"
    elif foreign_ratio > 0.02:
        score += 25
        direction = "bull"
    elif foreign_ratio < -0.05:       # 外資大賣
        score += 30    # 看空信號
        direction = "bear"
    elif foreign_ratio < -0.02:
        score += 20
        direction = "bear"
    # 外資連續買超加分
    if foreign_streak >= 5 and direction == "bull":
        score += min(20, foreign_streak * 3)
    elif foreign_streak <= -5 and direction == "bear":
        score += min(20, abs(foreign_streak) * 3)
    # 投信買超額外加分（提高至 25 分，投信是重要籌碼）
    trust_ratio = trust_net / trade_value if trade_value > 0 else 0
    if trust_ratio > 0.02 and direction == "bull":
        score += 25   # 投信大買
    elif trust_ratio > 0.01 and direction == "bull":
        score += 18
    elif trust_ratio < -0.02 and direction == "bear":
        score += 25
    elif trust_ratio < -0.01 and direction == "bear":
        score += 18
    # 法人聯手加分（外資+投信同向，最強信號）
    is_joint_bull = foreign_net > 0 and trust_net > 0
    is_joint_bear = foreign_net < 0 and trust_net < 0
    if is_joint_bull and direction == "bull":
        score += 20   # 法人聯手買
    elif is_joint_bear and direction == "bear":
        score += 20
    elif dealer_net > 0 and direction == "bull":
        score += 5    # 三大同向
    return min(100, score), direction


def score_margin_trading(
    margin_change_pct: float,
    short_change_pct: float,
    margin_balance: int,
    short_balance: int,
) -> tuple[float, str]:
    """
    融資融券評分 (0~100)
    融資增加 = 散戶看多（偏多但需注意過熱）
    融券增加 = 空頭布局（看空信號）
    融券回補（融券減少）= 強力看多
    """
    score = 0.0
    direction = "neutral"
    # 融券大幅回補 → 最強看多信號
    if short_change_pct < -5:
        score += 40
        direction = "bull"
        if short_change_pct < -10:
            score += 20
    # 融資適度增加（非過熱）
    if 2 < margin_change_pct < 15:
        score += 30
        direction = "bull" if direction == "neutral" else direction
    elif margin_change_pct > 15:   # 融資過熱，反向信號
        score += 20
        direction = "bear"
    # 融券大增 = 空頭信號
    if short_change_pct > 10:
        score += 30
        direction = "bear"
    # 融資大減（停損賣壓）= 看空
    if margin_change_pct < -10:
        score += 25
        direction = "bear"
    return min(100, score), direction


# ──────────────────────────────────────────────
#  主評分函式
# ──────────────────────────────────────────────
def score_tw_stock(raw: TwStockData) -> TwStockMetrics:
    """把 TwStockData 轉成 TwStockMetrics（計算所有評分）"""
    closes  = raw.closes
    highs   = raw.highs
    lows    = raw.lows
    volumes = raw.volumes

    m = TwStockMetrics(
        stock_id    = raw.stock_id,
        name        = raw.name,
        close       = raw.close,
        change_pct  = raw.change_pct,
        volume      = raw.volume,
        trade_value = raw.trade_value,
        foreign_net       = raw.foreign_net,
        foreign_streak    = raw.foreign_streak,
        institutional_net = raw.institutional_net,
        margin_change_pct = raw.margin_change_pct,
        short_change_pct  = raw.short_change_pct,
    )

    # 1. BB 壓縮
    m.score_bb, is_squeeze = score_bb_squeeze(closes)
    if is_squeeze:
        m.triggers.append("🔵 BB 壓縮中（即將爆發）")
        m.tags.append("🔵BB壓縮")

    # 2. 量能階梯
    m.score_vol_ladder, vol_ok = score_volume_ladder(volumes)
    if vol_ok:
        m.triggers.append("📈 量能階梯放大")

    # 3. 沉睡甦醒
    m.score_sleep, sleep_ok = score_sleeping_wake(volumes, closes)
    if sleep_ok:
        m.triggers.append("⚡ 沉睡甦醒（長期低波突然異動）")
        m.tags.append("⚡甦醒")

    # 4. ATR 擴張
    m.score_atr, atr_ok = score_atr_expansion(highs, lows, closes)
    if atr_ok:
        m.triggers.append("🌊 波動率擴張中")

    # 5. CVD 背離
    m.score_cvd, cvd_dir = score_cvd_divergence(closes, volumes, highs, lows)
    if cvd_dir == "bull":
        m.triggers.append("📊 CVD 看多背離（主力偷偷買）")
    elif cvd_dir == "bear":
        m.triggers.append("📊 CVD 看空背離（主力偷偷賣）")

    # 6. OB+FVG
    m.score_ob_fvg, m.support, m.resistance = score_ob_fvg(closes, highs, lows)
    if m.support and raw.close <= m.support * 1.02:
        m.triggers.append(f"🏦 在 Bullish OB 支撐區（${m.support:,.2f}）")

    # 7. 三大法人
    m.score_institution, inst_dir = score_institutional(
        raw.foreign_net, raw.trust_net, raw.dealer_net,
        raw.institutional_net, raw.foreign_streak, raw.trade_value,
    )
    if raw.foreign_net > 0:
        streak_str = f"連續 {abs(raw.foreign_streak)} 天" if abs(raw.foreign_streak) >= 3 else ""
        m.triggers.append(f"🏦 外資買超 {raw.foreign_net/1e8:+.1f} 億 {streak_str}")
        if raw.foreign_streak >= 5:
            m.tags.append("🏦外資連買")
    elif raw.foreign_net < 0:
        m.triggers.append(f"🏦 外資賣超 {raw.foreign_net/1e8:+.1f} 億")
    # 投信動向
    if raw.trust_net > 0:
        m.triggers.append(f"📊 投信買超 {raw.trust_net/1e8:+.2f} 億")
        if raw.foreign_net > 0:
            m.tags.append("🤝法人聯手")
    elif raw.trust_net < 0:
        m.triggers.append(f"📊 投信賣超 {raw.trust_net/1e8:+.2f} 億")

    # 8. 融資融券
    m.score_margin, margin_dir = score_margin_trading(
        raw.margin_change_pct, raw.short_change_pct,
        raw.margin_balance, raw.short_balance,
    )
    if raw.short_change_pct < -5:
        m.triggers.append(f"🔥 融券大量回補 ({raw.short_change_pct:+.1f}%)")
        m.tags.append("🔥融券回補")
    if 2 < raw.margin_change_pct < 15:
        m.triggers.append(f"💰 融資適度增加 ({raw.margin_change_pct:+.1f}%)")

    # ── 綜合評分（加權平均）──
    weights = {
        "bb":          0.12,
        "vol_ladder":  0.08,
        "sleep":       0.08,
        "atr":         0.07,
        "cvd":         0.07,
        "ob_fvg":      0.08,
        "institution": 0.35,   # 法人動向最重（升至 35%）
        "margin":      0.15,
    }
    m.total_score = (
        m.score_bb          * weights["bb"]          +
        m.score_vol_ladder  * weights["vol_ladder"]  +
        m.score_sleep       * weights["sleep"]       +
        m.score_atr         * weights["atr"]         +
        m.score_cvd         * weights["cvd"]         +
        m.score_ob_fvg      * weights["ob_fvg"]      +
        m.score_institution * weights["institution"] +
        m.score_margin      * weights["margin"]
    )

    # MA20/MA60 趨勢修正（乘數：0.82 ~ 1.12）
    m.ma_multiplier, m.ma20, m.ma60 = score_ma_trend(closes)
    m.total_score = min(100, m.total_score * m.ma_multiplier)
    if m.ma_multiplier >= 1.10 and m.ma20 > 0:
        m.triggers.append(f"📈 多頭排列（MA20 {m.ma20:.1f} > MA60 {m.ma60:.1f}）")
        m.tags.append("📈多頭排列")
    elif m.ma_multiplier <= 0.85 and m.ma60 > 0:
        m.triggers.append(f"📉 跌破季線（MA60 {m.ma60:.1f}），訊號打折")

    # 領先分（尚未啟動的早期信號）
    m.early_score = (
        m.score_bb         * 0.35 +
        m.score_sleep      * 0.30 +
        m.score_vol_ladder * 0.20 +
        m.score_cvd        * 0.15
    )

    # 信心度
    m.confidence = min(1.0, m.total_score / 75)

    # ── 方向判斷 ──
    bull_signals = sum([
        inst_dir == "bull",
        margin_dir == "bull",
        cvd_dir == "bull",
        raw.change_pct > 0,
    ])
    bear_signals = sum([
        inst_dir == "bear",
        margin_dir == "bear",
        cvd_dir == "bear",
        raw.change_pct < 0,
    ])
    if is_squeeze or sleep_ok:
        m.direction = "🔵 蓄勢待發"
    elif bull_signals >= 3:
        m.direction = "🚀 多方強勢"
    elif bull_signals >= 2:
        m.direction = "🔋 偏多蓄勢"
    elif bear_signals >= 3:
        m.direction = "📉 空方強勢"
    elif bear_signals >= 2:
        m.direction = "⚠️ 偏空壓力"
    else:
        m.direction = "⚪ 觀望整理"

    # ── 倉位建議 %（台股現股，無槓桿）──
    conf = m.confidence
    if conf < 0.4:
        m.position_pct = 0
    elif conf < 0.55:
        m.position_pct = 5
    elif conf < 0.7:
        m.position_pct = 10
    elif conf < 0.85:
        m.position_pct = 15
    else:
        m.position_pct = 20

    return m


# ──────────────────────────────────────────────
#  掃描全部台股
# ──────────────────────────────────────────────
async def fetch_all_tw_metrics(top_n: int = 50) -> list[TwStockMetrics]:
    """掃描並評分，回傳依總分排序的 TwStockMetrics 列表"""
    raw_list = await fetch_all_tw_stocks(top_n=top_n)
    metrics  = [score_tw_stock(r) for r in raw_list]
    metrics.sort(key=lambda m: m.total_score, reverse=True)
    return metrics


async def fetch_single_tw_metrics(stock_id: str) -> "TwStockMetrics | None":
    """直接查詢單支台股並評分（不受掃描清單限制，供 /stock 使用）"""
    from tw_data_fetcher import fetch_single_tw_stock
    raw = await fetch_single_tw_stock(stock_id)
    if not raw.fetch_ok:
        return None
    return score_tw_stock(raw)


# ──────────────────────────────────────────────
#  篩選工具
# ──────────────────────────────────────────────
DEFAULT_TW_FILTERS = {
    "min_total_score":    45,
    "min_early_score":    20,   # 早分上限約 43（BB20+量15+ATR8），20 ≈ 47%
    "max_change_pct":     8.0,  # 漲停板（10%）前才算「尚未啟動」
    "min_trade_value":    1e8,  # 至少 1 億成交金額（流動性過濾）
}

def apply_tw_filters(metrics: list[TwStockMetrics], filters: dict) -> list[TwStockMetrics]:
    f = {**DEFAULT_TW_FILTERS, **filters}
    result = []
    for m in metrics:
        if m.total_score < f["min_total_score"]:
            continue
        if m.early_score < f["min_early_score"]:
            continue
        if abs(m.change_pct) > f["max_change_pct"]:
            continue
        if m.trade_value < f["min_trade_value"]:
            continue
        result.append(m)
    return result

def find_tw_pre_pump(metrics: list[TwStockMetrics]) -> list[TwStockMetrics]:
    """預備暴漲：蓄勢但尚未大漲，外資/融券信號強"""
    return [m for m in metrics
            if m.change_pct < 5
            and (m.score_bb > 50 or m.score_sleep > 50)
            and (m.foreign_net > 0 or m.short_change_pct < -3)
            and m.total_score >= 45
            and "空" not in m.direction]  # 排除空方主導股票

def find_tw_squeeze(metrics: list[TwStockMetrics]) -> list[TwStockMetrics]:
    """布林帶壓縮蓄勢"""
    return [m for m in metrics if m.score_bb >= 60]

def find_tw_institutional_buy(metrics: list[TwStockMetrics]) -> list[TwStockMetrics]:
    """外資連續買超（≥3天）"""
    return [m for m in metrics if m.foreign_streak >= 3 and m.foreign_net > 0]

def find_tw_joint_buy(metrics: list[TwStockMetrics]) -> list[TwStockMetrics]:
    """法人聯手榜：外資 + 投信同日淨買超，且外資連買 ≥ 2 天"""
    return [
        m for m in metrics
        if m.foreign_net > 0
        and m.foreign_streak >= 2
        and "🤝法人聯手" in m.tags
        and m.total_score >= 40
    ]
