"""
OB + FVG 結構偵測模組
=====================
ICT (Inner Circle Trader) 概念:
- Order Block (OB): 機構訂單區,反轉前的最後一根反向 K
- Fair Value Gap (FVG): 連續三根 K 線形成的不平衡缺口
- Breaker Block: 失效的 OB 反轉成支撐/阻力

回測邏輯:
- Bullish OB:  下跌趨勢中最後一根紅 K (空頭吸籌區)
- Bearish OB:  上漲趨勢中最後一根綠 K (多頭出貨區)
- Bullish FVG: K1.high < K3.low (向上跳空缺口)
- Bearish FVG: K1.low  > K3.high (向下跳空缺口)
"""

from dataclasses import dataclass
from typing import Literal


@dataclass
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class OrderBlock:
    type: Literal["bullish", "bearish"]
    top: float
    bottom: float
    index: int           # K 線索引位置
    strength: float      # 0~1, 越大越強
    mitigated: bool = False  # 是否已被觸碰

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class FairValueGap:
    type: Literal["bullish", "bearish"]
    top: float
    bottom: float
    index: int
    size_pct: float      # 缺口占價格百分比
    filled: bool = False

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


# ============================================================
# OB 偵測
# ============================================================
def detect_order_blocks(klines: list[Kline], lookback: int = 50,
                        min_displacement_pct: float = 1.0) -> list[OrderBlock]:
    """
    偵測未失效的 Order Blocks。

    Bullish OB: 一根紅 K 之後,出現強勢上推(displacement),
                這根紅 K 就是 bullish OB,標誌機構吸籌完成。
    Bearish OB: 反之。

    min_displacement_pct: 後續推動需超過此 % 才算有效 OB
    """
    if len(klines) < lookback + 3:
        return []

    obs = []
    n = len(klines)
    start = max(0, n - lookback)

    for i in range(start, n - 3):
        candle = klines[i]
        # 檢查後續 1~3 根是否形成強勢推動
        next_candles = klines[i+1:i+4]
        max_high = max(k.high for k in next_candles)
        min_low = min(k.low for k in next_candles)

        # Bullish OB: 紅 K 後價格大漲
        if not candle.is_bull:
            up_move = (max_high - candle.high) / candle.high * 100
            if up_move >= min_displacement_pct:
                ob = OrderBlock(
                    type="bullish",
                    top=candle.high,
                    bottom=candle.low,
                    index=i,
                    strength=min(1.0, up_move / 5.0),
                )
                # 檢查是否已被觸碰失效
                for k in klines[i+4:]:
                    if k.low <= ob.bottom:
                        ob.mitigated = True
                        break
                if not ob.mitigated:
                    obs.append(ob)

        # Bearish OB: 綠 K 後價格大跌
        if candle.is_bull:
            down_move = (candle.low - min_low) / candle.low * 100
            if down_move >= min_displacement_pct:
                ob = OrderBlock(
                    type="bearish",
                    top=candle.high,
                    bottom=candle.low,
                    index=i,
                    strength=min(1.0, down_move / 5.0),
                )
                for k in klines[i+4:]:
                    if k.high >= ob.top:
                        ob.mitigated = True
                        break
                if not ob.mitigated:
                    obs.append(ob)

    return obs


# ============================================================
# FVG 偵測
# ============================================================
def detect_fvgs(klines: list[Kline], lookback: int = 50,
                min_size_pct: float = 0.15) -> list[FairValueGap]:
    """
    偵測未填補的 Fair Value Gaps。

    Bullish FVG: K[i-1].high < K[i+1].low (中間 K 強勢拉升留下缺口)
    Bearish FVG: K[i-1].low  > K[i+1].high
    """
    if len(klines) < 3:
        return []

    fvgs = []
    n = len(klines)
    start = max(1, n - lookback)

    for i in range(start, n - 1):
        prev_k = klines[i-1]
        next_k = klines[i+1]

        # Bullish FVG
        if prev_k.high < next_k.low:
            gap_size = next_k.low - prev_k.high
            size_pct = gap_size / prev_k.high * 100
            if size_pct >= min_size_pct:
                fvg = FairValueGap(
                    type="bullish",
                    top=next_k.low,
                    bottom=prev_k.high,
                    index=i,
                    size_pct=size_pct,
                )
                # 檢查後續是否已填補
                for k in klines[i+2:]:
                    if k.low <= fvg.bottom:
                        fvg.filled = True
                        break
                if not fvg.filled:
                    fvgs.append(fvg)

        # Bearish FVG
        if prev_k.low > next_k.high:
            gap_size = prev_k.low - next_k.high
            size_pct = gap_size / prev_k.low * 100
            if size_pct >= min_size_pct:
                fvg = FairValueGap(
                    type="bearish",
                    top=prev_k.low,
                    bottom=next_k.high,
                    index=i,
                    size_pct=size_pct,
                )
                for k in klines[i+2:]:
                    if k.high >= fvg.top:
                        fvg.filled = True
                        break
                if not fvg.filled:
                    fvgs.append(fvg)

    return fvgs


# ============================================================
# 結構評分
# ============================================================
def score_structure(klines: list[Kline], current_price: float) -> dict:
    """
    綜合 OB + FVG 評估當前位置的結構強度。

    回傳:
    - score: 0~100 結構分
    - bias: bullish/bearish/neutral
    - nearest_ob: 最近的 OB
    - nearest_fvg: 最近的 FVG
    - signal: 文字訊號
    """
    obs = detect_order_blocks(klines)
    fvgs = detect_fvgs(klines)

    # 計算當前價格距離各個 OB / FVG 的位置
    bull_obs = [o for o in obs if o.type == "bullish" and current_price > o.top]
    bear_obs = [o for o in obs if o.type == "bearish" and current_price < o.bottom]
    bull_fvgs = [f for f in fvgs if f.type == "bullish"]
    bear_fvgs = [f for f in fvgs if f.type == "bearish"]

    # 找最近的支撐 (bullish OB 上方最近的)
    nearest_support = None
    if bull_obs:
        nearest_support = max(bull_obs, key=lambda o: o.top)

    # 找最近的阻力
    nearest_resistance = None
    if bear_obs:
        nearest_resistance = min(bear_obs, key=lambda o: o.bottom)

    # 偏多 vs 偏空訊號計分
    bull_strength = sum(o.strength for o in bull_obs) + sum(f.size_pct for f in bull_fvgs) * 0.5
    bear_strength = sum(o.strength for o in bear_obs) + sum(f.size_pct for f in bear_fvgs) * 0.5

    if bull_strength + bear_strength == 0:
        bias = "neutral"
        score = 0
    elif bull_strength > bear_strength * 1.3:
        bias = "bullish"
        score = min(100, bull_strength * 30)
    elif bear_strength > bull_strength * 1.3:
        bias = "bearish"
        score = min(100, bear_strength * 30)
    else:
        bias = "neutral"
        score = min(100, max(bull_strength, bear_strength) * 20)

    # 訊號
    signals = []
    if nearest_support and (current_price - nearest_support.top) / current_price < 0.02:
        signals.append("接近 Bullish OB (支撐區)")
    if nearest_resistance and (nearest_resistance.bottom - current_price) / current_price < 0.02:
        signals.append("接近 Bearish OB (阻力區)")

    # FVG 進場機會
    untouched_bull_fvg = [f for f in bull_fvgs if current_price > f.top]
    untouched_bear_fvg = [f for f in bear_fvgs if current_price < f.bottom]
    if untouched_bull_fvg:
        signals.append(f"上方 {len(untouched_bull_fvg)} 個未填 Bullish FVG")
    if untouched_bear_fvg:
        signals.append(f"下方 {len(untouched_bear_fvg)} 個未填 Bearish FVG")

    return {
        "score": score,
        "bias": bias,
        "ob_count": len(obs),
        "fvg_count": len(fvgs),
        "bull_obs": len([o for o in obs if o.type == "bullish"]),
        "bear_obs": len([o for o in obs if o.type == "bearish"]),
        "nearest_support": nearest_support.mid if nearest_support else None,
        "nearest_resistance": nearest_resistance.mid if nearest_resistance else None,
        "signals": signals,
    }


# ============================================================
# 輔助: 解析幣安 K 線資料
# ============================================================
def parse_klines(raw: list) -> list[Kline]:
    """幣安 K 線格式: [openTime, open, high, low, close, volume, ...]"""
    return [
        Kline(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
        )
        for k in raw
    ]
