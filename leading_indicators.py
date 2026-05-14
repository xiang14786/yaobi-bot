"""
領先指標模組 (Leading Indicators)
==================================
核心理念: 不追已經爆動的幣,而是抓「即將妖動」的徵兆。

10 個提早預警維度:
1. OI 暴增 + 價格平靜      - 大資金建倉,尚未發動
2. 資金費率背離            - 費率變化 vs 價格變化的 divergence
3. 盤整壓縮 (BB Squeeze)   - 波動率收斂,蓄勢待發
4. 量能階梯式放大          - 連續逐步放量,非單根爆量
5. 多空比變化率            - 看斜率不看絕對值
6. CVD 背離                - 主動買賣量累積 vs 價格背離
7. 訂單薄稀薄度             - 流動性枯竭,易拉動
8. 多時框共振               - 15m/1h/4h 訊號一致性
9. 沉睡幣甦醒              - 長期低波後突然出現異動
10. 波動率擴張即將出現       - ATR 變化加速度
"""

import math
import statistics
from dataclasses import dataclass

from structure_analyzer import Kline


@dataclass
class LeadingSignals:
    # 核心提早分數
    early_score: float = 0.0          # 0~100 提早預警總分
    direction_bias: str = "NEUTRAL"   # PRE_PUMP / PRE_DUMP / NEUTRAL
    confidence: float = 0.0           # 0~1 信心度

    # 各子訊號
    oi_buildup: float = 0.0           # OI 暴增分
    funding_divergence: float = 0.0   # 費率背離分
    bb_squeeze: float = 0.0           # BB 壓縮分
    vol_stairs: float = 0.0           # 量能階梯分
    ls_velocity: float = 0.0          # 多空比變化率
    cvd_divergence: float = 0.0       # CVD 背離分
    sleep_wake: float = 0.0           # 沉睡甦醒分
    vol_expansion: float = 0.0        # 波動率擴張分
    mtf_align: float = 0.0            # 多時框共振分

    triggers: list = None             # 觸發的訊號文字

    def __post_init__(self):
        if self.triggers is None:
            self.triggers = []


# ============================================================
# 1. OI 暴增 + 價格平靜 (最強領先指標之一)
# ============================================================
def score_oi_buildup(oi_history: list[float], price_history: list[float]) -> tuple[float, str]:
    """
    OI 持續上升但價格波動小 = 大資金正在建倉。
    這是最經典的「暴動前夕」訊號。

    oi_history: 最近 N 個時間點的 OI (USDT 計價)
    price_history: 對應的價格
    """
    if len(oi_history) < 8 or len(price_history) < 8:
        return 0.0, ""

    # OI 變化率 (近 8 期)
    oi_change = (oi_history[-1] - oi_history[-8]) / max(oi_history[-8], 1) * 100

    # 價格變化率
    price_change = abs(price_history[-1] - price_history[-8]) / price_history[-8] * 100

    # 訊號條件: OI 漲 >5%,但價格幅度 <3%
    if oi_change > 5 and price_change < 3:
        ratio = oi_change / max(price_change, 0.5)   # 比值越大越異常
        score = min(100, ratio * 12)
        msg = f"⚠️ OI 暴增 +{oi_change:.1f}% 但價格僅動 {price_change:.1f}% (比值 {ratio:.1f})"
        return score, msg
    return 0.0, ""


# ============================================================
# 2. 資金費率背離
# ============================================================
def score_funding_divergence(funding_history: list[float],
                             price_history: list[float]) -> tuple[float, str]:
    """
    費率持續為正但價格不漲 -> 多頭擁擠,可能爆倉空頭
    費率持續為負但價格不跌 -> 空頭擁擠,可能軋空
    """
    if len(funding_history) < 6 or len(price_history) < 6:
        return 0.0, ""

    avg_funding = statistics.mean(funding_history[-6:])
    price_change = (price_history[-1] - price_history[-6]) / price_history[-6] * 100

    # 多頭擁擠但漲不動
    if avg_funding > 0.0005 and price_change < 1:
        score = min(100, avg_funding * 80000)
        return score, f"💢 費率持續正 ({avg_funding*100:.3f}%) 但漲不動 → 軋多風險"

    # 空頭擁擠但跌不動
    if avg_funding < -0.0005 and price_change > -1:
        score = min(100, abs(avg_funding) * 80000)
        return score, f"💢 費率持續負 ({avg_funding*100:.3f}%) 但跌不動 → 軋空機會"

    return 0.0, ""


# ============================================================
# 3. Bollinger Band 壓縮 (波動率收斂)
# ============================================================
def score_bb_squeeze(klines: list[Kline], period: int = 20) -> tuple[float, str]:
    """
    布林帶寬度低於近 N 期分位數 -> 即將爆發。
    歷史經驗: BB 壓縮後 1-3 根 K 內常出現大幅波動。
    """
    if len(klines) < period * 3:
        return 0.0, ""

    # 計算每個時間點的 BB Width %
    closes = [k.close for k in klines]
    widths = []
    for i in range(period, len(closes)):
        window = closes[i-period:i]
        mean = statistics.mean(window)
        std = statistics.stdev(window)
        width_pct = (std * 4) / mean * 100   # 上下軌距離占價格 %
        widths.append(width_pct)

    if not widths:
        return 0.0, ""

    current_width = widths[-1]
    # 取近 60 期分位數
    recent = widths[-60:] if len(widths) >= 60 else widths
    sorted_w = sorted(recent)
    rank = sorted_w.index(current_width) if current_width in sorted_w else 0
    percentile = rank / len(sorted_w) * 100

    # 在歷史最低 20% 區間 = 強壓縮
    if percentile < 20:
        score = (20 - percentile) * 5
        return min(100, score), f"🎯 BB 壓縮中 (歷史 {percentile:.0f} 分位) → 即將爆發"
    return 0.0, ""


# ============================================================
# 4. 量能階梯式放大
# ============================================================
def score_volume_stairs(klines: list[Kline]) -> tuple[float, str]:
    """
    最近 N 根 K 線成交量呈現「逐步遞增」趨勢,而非單根爆量。
    這代表持續性買盤/賣盤湧入,比單根爆量更有持續力。
    """
    if len(klines) < 12:
        return 0.0, ""

    recent = klines[-10:]
    older = klines[-20:-10]

    recent_avg = statistics.mean(k.volume for k in recent)
    older_avg = statistics.mean(k.volume for k in older)

    if older_avg <= 0:
        return 0.0, ""

    growth = recent_avg / older_avg

    # 計算遞增的單調性 (有多少根比前一根大)
    increases = sum(1 for i in range(1, len(recent)) if recent[i].volume > recent[i-1].volume)
    monotonic = increases / (len(recent) - 1)

    if growth > 1.5 and monotonic > 0.5:
        score = min(100, (growth - 1) * 50 + monotonic * 30)
        return score, f"📊 量能階梯式放大 ({growth:.1f}x, 遞增度 {monotonic:.0%})"
    return 0.0, ""


# ============================================================
# 5. 多空比變化率 (斜率,不是絕對值)
# ============================================================
def score_ls_velocity(ls_history: list[float]) -> tuple[float, str]:
    """
    多空比短期內快速變化 = 散戶情緒急轉,常為反指標。
    """
    if len(ls_history) < 6:
        return 0.0, ""

    start = ls_history[-6]
    end = ls_history[-1]

    if start <= 0 or end <= 0:
        return 0.0, ""

    # 對數變化避免極值
    velocity = abs(math.log(end / start))

    if velocity > 0.3:   # 約等於 ratio 變化 35% 以上
        score = min(100, velocity * 200)
        direction = "多" if end > start else "空"
        return score, f"⚡ 多空比急轉向{direction} (Δ {velocity:.2f})"
    return 0.0, ""


# ============================================================
# 6. CVD 背離 (Cumulative Volume Delta)
# ============================================================
def score_cvd_divergence(klines: list[Kline],
                         taker_ratios: list[float]) -> tuple[float, str]:
    """
    CVD 背離:
    - 價格創新低,但 CVD (買壓累積) 在升 -> 抄底訊號
    - 價格創新高,但 CVD 在降 -> 出貨訊號

    這裡用簡化版: 主動買賣比 × 量能 來估算 CVD。
    """
    if len(klines) < 20 or len(taker_ratios) < 20:
        return 0.0, ""

    # 估算每根 K 的 CVD 增量
    cvd = [0.0]
    for k, tr in zip(klines[-20:], taker_ratios[-20:]):
        # tr = buy/sell, 轉成買量比例
        buy_ratio = tr / (tr + 1) if tr > 0 else 0.5
        delta = k.volume * (buy_ratio - 0.5) * 2   # +volume 為主動買多,-volume 為主動賣多
        cvd.append(cvd[-1] + delta)

    # 比較最近 10 根的價格 vs CVD 趨勢
    prices = [k.close for k in klines[-10:]]
    cvd_recent = cvd[-10:]

    price_trend = prices[-1] - prices[0]
    cvd_trend = cvd_recent[-1] - cvd_recent[0]

    # 標準化方向
    p_dir = 1 if price_trend > 0 else -1
    c_dir = 1 if cvd_trend > 0 else -1

    if p_dir != c_dir and abs(price_trend / prices[0]) > 0.01:
        # 背離強度
        strength = abs(cvd_trend) / max(abs(sum(k.volume for k in klines[-10:])), 1) * 100
        score = min(100, strength * 5)
        if c_dir > 0:   # 價跌但買盤累積
            return score, "🔄 CVD 看漲背離 (價跌量買盤積)"
        else:           # 價漲但賣盤累積
            return score, "🔄 CVD 看跌背離 (價漲量賣盤積)"
    return 0.0, ""


# ============================================================
# 7. 沉睡幣甦醒
# ============================================================
def score_sleep_wake(klines: list[Kline]) -> tuple[float, str]:
    """
    過去長期低波,但最近 1-2 根 K 突然出現異常波動。
    這種剛甦醒的妖幣常常是大行情的起點。
    """
    if len(klines) < 50:
        return 0.0, ""

    # 過去 40 根的平均波幅
    historical = klines[-50:-5]
    hist_avg_range = statistics.mean(
        (k.high - k.low) / k.low * 100 for k in historical if k.low > 0
    )

    # 最近 5 根
    recent_max_range = max(
        (k.high - k.low) / k.low * 100 for k in klines[-5:] if k.low > 0
    )

    if hist_avg_range < 1.5 and recent_max_range > hist_avg_range * 3:
        wake_factor = recent_max_range / max(hist_avg_range, 0.3)
        score = min(100, wake_factor * 15)
        return score, f"😴→🔥 沉睡幣甦醒 (歷史均幅 {hist_avg_range:.2f}% → 突發 {recent_max_range:.2f}%)"
    return 0.0, ""


# ============================================================
# 8. 波動率擴張即將出現 (ATR 加速度)
# ============================================================
def score_volatility_expansion(klines: list[Kline]) -> tuple[float, str]:
    """
    ATR 連續上升 + 加速度為正 = 波動率將進一步擴張。
    """
    if len(klines) < 20:
        return 0.0, ""

    # 計算每根的 True Range
    trs = []
    for i in range(1, len(klines)):
        h = klines[i].high
        l = klines[i].low
        pc = klines[i-1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr / klines[i].close * 100)   # 百分比 ATR

    # ATR 移動平均
    if len(trs) < 14:
        return 0.0, ""
    atr_recent = statistics.mean(trs[-7:])
    atr_older = statistics.mean(trs[-14:-7])

    if atr_older <= 0:
        return 0.0, ""

    expansion = atr_recent / atr_older

    if expansion > 1.3:
        score = min(100, (expansion - 1) * 100)
        return score, f"📈 波動率擴張加速 ({expansion:.2f}x ATR)"
    return 0.0, ""


# ============================================================
# 9. 多時框共振
# ============================================================
def score_mtf_alignment(signals_15m: dict, signals_1h: dict,
                       signals_4h: dict) -> tuple[float, str]:
    """
    15m / 1h / 4h 三個時間框架方向是否一致。
    一致性越高,行情越有持續力。
    """
    biases = []
    for s in [signals_15m, signals_1h, signals_4h]:
        if s and s.get("bias"):
            biases.append(s["bias"])

    if len(biases) < 2:
        return 0.0, ""

    bull_count = biases.count("bullish")
    bear_count = biases.count("bearish")

    if bull_count == 3:
        return 100, "✅ 三時框全多頭共振"
    if bear_count == 3:
        return 100, "✅ 三時框全空頭共振"
    if bull_count == 2:
        return 60, "🔼 2 時框偏多"
    if bear_count == 2:
        return 60, "🔽 2 時框偏空"
    return 0.0, ""


# ============================================================
# 整合: 計算領先分數
# ============================================================
def compute_leading_signals(
    klines_15m: list[Kline],
    klines_1h: list[Kline] = None,
    klines_4h: list[Kline] = None,
    oi_history: list[float] = None,
    price_history: list[float] = None,
    funding_history: list[float] = None,
    ls_history: list[float] = None,
    taker_history: list[float] = None,
    structure_15m: dict = None,
    structure_1h: dict = None,
    structure_4h: dict = None,
) -> LeadingSignals:
    """
    彙總所有領先指標,輸出綜合提早預警分數。
    """
    sig = LeadingSignals()
    triggers = []

    # 1. OI 暴增
    if oi_history and price_history:
        s, msg = score_oi_buildup(oi_history, price_history)
        sig.oi_buildup = s
        if msg: triggers.append(msg)

    # 2. 費率背離
    if funding_history and price_history:
        s, msg = score_funding_divergence(funding_history, price_history)
        sig.funding_divergence = s
        if msg: triggers.append(msg)

    # 3. BB 壓縮
    if klines_15m:
        s, msg = score_bb_squeeze(klines_15m)
        sig.bb_squeeze = s
        if msg: triggers.append(msg)

    # 4. 量能階梯
    if klines_15m:
        s, msg = score_volume_stairs(klines_15m)
        sig.vol_stairs = s
        if msg: triggers.append(msg)

    # 5. 多空比變化率
    if ls_history:
        s, msg = score_ls_velocity(ls_history)
        sig.ls_velocity = s
        if msg: triggers.append(msg)

    # 6. CVD 背離
    if klines_15m and taker_history:
        s, msg = score_cvd_divergence(klines_15m, taker_history)
        sig.cvd_divergence = s
        if msg: triggers.append(msg)

    # 7. 沉睡甦醒
    if klines_1h:
        s, msg = score_sleep_wake(klines_1h)
        sig.sleep_wake = s
        if msg: triggers.append(msg)

    # 8. 波動率擴張
    if klines_15m:
        s, msg = score_volatility_expansion(klines_15m)
        sig.vol_expansion = s
        if msg: triggers.append(msg)

    # 9. 多時框共振
    if structure_15m and structure_1h and structure_4h:
        s, msg = score_mtf_alignment(structure_15m, structure_1h, structure_4h)
        sig.mtf_align = s
        if msg: triggers.append(msg)

    # 加權合成 (重點放在 OI/費率/壓縮/量能,因為最領先)
    weights = {
        "oi_buildup": 0.18,
        "funding_divergence": 0.14,
        "bb_squeeze": 0.13,
        "vol_stairs": 0.12,
        "ls_velocity": 0.08,
        "cvd_divergence": 0.10,
        "sleep_wake": 0.10,
        "vol_expansion": 0.08,
        "mtf_align": 0.07,
    }

    sig.early_score = (
        sig.oi_buildup * weights["oi_buildup"]
        + sig.funding_divergence * weights["funding_divergence"]
        + sig.bb_squeeze * weights["bb_squeeze"]
        + sig.vol_stairs * weights["vol_stairs"]
        + sig.ls_velocity * weights["ls_velocity"]
        + sig.cvd_divergence * weights["cvd_divergence"]
        + sig.sleep_wake * weights["sleep_wake"]
        + sig.vol_expansion * weights["vol_expansion"]
        + sig.mtf_align * weights["mtf_align"]
    )

    # 方向判斷: 結合費率與結構偏向
    bull_votes = 0
    bear_votes = 0
    if funding_history:
        avg_f = statistics.mean(funding_history[-6:]) if len(funding_history) >= 6 else 0
        if avg_f > 0.0005: bear_votes += 1   # 費率高 → 軋多 → 偏空
        if avg_f < -0.0005: bull_votes += 1  # 費率負 → 軋空 → 偏多
    if structure_1h and structure_1h.get("bias") == "bullish": bull_votes += 1
    if structure_1h and structure_1h.get("bias") == "bearish": bear_votes += 1
    if structure_4h and structure_4h.get("bias") == "bullish": bull_votes += 1
    if structure_4h and structure_4h.get("bias") == "bearish": bear_votes += 1

    if bull_votes > bear_votes:
        sig.direction_bias = "PRE_PUMP 🔋"
    elif bear_votes > bull_votes:
        sig.direction_bias = "PRE_DUMP ⚠️"
    else:
        sig.direction_bias = "蓄勢中 ⏳"

    # 信心度: 同時觸發越多訊號越可信
    active_signals = sum(1 for v in [
        sig.oi_buildup, sig.funding_divergence, sig.bb_squeeze,
        sig.vol_stairs, sig.ls_velocity, sig.cvd_divergence,
        sig.sleep_wake, sig.vol_expansion, sig.mtf_align,
    ] if v > 30)
    sig.confidence = min(1.0, active_signals / 4.0)

    sig.triggers = triggers
    return sig
