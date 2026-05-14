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
  - 未登入：每分鐘 30 次請求
  - 免費帳號 token：每分鐘 600 次請求（強烈建議申請！）
  - 申請網址：https://finmindtrade.com → 右上角 Register
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

# ──────────────────────────────────────────────
#  全域 API 限流控制
#  無 token：每分鐘 30 次 → 每次至少間隔 2.0 秒，並行上限 1
#  有 token：每分鐘 600 次 → 每次間隔 0.15 秒，並行上限 5
# ──────────────────────────────────────────────
def _get_rate_config():
    if FINMIND_TOKEN:
        return dict(sem_count=5, delay=0.15)
    else:
        return dict(sem_count=1, delay=2.1)

# 懶初始化（第一次 fetch 時建立，避免事件循環問題）
_api_sem: asyncio.Semaphore | None = None
_api_delay: float = 2.1

def _init_rate_limiter():
    global _api_sem, _api_delay
    cfg = _get_rate_config()
    if _api_sem is None:
        _api_sem = asyncio.Semaphore(cfg["sem_count"])
        _api_delay = cfg["delay"]
        mode = "有 token（600次/分）" if FINMIND_TOKEN else "無 token（30次/分）"
        log.info(f"[TW] FinMind 限流模式：{mode}，並行={cfg['sem_count']}，間隔={cfg['delay']}s")


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
    # 資料日期
    data_date:  str  = ""
    # 狀態
    fetch_ok:   bool = True
    error_msg:  str  = ""


# ──────────────────────────────────────────────
#  FinMind 通用請求（含限流 + 自動重試）
# ──────────────────────────────────────────────
async def _finmind_get(
    session: aiohttp.ClientSession,
    dataset: str,
    stock_id: str,
    start_date: str,
    retries: int = 3,
) -> list[dict]:
    """
    呼叫 FinMind API，回傳 data 陣列。
    使用全域 semaphore 限流，並在遇到 rate limit 時自動重試。
    """
    global _api_sem, _api_delay
    _init_rate_limiter()

    params = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    for attempt in range(retries):
        async with _api_sem:  # 全域限流：同時最多 N 個 API 呼叫
            try:
                async with session.get(
                    FINMIND_BASE, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 429:
                        wait = 60 * (attempt + 1)
                        log.warning(f"[TW] Rate limit (429) {dataset} {stock_id}，等 {wait}s 後重試")
                        await asyncio.sleep(wait)
                        continue

                    if resp.status != 200:
                        log.warning(f"[TW] FinMind {dataset} {stock_id} HTTP {resp.status}")
                        return []

                    j = await resp.json()
                    status = j.get("status")

                    # FinMind 回傳 status=402 或 msg 含 rate limit 相關文字時重試
                    if status == 402 or (isinstance(j.get("msg"), str) and "Limit" in j.get("msg", "")):
                        wait = 60 * (attempt + 1)
                        log.warning(f"[TW] FinMind rate limit {dataset} {stock_id} msg={j.get('msg')}，等 {wait}s")
                        await asyncio.sleep(wait)
                        continue

                    if status != 200:
                        log.warning(f"[TW] FinMind {dataset} {stock_id} status={status} msg={j.get('msg')}")
                        return []

                    data = j.get("data", [])
                    # 加入 API 間隔，避免過快打下一個請求
                    await asyncio.sleep(_api_delay)
                    return data

            except asyncio.TimeoutError:
                log.warning(f"[TW] FinMind timeout {dataset} {stock_id}（第{attempt+1}次）")
                await asyncio.sleep(5 * (attempt + 1))
            except Exception as e:
                log.error(f"[TW] FinMind {dataset} {stock_id} error: {e}")
                return []

    log.error(f"[TW] FinMind {dataset} {stock_id} 重試 {retries} 次後仍失敗")
    return []


# ──────────────────────────────────────────────
#  個股完整資料抓取（三個資料集改為循序，保護限流）
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

    注意：三個資料集改為循序呼叫（sequential），
    避免同時打出多個 API 請求超過 rate limit。
    """
    result = TwStockData(stock_id=stock_id, name=name or stock_id)
    # 往前多抓 45 天，確保即使今日資料未發佈，仍能取到最新歷史資料
    start  = (date.today() - timedelta(days=days + 45)).isoformat()

    # ── 循序抓三組資料（不用 asyncio.gather，保護 rate limit）──
    price_data = await _finmind_get(session, "TaiwanStockPrice", stock_id, start)
    inst_data  = await _finmind_get(session, "TaiwanStockInstitutionalInvestors", stock_id, start)
    margin_data= await _finmind_get(session, "TaiwanStockMarginPurchaseShortSale", stock_id, start)

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
    result.data_date   = latest.get("date", "")
    result.close       = float(latest.get("close", 0))
    result.open_       = float(latest.get("open", 0))
    result.high        = float(latest.get("max", 0))
    result.low         = float(latest.get("min", 0))
    result.volume      = int(latest.get("Trading_Volume", 0))
    result.trade_value = float(latest.get("Trading_money", 0))   # FinMind 欄位小寫 m

    # 計算漲跌幅
    if len(result.closes) >= 2 and result.closes[-2] > 0:
        result.change_pct = (result.closes[-1] - result.closes[-2]) / result.closes[-2] * 100

    # ── 處理三大法人 ────────────────────────
    if inst_data:
        inst_data = sorted(inst_data, key=lambda x: x["date"])
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
    else:
        log.warning(f"[TW] {stock_id} 三大法人資料為空（可能仍在抓取中或 rate limited）")

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
    else:
        log.warning(f"[TW] {stock_id} 融資融券資料為空（可能仍在抓取中或 rate limited）")

    return result


# ──────────────────────────────────────────────
#  取得掃描股票清單（成交量前 N 大，含昨日 fallback）
# ──────────────────────────────────────────────
async def fetch_top_stocks_by_volume(
    session: aiohttp.ClientSession,
    top_n: int = 80
) -> list[tuple[str, str]]:
    """
    從 TWSE 取得成交量前 N 大的股票代號與名稱。
    TWSE MI_INDEX20 只有收盤後（約 14:30 後）才有當日資料，
    盤中或假日會自動嘗試前一個交易日。
    回傳 list of (stock_id, name)
    """
    # 依序嘗試今日、昨日、前兩日（應對假日/資料延遲）
    for delta in range(3):
        try_date = (date.today() - timedelta(days=delta)).strftime("%Y%m%d")
        url = (
            f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
            f"?date={try_date}&type=IND&response=json"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                j = await resp.json(content_type=None)
                rows = j.get("data", [])
                if not rows:
                    continue  # 該日無資料（例如假日），試前一天

                stocks = []
                for row in rows[:top_n]:
                    if len(row) >= 3:
                        sid  = str(row[1]).strip()
                        name = str(row[2]).strip()
                        # 只保留 4 碼純數字股票（排除 ETF、特殊股）
                        if sid.isdigit() and len(sid) == 4:
                            stocks.append((sid, name))

                if stocks:
                    log.info(f"[TW] TWSE 成交量排行：{try_date}，取得 {len(stocks)} 支")
                    return stocks

        except Exception as e:
            log.warning(f"[TW] TWSE 成交量排行 {try_date} 失敗: {e}")

    # Fallback：使用台灣 50 核心清單
    log.warning("[TW] TWSE API 均無資料，改用 CORE_TW50_STOCKS 預設清單")
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
    ("2912", "統一超"), ("3037", "欣興"), ("6669", "緯穎"),
    ("2337", "旺宏"), ("3231", "緯創"),
]


# ──────────────────────────────────────────────
#  主掃描函式
# ──────────────────────────────────────────────
async def fetch_all_tw_stocks(top_n: int = 50) -> list[TwStockData]:
    """
    掃描台股成交量前 top_n 大的股票，回傳完整資料列表。

    FinMind 限流策略：
      無 token → 每次 API 呼叫間隔 2.1s，串行（並行=1）
      有 token → 每次間隔 0.15s，並行最多 5 支股票

    無 token 掃描 20 支股票約 2 分鐘，50 支約 5 分鐘。
    強烈建議申請 FinMind 免費 token（600次/分）。
    """
    _init_rate_limiter()
    cfg = _get_rate_config()
    log.info(f"[TW] 開始掃描台股 top {top_n}（並行={cfg['sem_count']}，間隔={cfg['delay']}s）...")

    # 股票層級的並行控制（獨立於 API 限流 semaphore）
    stock_sem = asyncio.Semaphore(cfg["sem_count"])

    async def fetch_with_limit(session, sid, name):
        async with stock_sem:
            return await fetch_one_stock(session, sid, name)

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        stock_list = await fetch_top_stocks_by_volume(session, top_n)
        log.info(f"[TW] 取得 {len(stock_list)} 支股票，開始抓資料...")

        tasks   = [fetch_with_limit(session, sid, name) for sid, name in stock_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    stocks = []
    fail_count = 0
    for r in results:
        if isinstance(r, TwStockData):
            if r.fetch_ok and r.close > 0:
                stocks.append(r)
            else:
                fail_count += 1
                log.debug(f"[TW] 跳過 {r.stock_id}：{r.error_msg}")
        elif isinstance(r, Exception):
            fail_count += 1
            log.error(f"[TW] 抓取異常: {r}")

    log.info(f"[TW] 掃描完成，有效 {len(stocks)} 支，失敗 {fail_count} 支")
    return stocks


# ──────────────────────────────────────────────
#  快速測試
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)

    async def test():
        print(f"FINMIND_TOKEN = {'已設定' if FINMIND_TOKEN else '未設定（使用免費限流模式，會較慢）'}")
        stocks = await fetch_all_tw_stocks(top_n=5)
        for s in stocks:
            print(f"\n=== {s.stock_id} {s.name} ===")
            print(f"  資料日期: {s.data_date}")
            print(f"  收盤: {s.close}  漲跌: {s.change_pct:+.2f}%")
            print(f"  成交金額: {s.trade_value/1e8:.2f} 億")
            print(f"  外資: {s.foreign_net:+,.0f}  連續: {s.foreign_streak:+d} 天")
            print(f"  三大法人合計: {s.institutional_net:+,.0f}")
            print(f"  融資餘額: {s.margin_balance:,}  變化: {s.margin_change_pct:+.1f}%")
            print(f"  融券餘額: {s.short_balance:,}  變化: {s.short_change_pct:+.1f}%")
            print(f"  K 線筆數: {len(s.closes)}")

    asyncio.run(test())
