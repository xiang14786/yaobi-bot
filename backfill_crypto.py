"""
backfill_crypto.py — 加密貨幣歷史回填腳本
==========================================
從 Binance 公開 API 抓取歷史 K 線 + 資金費率，
計算近似特徵，標記 5 天後漲跌幅，輸出 CSV。

執行環境：本機 Windows（Python 3.10+）
安裝依賴：pip install aiohttp pandas tqdm

使用方式：
    python backfill_crypto.py
輸出：
    ml_crypto.csv（可直接 import 到 yaobi.db）
"""

import asyncio
import csv
import time
import math
from datetime import datetime, timezone

import aiohttp
import pandas as pd
from tqdm import tqdm

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_API  = "https://api.binance.com"

# 回填天數
LOOKBACK_DAYS = 180
# 每批並發幣數
CONCURRENCY   = 5
# 5 天後標記
LABEL_DAYS    = 5

OUTPUT_FILE   = "ml_crypto.csv"

STABLE_TOKENS = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDT", "USDE"}

FIELDNAMES = [
    "symbol", "market", "scan_ts",
    "total_score", "early_score", "confidence",
    "price", "change_pct",
    "feat1",   # funding_rate
    "feat2",   # long_short_ratio (用 taker 買賣比近似)
    "feat3",   # top_trader (用 BB 壓縮程度近似)
    "feat4",   # oi_change_pct (用成交量比近似)
    "outcome_pct", "outcome_label", "labeled_ts",
]


async def get_top_symbols(session: aiohttp.ClientSession, n=50) -> list[str]:
    """取成交量前 N 大的永續合約幣種"""
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json()
    # 過濾 USDT 合約，排除穩定幣
    pairs = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and d["symbol"][:-4] not in STABLE_TOKENS
        and float(d["quoteVolume"]) > 1e7
    ]
    pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [p["symbol"] for p in pairs[:n]]


async def get_klines(session: aiohttp.ClientSession, symbol: str,
                     interval="1d", limit=200) -> list[dict]:
    """取日 K 線"""
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        result = []
        for k in data:
            result.append({
                "ts":     int(k[0]) // 1000,  # open time → unix
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            })
        return result
    except Exception as e:
        print(f"  [WARN] {symbol} klines 失敗: {e}")
        return []


async def get_funding_history(session: aiohttp.ClientSession,
                               symbol: str, limit=200) -> dict[int, float]:
    """取資金費率歷史，回傳 {date_ts: rate}"""
    url = f"{BINANCE_FAPI}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        result = {}
        for item in data:
            # fundingTime → 取日期（取整天）
            day_ts = (int(item["fundingTime"]) // 1000 // 86400) * 86400
            result[day_ts] = float(item["fundingRate"])
        return result
    except Exception as e:
        print(f"  [WARN] {symbol} funding 失敗: {e}")
        return {}


async def get_oi_history(session: aiohttp.ClientSession,
                          symbol: str, limit=200) -> dict[int, float]:
    """取 OI 歷史，回傳 {date_ts: oi_change_pct}（當日相對前一日變化率）"""
    url = f"{BINANCE_FAPI}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "1d", "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        if not isinstance(data, list) or not data:
            return {}
        result = {}
        prev_oi = None
        for item in data:
            day_ts = (int(item["timestamp"]) // 1000 // 86400) * 86400
            oi = float(item["sumOpenInterest"])
            if prev_oi and prev_oi > 0:
                result[day_ts] = (oi - prev_oi) / prev_oi  # OI 日變化率
            prev_oi = oi
        return result
    except Exception as e:
        print(f"  [WARN] {symbol} OI 失敗: {e}")
        return {}


def calc_bb_squeeze(closes: list[float], period=20) -> float:
    """計算 BB 壓縮程度（0~1，越小越壓縮）"""
    if len(closes) < period:
        return 0.5
    recent = closes[-period:]
    mean = sum(recent) / period
    std  = math.sqrt(sum((x - mean)**2 for x in recent) / period)
    bw   = (2 * std / mean) if mean else 0
    # 歷史比較：過去 50 天
    hist_bws = []
    for i in range(max(0, len(closes) - 50), len(closes) - period + 1):
        s = closes[i:i+period]
        m = sum(s) / period
        sd = math.sqrt(sum((x - m)**2 for x in s) / period)
        hist_bws.append((2 * sd / m) if m else 0)
    if not hist_bws:
        return 0.5
    pct = sum(1 for x in hist_bws if x > bw) / len(hist_bws)  # percentile (低=壓縮)
    return pct  # 低 = 壓縮


def simple_score(close: float, prev_close: float, volume: float,
                 avg_vol: float, bb_squeeze: float,
                 funding_rate: float) -> tuple[float, float, float]:
    """
    簡化評分（近似 yaobi_scorer）
    回傳 (total_score, early_score, confidence)
    """
    # 動能分 (0~30)
    chg = (close - prev_close) / prev_close if prev_close else 0
    momentum = min(30, max(0, 15 + chg * 200))

    # 量能分 (0~20)
    vol_ratio = volume / avg_vol if avg_vol else 1
    vol_score = min(20, vol_ratio * 10)

    # BB 壓縮分 (0~20, 越壓縮越高)
    bb_score = min(20, (1 - bb_squeeze) * 20)

    # 資金費率分 (0~15, 負費率有利多頭)
    fr_score = min(15, max(0, 7.5 - funding_rate * 5000))

    # 早分 (bb + vol)
    early_score = bb_score + vol_score * 0.5

    total = momentum + vol_score + bb_score + fr_score
    total = min(100, max(0, total))
    confidence = min(1.0, total / 80)

    return total, early_score, confidence


async def process_symbol(session: aiohttp.ClientSession,
                          symbol: str, semaphore: asyncio.Semaphore) -> list[dict]:
    """處理單一幣種，回傳每天的 ML 資料列"""
    async with semaphore:
        klines    = await get_klines(session, symbol, limit=LOOKBACK_DAYS + LABEL_DAYS + 30)
        funding   = await get_funding_history(session, symbol, limit=500)
        oi_hist   = await get_oi_history(session, symbol, limit=200)
        await asyncio.sleep(0.3)  # 避免 rate limit

    if len(klines) < LOOKBACK_DAYS:
        return []

    rows = []
    closes  = [k["close"]  for k in klines]
    volumes = [k["volume"] for k in klines]

    # 計算平均成交量（20日）
    for i in range(20, len(klines) - LABEL_DAYS):
        k       = klines[i]
        prev_k  = klines[i-1]
        avg_vol = sum(volumes[i-20:i]) / 20

        # 找當天資金費率 + OI 變化
        day_ts  = (k["ts"] // 86400) * 86400
        fr      = funding.get(day_ts, 0.0)
        if fr == 0.0:
            fr = funding.get(day_ts - 86400, 0.0)
        oi_chg  = oi_hist.get(day_ts, 0.0)
        if oi_chg == 0.0:
            oi_chg = oi_hist.get(day_ts - 86400, 0.0)

        bb_squeeze = calc_bb_squeeze(closes[:i+1])

        total, early, conf = simple_score(
            k["close"], prev_k["close"],
            k["volume"], avg_vol,
            bb_squeeze, fr
        )

        # 5 天後漲跌幅
        future_k    = klines[i + LABEL_DAYS]
        outcome_pct = (future_k["close"] - k["close"]) / k["close"] * 100

        if outcome_pct > 3:
            label = 1
        elif outcome_pct < -3:
            label = -1
        else:
            label = 0

        # feat1=funding_rate, feat2=vol_ratio, feat3=bb_squeeze_score, feat4=oi_change(0=無資料)
        rows.append({
            "symbol":       symbol,
            "market":       "crypto",
            "scan_ts":      k["ts"],
            "total_score":  round(total, 2),
            "early_score":  round(early, 2),
            "confidence":   round(conf, 3),
            "price":        k["close"],
            "change_pct":   round((k["close"] - prev_k["close"]) / prev_k["close"] * 100, 3),
            "feat1":        round(fr, 6),
            "feat2":        round(k["volume"] / avg_vol if avg_vol else 1, 3),
            "feat3":        round(1 - bb_squeeze, 3),
            "feat4":        round(oi_chg, 6),  # OI 日變化率
            "outcome_pct":  round(outcome_pct, 3),
            "outcome_label": label,
            "labeled_ts":   int(time.time()),
        })
    return rows


async def main():
    print("=== 加密貨幣歷史回填 ===")
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        print("取得前 50 大幣種...")
        symbols = await get_top_symbols(session, n=50)
        print(f"共 {len(symbols)} 個幣種: {symbols[:5]}...")

        semaphore = asyncio.Semaphore(CONCURRENCY)
        all_rows  = []

        for sym in tqdm(symbols, desc="處理中"):
            rows = await process_symbol(session, sym, semaphore)
            all_rows.extend(rows)
            await asyncio.sleep(0.1)

    print(f"\n共 {len(all_rows)} 筆資料，寫入 {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    # 統計
    df = pd.read_csv(OUTPUT_FILE)
    print(f"完成！")
    print(f"  總筆數: {len(df)}")
    print(f"  幣種數: {df['symbol'].nunique()}")
    print(f"  標籤分布: {df['outcome_label'].value_counts().to_dict()}")
    print(f"  漲>3%: {(df['outcome_label']==1).sum()}, 平: {(df['outcome_label']==0).sum()}, 跌<-3%: {(df['outcome_label']==-1).sum()}")
    print(f"\n下一步：python import_ml_csv.py ml_crypto.csv")


if __name__ == "__main__":
    asyncio.run(main())
