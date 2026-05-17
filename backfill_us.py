"""
backfill_us.py — 美股歷史回填腳本
====================================
從 yfinance 抓取歷史日 K，計算近似特徵，
標記 5 天後漲跌幅，輸出 CSV。

執行環境：本機 Windows（Python 3.10+）
安裝依賴：pip install yfinance pandas tqdm

使用方式：
    python backfill_us.py
輸出：
    ml_us.csv（可直接 import 到 yaobi.db）

備註：
    法人持股 (inst_pct) 為靜態值（yfinance 不提供歷史），
    一律用當前值填入（近似）。
    RS Rating 以截至當日的相對強度計算。
"""

import csv
import math
import time
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd
from tqdm import tqdm

LOOKBACK_DAYS = 252  # 1 年（RS Rating 需要）
LABEL_DAYS    = 5
OUTPUT_FILE   = "ml_us.csv"

# 美股標的（與 bot 一致）
US_SYMBOLS = [
    # 科技
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD",
    "AVGO", "QCOM", "ORCL", "CRM", "ADBE", "NOW", "PANW", "ANET",
    "MU", "LRCX", "KLAC", "AMAT", "MRVL", "FTNT", "SNOW", "DDOG",
    "ZS", "CRWD",
    # 金融
    "JPM", "BAC", "GS", "MS", "V", "MA", "BX", "KKR", "APO",
    # 消費
    "AMZN", "COST", "LULU", "BKNG", "NKE",
    # 醫療
    "LLY", "UNH", "ABBV", "JNJ",
    # 工業/能源/防禦
    "CAT", "DE", "NOC", "GEV",
    # 加密/新興
    "COIN", "APP", "SPOT",
    # 指數 ETF（作為市場基準，不列入訓練）
]
# 去重
US_SYMBOLS = list(dict.fromkeys(US_SYMBOLS))

FIELDNAMES = [
    "symbol", "market", "scan_ts",
    "total_score", "early_score", "confidence",
    "price", "change_pct",
    "feat1",   # rs_rating
    "feat2",   # accum_score (A/D ratio)
    "feat3",   # momentum_score
    "feat4",   # inst_pct (靜態)
    "outcome_pct", "outcome_label", "labeled_ts",
]


def calc_rs_rating(closes: list[float], idx: int, spy_closes: list[float] = None) -> float:
    """
    RS Rating（IBD 風格）：計算截至 idx 的 1 年相對強度
    簡化版：用價格 1 年報酬率百分位（相對 US_SYMBOLS 整體）
    """
    if idx < 63:
        return 50.0
    p_now  = closes[idx]
    p_q1   = closes[max(0, idx - 63)]   # 3 個月前
    p_q2   = closes[max(0, idx - 126)]  # 6 個月前
    p_q3   = closes[max(0, idx - 189)]  # 9 個月前
    p_yr   = closes[max(0, idx - 252)]  # 1 年前
    # IBD 加權（最近 1 季佔比最高）
    r1 = (p_now / p_q1 - 1) * 40 if p_q1 else 0
    r2 = (p_now / p_q2 - 1) * 20 if p_q2 else 0
    r3 = (p_now / p_q3 - 1) * 20 if p_q3 else 0
    r4 = (p_now / p_yr  - 1) * 20 if p_yr  else 0
    return round(r1 + r2 + r3 + r4, 4)  # 原始強度，後面批次排名轉 0~99


def calc_ad_ratio(closes: list[float], volumes: list[float], idx: int, n=20) -> float:
    """A/D Ratio：上漲日量 / 下跌日量"""
    if idx < n:
        return 1.0
    up_vol = sum(volumes[i] for i in range(idx - n + 1, idx + 1) if closes[i] > closes[i-1])
    dn_vol = sum(volumes[i] for i in range(idx - n + 1, idx + 1) if closes[i] <= closes[i-1])
    return round(up_vol / dn_vol if dn_vol else 2.0, 3)


def calc_bb_score(closes: list[float], idx: int, period=20) -> float:
    """BB 壓縮分 0~20"""
    if idx < period + 30:
        return 5.0
    sub = closes[max(0, idx-period+1):idx+1]
    mean = sum(sub) / len(sub)
    std  = math.sqrt(sum((x-mean)**2 for x in sub) / len(sub))
    bw   = (2 * std / mean) if mean else 0
    hist_bws = []
    for j in range(max(0, idx-50), idx-period+2):
        s = closes[j:j+period]
        m = sum(s)/len(s)
        sd = math.sqrt(sum((x-m)**2 for x in s)/len(s))
        hist_bws.append((2*sd/m) if m else 0)
    if not hist_bws:
        return 5.0
    pct = sum(1 for x in hist_bws if x > bw) / len(hist_bws)
    return round(pct * 20, 2)


def calc_momentum_score(closes: list[float], volumes: list[float], idx: int) -> float:
    """動能分 0~20（RSI 近似 + 均線排列）"""
    if idx < 21:
        return 10.0
    # 簡易 RSI(14)
    gains = [max(0, closes[i] - closes[i-1]) for i in range(idx-13, idx+1)]
    losses = [max(0, closes[i-1] - closes[i]) for i in range(idx-13, idx+1)]
    avg_g = sum(gains) / 14 if gains else 0
    avg_l = sum(losses) / 14 if losses else 1
    rs    = avg_g / avg_l if avg_l else 0
    rsi   = 100 - 100 / (1 + rs)

    # MA 排列
    ma20 = sum(closes[idx-19:idx+1]) / 20 if idx >= 20 else closes[idx]
    ma60 = sum(closes[max(0,idx-59):idx+1]) / min(60, idx+1)
    above_ma20 = closes[idx] > ma20
    above_ma60 = closes[idx] > ma60

    score = 0
    if 50 < rsi <= 70:
        score += 10
    elif rsi <= 50:
        score += 4
    else:
        score += 6
    if above_ma20:
        score += 5
    if above_ma60:
        score += 5
    return min(20, max(0, score))


def simple_us_score(bb_score: float, ad_ratio: float,
                     momentum: float, inst_pct: float) -> tuple:
    """近似美股評分"""
    # BB (0~20), AD_ratio → accum (0~15), momentum (0~20), inst (0~10)
    accum = min(15, max(0, (ad_ratio - 1) * 10 + 7.5))
    inst  = min(10, inst_pct * 20)
    total = bb_score + accum + momentum + inst
    total = min(100, max(0, total))
    early = bb_score + momentum * 0.3
    conf  = min(1.0, total / 70)
    return round(total, 2), round(early, 2), round(conf, 3)


def process_symbol(ticker: str, hist: pd.DataFrame, inst_pct: float,
                    raw_rs_list: dict) -> list[dict]:
    """處理單一股票的歷史資料"""
    if len(hist) < 30:
        return []

    closes  = hist["Close"].tolist()
    volumes = hist["Volume"].tolist()
    dates   = hist.index.tolist()

    # 計算每日原始 RS strength（後面批次排名）
    rs_raw_by_date = {}
    for i in range(63, len(closes) - LABEL_DAYS):
        d = dates[i]
        rs_raw_by_date[d] = calc_rs_rating(closes, i)

    rows = []
    for i in range(63, len(closes) - LABEL_DAYS):
        d       = dates[i]
        close   = closes[i]
        prev_c  = closes[i-1]

        bb_sc   = calc_bb_score(closes, i)
        ad      = calc_ad_ratio(closes, volumes, i)
        mom     = calc_momentum_score(closes, volumes, i)
        rs_raw  = rs_raw_by_date.get(d, 0)

        # RS 百分位暫存原始值，批次處理後替換
        total, early, conf = simple_us_score(bb_sc, ad, mom, inst_pct)

        future = closes[i + LABEL_DAYS]
        outcome_pct = (future - close) / close * 100 if close else 0
        label = 1 if outcome_pct > 3 else (-1 if outcome_pct < -3 else 0)

        ts = int(pd.Timestamp(d).timestamp())

        rows.append({
            "symbol":       ticker,
            "market":       "us",
            "scan_ts":      ts,
            "total_score":  total,
            "early_score":  early,
            "confidence":   conf,
            "price":        round(close, 4),
            "change_pct":   round((close - prev_c) / prev_c * 100, 3) if prev_c else 0,
            "feat1":        rs_raw,        # raw RS，後面換成百分位
            "feat2":        round(ad, 3),  # A/D ratio
            "feat3":        round(mom, 2), # momentum_score
            "feat4":        round(inst_pct, 4),
            "outcome_pct":  round(outcome_pct, 3),
            "outcome_label": label,
            "labeled_ts":   int(time.time()),
        })
    return rows


def convert_rs_to_percentile(all_rows: list[dict]) -> list[dict]:
    """
    批次計算 RS Rating 百分位（0~99）
    同一天各股比較原始 RS，轉為百分位
    """
    # 按日期分組
    by_date: dict[int, list] = {}
    for r in all_rows:
        by_date.setdefault(r["scan_ts"], []).append(r)

    for ts, group in by_date.items():
        raw_vals = [r["feat1"] for r in group]
        n = len(raw_vals)
        for r in group:
            rank = sum(1 for v in raw_vals if v < r["feat1"])
            r["feat1"] = round(rank / n * 99, 1)  # RS Rating 0~99

    return all_rows


def main():
    print("=== 美股歷史回填 ===")
    symbols = US_SYMBOLS
    print(f"標的數: {len(symbols)}")

    start = (datetime.today() - timedelta(days=LOOKBACK_DAYS + LABEL_DAYS + 10)).strftime("%Y-%m-%d")
    end   = datetime.today().strftime("%Y-%m-%d")
    print(f"日期: {start} ~ {end}")

    # Bulk 下載
    print("bulk 下載歷史資料...")
    raw = yf.download(
        symbols,
        start=start,
        end=end,
        group_by="ticker",
        auto_adjust=True,
        progress=True,
        threads=True,
    )

    # 靜態法人持股
    print("取得法人持股...")
    inst_map = {}
    for sym in tqdm(symbols, desc="inst_pct"):
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            inst = getattr(info, "three_month_average_volume", None)
            # yfinance fast_info 沒有 inst_pct，用 institutionsPercentHeld
            try:
                idict = t.institutional_holders
                if idict is not None and not idict.empty:
                    pct = idict["% Out"].sum() / 100 if "% Out" in idict.columns else 0
                else:
                    pct = 0
            except Exception:
                pct = 0
            inst_map[sym] = min(1.0, pct)
        except Exception:
            inst_map[sym] = 0.0
        time.sleep(0.1)

    all_rows = []
    for sym in tqdm(symbols, desc="計算特徵"):
        try:
            if len(symbols) == 1:
                hist = raw
            else:
                if sym not in raw.columns.get_level_values(0):
                    continue
                hist = raw[sym].dropna(subset=["Close"])

            if len(hist) < 30:
                continue

            rows = process_symbol(sym, hist, inst_map.get(sym, 0.0), {})
            all_rows.extend(rows)
        except Exception as e:
            print(f"  [WARN] {sym}: {e}")

    # 轉換 RS Rating 為百分位
    print("計算 RS Rating 百分位...")
    all_rows = convert_rs_to_percentile(all_rows)

    print(f"\n共 {len(all_rows)} 筆資料，寫入 {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    df = pd.read_csv(OUTPUT_FILE)
    print("完成！")
    print(f"  總筆數: {len(df)}")
    print(f"  股票數: {df['symbol'].nunique()}")
    print(f"  漲>3%: {(df['outcome_label']==1).sum()}, 平: {(df['outcome_label']==0).sum()}, 跌<-3%: {(df['outcome_label']==-1).sum()}")
    print(f"\n下一步：python import_ml_csv.py ml_us.csv")


if __name__ == "__main__":
    main()
