"""
tw_data_fetcher.py
==================
台股資料抓取模組 — 使用 FinMind API（免費）

資料來源：
  - FinMind API (https://finmindtrade.com)
    → 股價 OHLCV、三大法人、融資融券
  - 台灣證交所 TWSE 開放 API
    → 當日成交量排行（不需 API Key）

FinMind 免費方案限制：
  - 每分鐘 30 次請求
  - 建議先到 https://finmindtrade.com 註冊取得 token，
    可提升到每分鐘 600 次（免費）
  - 把 token 設為環境變數 FINMIND_TOKEN

使用方式：
    from tw_data_fetcher import fetch_all_tw_stocks, TwStockData
    stocks = await fetch_all_tw_stocks(top_n=50)
"""

import os
import asyncio
import logging
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional

log = logging.getLogger("tw_fetcher")

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"
TWSE_RANKING  = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"

# ──────────────────────────────────────────────
#  資料結構
# ──────────────────────────────────────────────
@dataclass
class TwStockData:
    """單一台股的原始資料彙整"""
    stock_id:   str        # 股票代號，e.g. "2330"
    name:       str        # 股票名稱，e.g. "台積電"
    # 最新日價格
    close:      float = 0.0
    open_:      float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    volume:     int   = 0      # 成交股數
    trade_value: float = 0.0   # 成交金額（元）
    # 漲跌幅
    change_pct: float = 0.0    # 今日漲跌幅 %
    # K 線歷史（最近 60 日，用於技術指標）
    closes:     list[float] = field(default_factory=list)
    highs:      list[float] = field(default_factory=list)
    lows:       list[float] = field(default_factory=list)
    volumes:    list[int]   = field(default_factory=list)
    # 三大法人（最新一日，單位：元）
    foreign_net:   float = 0.0   # 外資淨買賣超
    trust_net:     float = 0.0   # 投信淨買賣超
    dealer_net:    float = 0.0   # 自營商淨買賣超
    institutional_net: float = 0.0  # 三大法人合計
    # 三大法人連續買超天數（正=買超，負=賣超）
    foreign_streak: int = 0
    # 融資融券（最新一日）
    margin_buy:    int   = 0     # 融資買進（股）
    margin_sell:   int   = 0     # 融資賣出
    margin_balance: int  = 0     # 融資餘額
    margin_change_pct: float = 0.0  # 融資餘額變化 %
    short_sell:    int   = 0     # 融券賣出
    short_balance: int   = 0     # 融券餘額
    short_change_pct: float = 0.0   # 融券餘額變化 %
    # 狀態
    fetch_ok:   bool = True
    error_msg:  str  = ""


# ──────────────────────────────────────────────
#  FinMind 通用請求
# ──────────────────────────────────────────────
async def _finmind_get(
    session: aiohttp.ClientSession,
    dataset: str,
    stock_id: str,
    start_date: str,
) -> list[dict]:
    """呼叫 FinMind API，回傳 data 陣列"""
    params = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        async with session.get(FINMIND_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning(f"FinMind {dataset} {stock_id} HTTP {resp.status}")
                return []
            j = await resp.json()
            if j.get("status") != 200:
                log.warning(f"FinMind {dataset} {stock_id} status={j.get('status')} msg={j.get('msg')}")
                return []
            return j.get("data", [])
    except Exception as e:
        log.error(f"FinMind {dataset} {stock_id} error: {e}")
        return []


# ──────────────────────────────────────────────
#  個股完整資料抓取
# ──────────────────────────────────────────────
async def fetch_one_stock(
    session: aiohttp.ClientSession,
    stock_id: str,
    name: str = "",
    days: int = 60,
) -> TwStockData:
    """
    抓取單一股票的所有需要資料：
      OHLCV + 三大法人 + 融資融券
    """
    result = TwStockData(stock_id=stock_id, name=name or stock_id)
    start  = (date.today() - timedelta(days=days + 30)).isoformat()

    # 並行抓三組資料
    price_task       = _finmind_get(session, "TaiwanStockPrice",                    stock_id, start)
    institution_task = _finmind_get(session, "TaiwanStockInstitutionalInvestors",   stock_id, start)
    margin_task      = _finmind_get(session, "TaiwanStockMarginPurchaseShortSale",  stock_id, start)

    price_data, inst_data, margin_data = await asyncio.gather(
        price_task, institution_task, margin_task
    )

    # ── 處理 OHLCV ──────────────────────────
    if not price_data:
        result.fetch_ok  = False
        result.error_msg = "無價格資料"
        return result

    # FinMind 資料為舊到新排列，取最近 days 筆
    price_data = sorted(price_data, key=lambda x: x["date"])[-days:]

    for row in price_data:
        try:
            result.closes.append(float(row["close"]))
            result.highs.append(float(row["max"]))
            result.lows.append(float(row["min"]))
            result.volumes.append(int(row["Trading_Volume"]))
        except Exception:
            continue

    if not result.closes:
        result.fetch_ok  = False
        result.error_msg = "OHLCV 解析失敗"
        return result

    latest = price_data[-1]
    result.close      = float(latest.get("close", 0))
    result.open_      = float(latest.get("open", 0))
    result.high       = float(latest.get("max", 0))
    result.low        = float(latest.get("min", 0))
    result.volume     = int(latest.get("Trading_Volume", 0))
    result.trade_value= float(latest.get("Trading_Money", 0))

    # 計算漲跌幅
    if len(result.closes) >= 2 and result.closes[-2] > 0:
        result.change_pct = (result.closes[-1] - result.closes[-2]) / result.closes[-2] * 100

    # ── 處理三大法人 ────────────────────────
    if inst_data:
        inst_data = sorted(inst_data, key=lambda x: x["date"])
        # 彙整最新一日三方向
        latest_date = inst_data[-1]["date"]
        daily = [r for r in inst_data if r["date"] == latest_date]
        for row in daily:
            name_field = row.get("name", "")
            net = float(row.get("buy", 0)) - float(row.get("sell", 0))
            if "外資" in name_field:
                result.foreign_net = net
            elif "投信" in name_field:
                result.trust_net = net
            elif "自營商" in name_field:
                result.dealer_net = net
        result.institutional_net = result.foreign_net + result.trust_net + result.dealer_net

        # 計算外資連續買超/賣超天數
        foreign_rows = [r for r in inst_data if "外資" in r.get("name", "")]
        streak = 0
        if foreign_rows:
            last_sign = None
            for row in reversed(foreign_rows):
                net = float(row.get("buy", 0)) - float(row.get("sell", 0))
                sign = 1 if net > 0 else -1
                if last_sign is None:
                    last_sign = sign
                if sign == last_sign:
                    streak += sign
                else:
                    break
        result.foreign_streak = streak

    # ── 處理融資融券 ────────────────────────
    if margin_data:
        margin_data = sorted(margin_data, key=lambda x: x["date"])
        latest_margin = margin_data[-1]
        result.margin_buy     = int(latest_margin.get("MarginPurchaseBuy", 0))
        result.margin_sell    = int(latest_margin.get("MarginPurchaseSell", 0))
        result.margin_balance = int(latest_margin.get("MarginPurchaseBalance", 0))
        result.short_sell     = int(latest_margin.get("ShortSaleSell", 0))
        result.short_balance  = int(latest_margin.get("ShortSaleBalance", 0))

        # 融資餘額變化 %（vs 5 日前）
        if len(margin_data) >= 6:
            prev = margin_data[-6]
            prev_bal = int(prev.get("MarginPurchaseBalance", 0))
            if prev_bal > 0:
                result.margin_change_pct = (result.margin_balance - prev_bal) / prev_bal * 100
            prev_short = int(prev.get("ShortSaleBalance", 0))
            if prev_short > 0:
                result.short_change_pct = (result.short_balance - prev_short) / prev_short * 100

    return result


# ──────────────────────────────────────────────
#  取得掃描股票清單（成交量前 N 大）
# ──────────────────────────────────────────────
async def fetch_top_stocks_by_volume(session: aiohttp.ClientSession, top_n: int = 80) -> list[tuple[str, str]]:
    """
    從 TWSE 取得當日成交量前 N 大的股票代號與名稱。
    回傳 list of (stock_id, name)
    """
    # TWSE 成交量排行
    today = date.today().strftime("%Y%m%d")
    url   = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20?date={today}&type=IND&response=json"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                j = await resp.json(content_type=None)
                rows = j.get("data", [])
                stocks = []
                for row in rows[:top_n]:
                    # row 格式：[排名, 代號, 名稱, ...]
                    if len(row) >= 3:
                        sid  = str(row[1]).strip()
                        name = str(row[2]).strip()
                        # 過濾 ETF（代號超過 4 碼或含英文通常是 ETF，可依需求調整）
                        if sid.isdigit() and len(sid) == 4:
                            stocks.append((sid, name))
                if stocks:
                    return stocks
    except Exception as e:
        log.warning(f"TWSE 成交量排行抓取失敗: {e}，改用預設清單")

    # Fallback：使用預設的台灣 50 成分股核心清單
    return CORE_TW50_STOCKS[:top_n]


# 台灣 50 核心成分股（Fallback 用）
CORE_TW50_STOCKS: list[tuple[str, str]] = [
    ("2330", "台積電"), ("2317", "鴻海"), ("2454", "聯發科"),
    ("2308", "台達電"), ("2382", "廣達"), ("2303", "聯電"),
    ("2412", "中華電"), ("3711", "日月光投控"), ("2002", "中鋼"),
    ("1301", "台塑"), ("1303", "南亞"), ("1326", "台化"),
    ("2886", "兆豐金"), ("2891", "中信金"), ("2881", "富邦金"),
    ("2882", "國泰金"), ("2884", "玉山金"), ("2885", "元大金"),
    ("2892", "第一金"), ("2880", "華南金"), ("2887", "台新金"),
    ("2357", "華碩"), ("2376", "技嘉"), ("2379", "瑞昱"),
    ("3034", "聯詠"), ("3008", "大立光"), ("2395", "研華"),
    ("6505", "台塑化"), ("9910", "豐泰"), ("2207", "和泰車"),
    ("2408", "南亞科"), ("3045", "台灣大"), ("4938", "和碩"),
    ("2352", "佳世達"), ("2474", "可成"), ("2615", "萬海"),
    ("2609", "陽明"), ("2603", "長榮"), ("5880", "合庫金"),
    ("2883", "開發金"), ("2890", "永豐金"), ("2888", "新光金"),
    ("1216", "統一"), ("1101", "台泥"), ("2105", "正新"),
    ("2912", "統一超"), ("3037", "欣興"), ("2395", "研華"),
    ("6669", "緯穎"), ("2337", "旺宏"),
]


# ──────────────────────────────────────────────
#  主掃描函式
# ──────────────────────────────────────────────
async def fetch_all_tw_stocks(top_n: int = 50) -> list[TwStockData]:
    """
    掃描台股成交量前 top_n 大的股票，回傳完整資料列表。

    注意：FinMind 免費版每分鐘 30 次，這裡用 semaphore 限流。
    建議設定 FINMIND_TOKEN 環境變數以提升限額。
    """
    log.info(f"[TW] 開始掃描台股 top {top_n}...")
    sem = asyncio.Semaphore(5)   # 同時最多 5 個並行請求

    async def fetch_with_limit(session, sid, name):
        async with sem:
            result = await fetch_one_stock(session, sid, name)
            await asyncio.sleep(0.3)  # 避免 rate limit
            return result

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 取得掃描清單
        stock_list = await fetch_top_stocks_by_volume(session, top_n)
        log.info(f"[TW] 取得 {len(stock_list)} 支股票，開始抓資料...")

        tasks   = [fetch_with_limit(session, sid, name) for sid, name in stock_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    stocks = []
    for r in results:
        if isinstance(r, TwStockData) and r.fetch_ok and r.close > 0:
            stocks.append(r)
        elif isinstance(r, Exception):
            log.error(f"[TW] 抓取異常: {r}")

    log.info(f"[TW] 掃描完成，有效股票 {len(stocks)} 支")
    return stocks


# ──────────────────────────────────────────────
#  快速測試
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def test():
        stocks = await fetch_all_tw_stocks(top_n=5)
        for s in stocks:
            print(f"\n=== {s.stock_id} {s.name} ===")
            print(f"  收盤: {s.close}  漲跌: {s.change_pct:+.2f}%")
            print(f"  外資: {s.foreign_net:+,.0f}  連續: {s.foreign_streak:+d} 天")
            print(f"  三大法人合計: {s.institutional_net:+,.0f}")
            print(f"  融資餘額變化: {s.margin_change_pct:+.1f}%")
            print(f"  K 線筆數: {len(s.closes)}")

    asyncio.run(test())
