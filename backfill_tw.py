"""
backfill_tw.py — 台股歷史回填腳本
===================================
從 FinMind 公開 API 抓取台股歷史法人資料 + 日 K，
計算近似特徵，標記 5 天後漲跌幅，輸出 CSV。

執行環境：本機 Windows（Python 3.10+）
安裝依賴：pip install requests pandas tqdm

使用方式：
    python backfill_tw.py
輸出：
    ml_tw.csv（可直接 import 到 yaobi.db）

備註：
    FinMind 免費版每日限制約 600 requests（需登入可到 1000+）
    若遇限制，隔天再跑或分批執行。
    FinMind token（選填）：設環境變數 FINMIND_TOKEN=xxx 可提高額度
"""

import csv
import math
import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
from tqdm import tqdm

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

LOOKBACK_DAYS = 180
LABEL_DAYS    = 5
OUTPUT_FILE   = "ml_tw.csv"

# 台股核心標的（覆蓋各產業）
TW_SYMBOLS = [
    "2330", "2317", "2454", "2412", "2308",  # 台積電、鴻海、聯發科、中華電、台達電
    "2881", "2882", "2884", "2886", "2891",  # 金控
    "2303", "2357", "2382", "2395", "2409",  # 聯電、華碩、廣達、研華、友達
    "3711", "3008", "2379", "2337", "3481",  # 日月光、大立光、瑞昱、旺宏、群創
    "6505", "1301", "1303", "2002", "2207",  # 台塑化、台塑、南亞、中鋼、和泰
    "2886", "5880", "2892", "2880", "2823",  # 金控補充
    "2345", "2376", "2385", "2388", "2404",  # 科技補充
    "3045", "4904", "6415", "6669",            # 電信、矽力、鉅亞
    "2603", "2615", "2609", "2610", "9910",  # 航運、航太、裕融
]
# 去重
TW_SYMBOLS = list(dict.fromkeys(s for s in TW_SYMBOLS if s.isdigit() or len(s) == 4))

FIELDNAMES = [
    "symbol", "market", "scan_ts",
    "total_score", "early_score", "confidence",
    "price", "change_pct",
    "feat1",   # foreign_net億
    "feat2",   # foreign_streak (連買天數)
    "feat3",   # score_bb 近似
    "feat4",   # margin_change_pct
    "outcome_pct", "outcome_label", "labeled_ts",
]


def _get(dataset: str, stock_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    """FinMind API 通用查詢"""
    params = {
        "dataset":  dataset,
        "data_id":  stock_id,
        "start_date": start_date,
        "end_date":   end_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        r = requests.get(FINMIND_API, params=params, timeout=15)
        data = r.json()
        if data.get("status") != 200:
            return pd.DataFrame()
        return pd.DataFrame(data.get("data", []))
    except Exception as e:
        print(f"  [WARN] {dataset} {stock_id}: {e}")
        return pd.DataFrame()


def get_price_data(stock_id: str, start: str, end: str) -> pd.DataFrame:
    df = _get("TaiwanStockPrice", stock_id, start, end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def get_institutional(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """三大法人"""
    df = _get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_margin(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """融資融券"""
    df = _get("TaiwanStockMarginPurchaseShortSale", stock_id, start, end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def calc_bb_score(closes: list[float], period=20) -> float:
    """BB 壓縮分 0~20"""
    if len(closes) < period:
        return 5.0
    recent = closes[-period:]
    mean   = sum(recent) / period
    std    = math.sqrt(sum((x-mean)**2 for x in recent) / period)
    bw     = (2 * std / mean) if mean else 0
    hist_bws = []
    for i in range(max(0, len(closes)-50), len(closes)-period+1):
        s = closes[i:i+period]
        m = sum(s)/period
        sd = math.sqrt(sum((x-m)**2 for x in s)/period)
        hist_bws.append((2*sd/m) if m else 0)
    if not hist_bws:
        return 5.0
    pct_rank = sum(1 for x in hist_bws if x > bw) / len(hist_bws)
    return round(pct_rank * 20, 2)


def calc_foreign_streak(foreign_nets: list[float], idx: int) -> int:
    """計算連續外資買超天數（負=連賣）"""
    if idx < 1:
        return 0
    streak = 0
    direction = 1 if foreign_nets[idx] > 0 else -1
    for i in range(idx, max(-1, idx-20), -1):
        if (foreign_nets[i] > 0) == (direction > 0):
            streak += direction
        else:
            break
    return streak


def simple_tw_score(close: float, prev_close: float,
                     foreign_net: float, foreign_streak: int,
                     bb_score: float, margin_chg: float) -> tuple:
    """近似台股評分"""
    # 法人分（0~35）
    inst_score = min(35, max(0, foreign_net / 1e8 * 5 + 17))  # 億元換算
    if foreign_streak >= 5:
        inst_score = min(35, inst_score + 10)

    # 融資分（0~15）
    margin_score = min(15, max(0, 7 + margin_chg * 30))

    # 動能分（0~20）
    chg = (close - prev_close) / prev_close if prev_close else 0
    momentum = min(20, max(0, 10 + chg * 100))

    # BB 分（已計算）
    total = inst_score + margin_score + momentum + bb_score * 0.5
    total = min(100, max(0, total))
    early = bb_score + momentum * 0.3
    conf  = min(1.0, total / 80)
    return round(total, 2), round(early, 2), round(conf, 3)


def process_symbol(stock_id: str, start: str, end: str) -> list[dict]:
    price_df = get_price_data(stock_id, start, end)
    time.sleep(0.4)  # 避免 rate limit
    inst_df  = get_institutional(stock_id, start, end)
    time.sleep(0.4)
    margin_df = get_margin(stock_id, start, end)
    time.sleep(0.4)

    if price_df.empty or len(price_df) < 20:
        return []

    # 整理法人資料（pivot 外資/投信）
    foreign_by_date = {}
    if not inst_df.empty and "name" in inst_df.columns:
        for _, row in inst_df.iterrows():
            d = row["date"]
            if row.get("name") in ("Foreign_Investor", "外資"):
                buy  = float(row.get("buy", 0) or 0)
                sell = float(row.get("sell", 0) or 0)
                foreign_by_date[d] = buy - sell

    # 整理融資資料
    margin_by_date = {}
    if not margin_df.empty:
        for _, row in margin_df.iterrows():
            d = row["date"]
            chg = float(row.get("MarginPurchaseBuy", 0) or 0) - float(row.get("MarginPurchaseSell", 0) or 0)
            margin_by_date[d] = chg

    rows     = []
    closes   = [float(r["close"]) for _, r in price_df.iterrows()]
    dates    = [r["date"]         for _, r in price_df.iterrows()]

    foreign_nets = []
    for d in dates:
        foreign_nets.append(foreign_by_date.get(d, 0.0))

    for i in range(20, len(price_df) - LABEL_DAYS):
        row      = price_df.iloc[i]
        prev_row = price_df.iloc[i-1]

        d            = dates[i]
        close        = float(row["close"])
        prev_close   = float(prev_row["close"])
        foreign_net  = foreign_by_date.get(d, 0.0)
        foreign_streak = calc_foreign_streak(foreign_nets, i)
        bb_sc        = calc_bb_score(closes[:i+1])

        # 融資變化（以張數近似）
        mc    = margin_by_date.get(d, 0.0)
        mbase = max(1, abs(float(row.get("volume", 1) or 1)))
        margin_chg = mc / mbase if mbase else 0

        total, early, conf = simple_tw_score(
            close, prev_close,
            foreign_net, foreign_streak,
            bb_sc, margin_chg
        )

        future_close = float(price_df.iloc[i + LABEL_DAYS]["close"])
        outcome_pct  = (future_close - close) / close * 100 if close else 0

        label = 1 if outcome_pct > 3 else (-1 if outcome_pct < -3 else 0)
        ts    = int(d.timestamp()) if hasattr(d, "timestamp") else int(pd.Timestamp(d).timestamp())

        rows.append({
            "symbol":       stock_id,
            "market":       "tw",
            "scan_ts":      ts,
            "total_score":  total,
            "early_score":  early,
            "confidence":   conf,
            "price":        close,
            "change_pct":   round((close - prev_close) / prev_close * 100, 3) if prev_close else 0,
            "feat1":        round(foreign_net / 1e8, 4),   # 億元
            "feat2":        foreign_streak,
            "feat3":        bb_sc,
            "feat4":        round(margin_chg, 4),
            "outcome_pct":  round(outcome_pct, 3),
            "outcome_label": label,
            "labeled_ts":   int(time.time()),
        })
    return rows


def main():
    print("=== 台股歷史回填 ===")
    end_date   = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=LOOKBACK_DAYS + 30)).strftime("%Y-%m-%d")

    symbols = TW_SYMBOLS
    print(f"標的數: {len(symbols)}，回填 {LOOKBACK_DAYS} 天")
    print(f"日期: {start_date} ~ {end_date}")
    if not FINMIND_TOKEN:
        print("[注意] 未設定 FINMIND_TOKEN，使用匿名額度（每日限 600 requests）")

    all_rows = []
    for sid in tqdm(symbols, desc="台股"):
        try:
            rows = process_symbol(sid, start_date, end_date)
            all_rows.extend(rows)
        except Exception as e:
            print(f"  [ERROR] {sid}: {e}")
        time.sleep(0.2)

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
    print(f"\n下一步：python import_ml_csv.py ml_tw.csv")


if __name__ == "__main__":
    main()
