"""
妖幣框架 V2 (整合提早預警 + OB/FVG + 多時框)
=============================================
新增維度 (在原 7 維基礎上):
  8. 結構分數 (OB + FVG)
  9. 提早預警 (10 個領先子指標)
 10. 多時框共振

資料來源: 幣安公開 API (合約 + K 線)
"""

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp

from structure_analyzer import (
    parse_klines, score_structure, Kline,
)
from leading_indicators import (
    compute_leading_signals, LeadingSignals,
)


BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_API = "https://api.binance.com"

# 升級後權重 - 領先指標佔比最高
WEIGHTS_V2 = {
    "early_warning":  0.30,   # 提早預警 (最關鍵)
    "structure":      0.18,   # OB + FVG 結構
    "price_momentum": 0.08,   # 降低,因為已動 = 已晚
    "volume_anomaly": 0.10,
    "funding_rate":   0.10,
    "long_short":     0.06,
    "liquidation":    0.06,
    "sentiment":      0.06,
    "on_chain":       0.06,
}

DEFAULT_FILTERS_V2 = {
    "min_total_score": 55,
    "min_early_score": 40,           # 必須有領先訊號
    "max_price_change_pct": 15.0,    # 已漲跌 >15% 視為「已晚」
    "min_quote_volume_usd": 3e7,
    "exclude_stablecoins": True,
}

STABLE_TOKENS = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDT"}

# 簡易記憶體歷史 (儲存近 N 期 OI/費率/多空比 用於計算變化率)
_HISTORY_CACHE: dict = {}
_HISTORY_LEN = 12


@dataclass
class CoinMetricsV2:
    symbol: str
    base: str
    last_price: float = 0.0
    price_change_pct: float = 0.0
    quote_volume: float = 0.0
    market_cap: float = 0.0
    funding_rate: float = 0.0
    long_short_ratio: float = 1.0
    taker_buy_sell_ratio: float = 1.0
    liquidation_24h: float = 0.0
    open_interest_usd: float = 0.0

    # 原 7 維分
    score_price: float = 0.0
    score_volume: float = 0.0
    score_funding: float = 0.0
    score_ls: float = 0.0
    score_liq: float = 0.0
    score_sentiment: float = 0.0
    score_onchain: float = 0.0

    # V2 新增
    score_structure: float = 0.0
    score_early: float = 0.0
    structure_bias: str = "neutral"
    early_bias: str = "NEUTRAL"
    confidence: float = 0.0
    triggers: list = field(default_factory=list)
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0

    total_score: float = 0.0
    direction: str = "NEUTRAL"
    tags: list = field(default_factory=list)


class BinanceClientV2:
    def __init__(self, session):
        self.s = session

    async def _get(self, url, params=None):
        async with self.s.get(url, params=params, timeout=15) as r:
            r.raise_for_status()
            return await r.json()

    async def get_24h_tickers(self):
        return await self._get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr")

    async def get_premium_index(self):
        return await self._get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex")

    async def get_open_interest(self, symbol):
        return await self._get(
            f"{BINANCE_FAPI}/fapi/v1/openInterest", {"symbol": symbol}
        )

    async def get_oi_history(self, symbol, period="15m", limit=12):
        """歷史 OI (用於計算 OI 變化率)。"""
        try:
            return await self._get(
                f"{BINANCE_FAPI}/futures/data/openInterestHist",
                {"symbol": symbol, "period": period, "limit": limit},
            )
        except Exception:
            return []

    async def get_funding_history(self, symbol, limit=12):
        try:
            return await self._get(
                f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                {"symbol": symbol, "limit": limit},
            )
        except Exception:
            return []

    async def get_long_short_history(self, symbol, period="15m", limit=12):
        try:
            return await self._get(
                f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": period, "limit": limit},
            )
        except Exception:
            return []

    async def get_taker_history(self, symbol, period="15m", limit=20):
        try:
            return await self._get(
                f"{BINANCE_FAPI}/futures/data/takerlongshortRatio",
                {"symbol": symbol, "period": period, "limit": limit},
            )
        except Exception:
            return []

    async def get_klines(self, symbol, interval="15m", limit=100):
        return await self._get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )


# ============================================================
# 原 7 維評分函式 (從 yaobi_scorer 引入)
# ============================================================
def score_price_momentum(pct):
    a = abs(pct)
    if a < 3: return 0
    return min(100, 25 * math.log1p(a / 3) * 1.4)


def score_volume_anomaly(qv, mcap):
    if mcap <= 0:
        return min(100, math.log10(max(qv, 1) / 1e6) * 20)
    return min(100, qv / mcap * 200)


def score_funding(fr):
    a = abs(fr)
    if a < 0.0001: return 0
    return min(100, a * 60000)


def score_long_short(r):
    if r <= 0: return 0
    return min(100, abs(math.log(r)) * 100)


def score_liquidation(liq, oi):
    if oi <= 0:
        return min(100, math.log10(max(liq, 1) / 1e6) * 25)
    return min(100, liq / oi * 2000)


def score_sentiment(tr):
    if tr <= 0: return 0
    s = tr / (tr + 1)
    return min(100, abs(s - 0.5) * 333)


def score_onchain_proxy(vol, pc):
    return min(100, math.log10(max(vol, 1) / 1e6) * 8 + min(20, abs(pc)))


# ============================================================
# 主流程 V2
# ============================================================
async def fetch_all_metrics_v2(top_n: int = 50) -> list[CoinMetricsV2]:
    """
    V2 主流程:
    1. 抓 24h 行情 + 資金費率
    2. 取成交額前 N 名做深度分析
    3. 並行抓 K 線 / OI 歷史 / 費率歷史 / 多空比歷史 / taker 歷史
    4. 計算 OB+FVG + 領先指標
    """
    async with aiohttp.ClientSession() as session:
        client = BinanceClientV2(session)

        tickers, premiums = await asyncio.gather(
            client.get_24h_tickers(),
            client.get_premium_index(),
        )

        funding_map = {p["symbol"]: float(p.get("lastFundingRate", 0)) for p in premiums}

        # 過濾
        filtered = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT", "")
            if base in STABLE_TOKENS:
                continue
            qv = float(t.get("quoteVolume", 0))
            if qv < 1e6:
                continue
            filtered.append((t, base, sym, qv))

        filtered.sort(key=lambda x: x[3], reverse=True)
        filtered = filtered[:top_n]

        async def deep_analyze(item):
            t, base, sym, qv = item
            m = CoinMetricsV2(symbol=sym, base=base)
            m.last_price = float(t.get("lastPrice", 0))
            m.price_change_pct = float(t.get("priceChangePercent", 0))
            m.quote_volume = qv
            m.funding_rate = funding_map.get(sym, 0.0)

            # 並行抓多個資料源
            try:
                results = await asyncio.gather(
                    client.get_klines(sym, "15m", 100),
                    client.get_klines(sym, "1h", 100),
                    client.get_klines(sym, "4h", 60),
                    client.get_oi_history(sym, "15m", 12),
                    client.get_funding_history(sym, 12),
                    client.get_long_short_history(sym, "15m", 12),
                    client.get_taker_history(sym, "15m", 20),
                    client.get_open_interest(sym),
                    return_exceptions=True,
                )
                k15_raw, k1h_raw, k4h_raw, oi_hist, fund_hist, ls_hist, taker_hist, oi_now = results

                k15 = parse_klines(k15_raw) if isinstance(k15_raw, list) else []
                k1h = parse_klines(k1h_raw) if isinstance(k1h_raw, list) else []
                k4h = parse_klines(k4h_raw) if isinstance(k4h_raw, list) else []

                # OI 歷史(USDT 計價)
                oi_history = []
                if isinstance(oi_hist, list):
                    oi_history = [float(x.get("sumOpenInterestValue", 0)) for x in oi_hist]
                price_history = [k.close for k in k15[-12:]] if len(k15) >= 12 else []

                # 費率歷史
                funding_history = []
                if isinstance(fund_hist, list):
                    funding_history = [float(x.get("fundingRate", 0)) for x in fund_hist]

                # 多空比歷史
                ls_history = []
                if isinstance(ls_hist, list):
                    ls_history = [float(x.get("longShortRatio", 1)) for x in ls_hist]
                if ls_history:
                    m.long_short_ratio = ls_history[-1]

                # Taker 歷史
                taker_history = []
                if isinstance(taker_hist, list):
                    taker_history = [float(x.get("buySellRatio", 1)) for x in taker_hist]
                if taker_history:
                    m.taker_buy_sell_ratio = taker_history[-1]

                # 當前 OI
                if isinstance(oi_now, dict):
                    m.open_interest_usd = float(oi_now.get("openInterest", 0)) * m.last_price

                # === 結構分析 (OB + FVG) ===
                struct_15m = score_structure(k15, m.last_price) if k15 else None
                struct_1h = score_structure(k1h, m.last_price) if k1h else None
                struct_4h = score_structure(k4h, m.last_price) if k4h else None

                if struct_1h:
                    m.score_structure = struct_1h["score"]
                    m.structure_bias = struct_1h["bias"]
                    m.nearest_support = struct_1h.get("nearest_support") or 0
                    m.nearest_resistance = struct_1h.get("nearest_resistance") or 0

                # === 領先指標 ===
                leading = compute_leading_signals(
                    klines_15m=k15,
                    klines_1h=k1h,
                    klines_4h=k4h,
                    oi_history=oi_history,
                    price_history=price_history,
                    funding_history=funding_history,
                    ls_history=ls_history,
                    taker_history=taker_history,
                    structure_15m=struct_15m,
                    structure_1h=struct_1h,
                    structure_4h=struct_4h,
                )
                m.score_early = leading.early_score
                m.early_bias = leading.direction_bias
                m.confidence = leading.confidence
                m.triggers = leading.triggers

            except Exception as e:
                pass

            # 估算 (用於原 7 維)
            m.market_cap = max(m.open_interest_usd * 5, 1e6)
            m.liquidation_24h = (
                abs(m.funding_rate) * m.open_interest_usd * 4
                + abs(m.price_change_pct) * m.quote_volume * 0.001
            )

            # === 原 7 維 ===
            m.score_price = score_price_momentum(m.price_change_pct)
            m.score_volume = score_volume_anomaly(m.quote_volume, m.market_cap)
            m.score_funding = score_funding(m.funding_rate)
            m.score_ls = score_long_short(m.long_short_ratio)
            m.score_liq = score_liquidation(m.liquidation_24h, m.open_interest_usd)
            m.score_sentiment = score_sentiment(m.taker_buy_sell_ratio)
            m.score_onchain = score_onchain_proxy(m.quote_volume, m.price_change_pct)

            # === 加權總分 V2 ===
            m.total_score = (
                m.score_early     * WEIGHTS_V2["early_warning"]
                + m.score_structure * WEIGHTS_V2["structure"]
                + m.score_price     * WEIGHTS_V2["price_momentum"]
                + m.score_volume    * WEIGHTS_V2["volume_anomaly"]
                + m.score_funding   * WEIGHTS_V2["funding_rate"]
                + m.score_ls        * WEIGHTS_V2["long_short"]
                + m.score_liq       * WEIGHTS_V2["liquidation"]
                + m.score_sentiment * WEIGHTS_V2["sentiment"]
                + m.score_onchain   * WEIGHTS_V2["on_chain"]
            )

            # 方向判斷 - 優先參考 early_bias
            if "PRE_PUMP" in m.early_bias:
                m.direction = "🔋 蓄勢拉升"
            elif "PRE_DUMP" in m.early_bias:
                m.direction = "⚠️ 蓄勢下殺"
            elif m.price_change_pct > 8 and m.score_structure > 50:
                m.direction = "🚀 上漲延續"
            elif m.price_change_pct < -8 and m.score_structure > 50:
                m.direction = "📉 下跌延續"
            else:
                m.direction = "⏳ 觀察中"

            # 標籤
            if m.score_early > 60:
                m.tags.append("⚡ 強領先訊號")
            if abs(m.funding_rate) > 0.001:
                m.tags.append("極端費率")
            if m.long_short_ratio > 3 or m.long_short_ratio < 0.33:
                m.tags.append("多空失衡")
            if m.score_structure > 70:
                m.tags.append("結構共振")
            if abs(m.price_change_pct) < 5 and m.score_early > 50:
                m.tags.append("✨ 尚未啟動")
            if m.confidence > 0.7:
                m.tags.append("🎯 高信心")

            return m

        sem = asyncio.Semaphore(5)

        async def bounded(it):
            async with sem:
                return await deep_analyze(it)

        results = await asyncio.gather(*[bounded(it) for it in filtered])
        results.sort(key=lambda x: x.total_score, reverse=True)
        return results


# ============================================================
# 篩選器 V2
# ============================================================
def apply_filters_v2(coins: list[CoinMetricsV2], filters: dict = None) -> list[CoinMetricsV2]:
    f = {**DEFAULT_FILTERS_V2, **(filters or {})}
    out = []
    for c in coins:
        if f["exclude_stablecoins"] and c.base in STABLE_TOKENS:
            continue
        if c.total_score < f["min_total_score"]:
            continue
        if c.score_early < f["min_early_score"]:
            continue
        # 關鍵: 已經漲跌過大的不要
        if abs(c.price_change_pct) > f["max_price_change_pct"]:
            continue
        if c.quote_volume < f["min_quote_volume_usd"]:
            continue
        out.append(c)
    return out


def find_pre_pump(coins: list[CoinMetricsV2]) -> list[CoinMetricsV2]:
    """專找尚未啟動的預備暴漲幣。"""
    return [
        c for c in coins
        if "PRE_PUMP" in c.early_bias
        and c.score_early >= 45
        and abs(c.price_change_pct) < 8
        and c.confidence >= 0.4
    ]


def find_pre_dump(coins: list[CoinMetricsV2]) -> list[CoinMetricsV2]:
    """專找尚未啟動的預備暴跌幣。"""
    return [
        c for c in coins
        if "PRE_DUMP" in c.early_bias
        and c.score_early >= 45
        and abs(c.price_change_pct) < 8
        and c.confidence >= 0.4
    ]


def find_squeeze(coins: list[CoinMetricsV2]) -> list[CoinMetricsV2]:
    """專找 BB 壓縮 + 量能堆疊 (即將爆發)。"""
    out = []
    for c in coins:
        has_squeeze = any("BB 壓縮" in t for t in c.triggers)
        has_oi = any("OI 暴增" in t for t in c.triggers)
        has_stairs = any("階梯式" in t for t in c.triggers)
        if (has_squeeze or has_oi) and (has_stairs or has_oi):
            out.append(c)
    return out


# ============================================================
# CLI 測試
# ============================================================
async def _test():
    print("🔍 V2 引擎啟動...抓取 + 深度分析中")
    t0 = time.time()
    coins = await fetch_all_metrics_v2(top_n=30)
    print(f"完成! {time.time()-t0:.1f}s, 共 {len(coins)} 標的\n")

    print("=== 預備暴漲榜 (尚未啟動) ===")
    pre = find_pre_pump(coins)[:5]
    for c in pre:
        print(f"  {c.base:<10} 早分={c.score_early:>4.0f} 24h={c.price_change_pct:>+5.1f}% "
              f"信心={c.confidence:.0%} {c.direction}")
        for t in c.triggers[:3]:
            print(f"    └ {t}")


if __name__ == "__main__":
    asyncio.run(_test())
