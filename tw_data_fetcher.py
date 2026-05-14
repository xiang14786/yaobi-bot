"""
tw_data_fetcher.py
==================
台股資料抓取模組

資料來源：
  ┌─ FinMind API（需 token）
  │   → 股價 OHLCV（每支股票 1 次 API）
  │   → 申請免費 token：https://finmindtrade.com → Register
  │   → 環境變數：FINMIND_TOKEN
  │
  └─ 台灣證交所 TWSE 開放 API（完全免費，不需 token）
      → 三大法人（T86）：一次取全部股票，10 日歷史
      → 融資融券（MI_MARGN）：一次取全部股票，10 日歷史
      → 成交量排行（MI_INDEX20）

FinMind 免費方案限制：
  - 未登入：每分鐘 30 次 → 掃描 50 支約 5 分鐘
  - 免費 token：每分鐘 600 次 → 掃描 50 支約 30 秒

使用方式：
    from tw_data_fetcher import fetch_all_tw_stocks, TwStockData
    stocks = await fetch_all_tw_stocks(top_n=50)
"""

import os
import asyncio
import logging
import aiohttp
from dataclasses import dataclass, field
from datetime import date, timedelta

# TWSE 需要瀏覽器 User-Agent，否則會被擋
_TWSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer":         "https://www.twse.com.tw/zh/",
    "Connection":      "keep-alive",
}

log = logging.getLogger("tw_fetcher")

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"

# ──────────────────────────────────────────────
#  全域 API 限流（僅針對 FinMind，TWSE 不需要）
# ──────────────────────────────────────────────
def _get_rate_config():
    if FINMIND_TOKEN:
        return dict(sem_count=5, delay=0.15)
    else:
        return dict(sem_count=1, delay=2.1)

_api_sem: asyncio.Semaphore | None = None
_api_delay: float = 2.1

def _init_rate_limiter():
    global _api_sem, _api_delay
    cfg = _get_rate_config()
    if _api_sem is None:
        _api_sem = asyncio.Semaphore(cfg["sem_count"])
        _api_delay = cfg["delay"]
        mode = "有 token（600次/分）" if FINMIND_TOKEN else "無 token（30次/分）"
        log.info(f"[TW] FinMind 限流：{mode}，並行={cfg['sem_count']}，間隔={cfg['delay']}s")


# ──────────────────────────────────────────────
#  資料結構
# ──────────────────────────────────────────────
@dataclass
class TwStockData:
    stock_id:   str
    name:       str
    close:      float = 0.0
    open_:      float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    volume:     int   = 0
    trade_value: float = 0.0
    change_pct: float = 0.0
    closes:     list[float] = field(default_factory=list)
    highs:      list[float] = field(default_factory=list)
    lows:       list[float] = field(default_factory=list)
    volumes:    list[int]   = field(default_factory=list)
    # 三大法人（單位：元，= 股數 × 收盤價）
    foreign_net:        float = 0.0
    trust_net:          float = 0.0
    dealer_net:         float = 0.0
    institutional_net:  float = 0.0
    foreign_streak:     int   = 0
    # 融資融券
    margin_buy:         int   = 0
    margin_sell:        int   = 0
    margin_balance:     int   = 0
    margin_change_pct:  float = 0.0
    short_sell:         int   = 0
    short_balance:      int   = 0
    short_change_pct:   float = 0.0
    data_date:  str  = ""
    fetch_ok:   bool = True
    error_msg:  str  = ""


# ──────────────────────────────────────────────
#  工具函式
# ──────────────────────────────────────────────
def _parse_int(s) -> int:
    """把 TWSE 帶逗號的數字字串轉 int，失敗回傳 0"""
    try:
        return int(str(s).replace(",", "").replace("--", "0").strip() or 0)
    except Exception:
        return 0


def _recent_trading_dates(n: int = 10) -> list[str]:
    """回傳最近 n 個可能的交易日（含今日），格式 YYYYMMDD"""
    dates = []
    d = date.today()
    while len(dates) < n:
        if d.weekday() < 5:   # 排除週六(5)、週日(6)
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates


# ──────────────────────────────────────────────
#  TWSE 免費 API：三大法人（T86）
#  一次 API 呼叫取得所有上市股票當日三大法人資料
# ──────────────────────────────────────────────
async def _fetch_twse_t86_day(
    session: aiohttp.ClientSession,
    date_str: str,
) -> dict[str, dict]:
    """
    抓取單一日期的三大法人買賣超資料（全市場）。
    回傳 {stock_id: {"foreign": 股數, "trust": 股數, "dealer": 股數, "total": 股數}}
    股數可正（買超）可負（賣超）。
    """
    url = (
        f"https://www.twse.com.tw/exchangeReport/T86"
        f"?response=json&date={date_str}&selectType=ALLBUT0999"
    )
    try:
        async with session.get(url, headers=_TWSE_HEADERS, timeout=aiohttp.ClientTimeout(total=12), ssl=False) as resp:
            if resp.status != 200:
                log.warning(f"[TW] TWSE T86 {date_str} HTTP {resp.status}")
                return {}
            j = await resp.json(content_type=None)
            stat = j.get("stat", "")
            data_rows = j.get("data", [])
            if not data_rows:
                log.debug(f"[TW] TWSE T86 {date_str}: stat={stat!r}，無資料（可能非交易日）")
                return {}
            log.debug(f"[TW] TWSE T86 {date_str}: stat={stat!r}，{len(data_rows)} 列")
            result = {}
            for row in data_rows:
                if len(row) < 21:
                    continue
                sid = str(row[0]).strip()
                if not (sid.isdigit() and len(sid) == 4):
                    continue
                # T86 欄位（0-based，共 21 欄）：
                # [4]=外資及陸資買賣超, [10]=投信買賣超
                # [19]=自營商合計買賣超, [20]=三大法人買賣超合計
                result[sid] = {
                    "foreign": _parse_int(row[4]),
                    "trust":   _parse_int(row[10]),
                    "dealer":  _parse_int(row[19]),
                    "total":   _parse_int(row[20]),
                }
            log.info(f"[TW] TWSE T86 {date_str}: 解析 {len(result)} 支")
            return result
    except Exception as e:
        log.warning(f"[TW] TWSE T86 {date_str} 失敗: {e}")
        return {}


# ──────────────────────────────────────────────
#  TWSE 免費 API：融資融券（MI_MARGN）
#  一次 API 呼叫取得所有股票當日融資融券資料
# ──────────────────────────────────────────────
async def _fetch_twse_margin_day(
    session: aiohttp.ClientSession,
    date_str: str,
) -> dict[str, dict]:
    """
    抓取單一日期的融資融券資料（全市場）。
    回傳 {stock_id: {"margin_bal": 股數, "short_bal": 股數}}
    """
    url = (
        f"https://www.twse.com.tw/exchangeReport/MI_MARGN"
        f"?response=json&date={date_str}&selectType=MS"
    )
    try:
        async with session.get(url, headers=_TWSE_HEADERS, timeout=aiohttp.ClientTimeout(total=12), ssl=False) as resp:
            if resp.status != 200:
                log.warning(f"[TW] TWSE MI_MARGN {date_str} HTTP {resp.status}")
                return {}
            j = await resp.json(content_type=None)
            stat = j.get("stat", "")
            data_rows = j.get("data", [])
            if not data_rows:
                log.debug(f"[TW] TWSE MI_MARGN {date_str}: stat={stat!r}，無資料（可能非交易日）")
                return {}
            log.debug(f"[TW] TWSE MI_MARGN {date_str}: stat={stat!r}，{len(data_rows)} 列")
            result = {}
            for row in data_rows:
                if len(row) < 11:
                    continue
                sid = str(row[0]).strip()
                if not (sid.isdigit() and len(sid) == 4):
                    continue
                # MI_MARGN(MS) 欄位（0-based）：
                # [5]=融資餘額, [10]=融券餘額
                # 注意：[11]=融券限額（不是餘額！）
                result[sid] = {
                    "margin_bal": _parse_int(row[5]),
                    "short_bal":  _parse_int(row[10]),
                }
            log.info(f"[TW] TWSE MI_MARGN {date_str}: 解析 {len(result)} 支")
            return result
    except Exception as e:
        log.warning(f"[TW] TWSE MI_MARGN {date_str} 失敗: {e}")
        return {}


# ──────────────────────────────────────────────
#  批量抓取 TWSE 資料（多日，全股票）
# ──────────────────────────────────────────────
async def fetch_twse_bulk(
    session: aiohttp.ClientSession,
    days: int = 10,
) -> tuple[dict[str, list], dict[str, list]]:
    """
    抓取最近 days 個交易日的三大法人和融資融券資料。

    回傳：
        inst_history[stock_id]   = [{"date":..., "foreign":..., ...}, ...]  按日期由舊到新
        margin_history[stock_id] = [{"date":..., "margin_bal":..., ...}, ...] 按日期由舊到新

    TWSE API 每次呼叫包含全市場所有股票，
    所以 days 個交易日只需 2*days 次 API，
    比 FinMind 每支股票各自呼叫效率高很多。
    """
    trade_dates = _recent_trading_dates(days)
    log.info(f"[TW] 抓取 TWSE 三大法人 + 融資券，最近 {days} 個交易日...")

    # 並行抓所有日期（TWSE 無嚴格限流）
    t86_tasks  = [_fetch_twse_t86_day(session, d)    for d in trade_dates]
    mgn_tasks  = [_fetch_twse_margin_day(session, d) for d in trade_dates]

    t86_results  = await asyncio.gather(*t86_tasks,  return_exceptions=True)
    mgn_results  = await asyncio.gather(*mgn_tasks,  return_exceptions=True)

    # 整理成 {stock_id: [{date, 資料}, ...]} 的格式
    inst_history:   dict[str, list] = {}
    margin_history: dict[str, list] = {}

    for dt, t86 in zip(trade_dates, t86_results):
        if isinstance(t86, dict):
            for sid, vals in t86.items():
                inst_history.setdefault(sid, []).append({"date": dt, **vals})

    for dt, mgn in zip(trade_dates, mgn_results):
        if isinstance(mgn, dict):
            for sid, vals in mgn.items():
                margin_history.setdefault(sid, []).append({"date": dt, **vals})

    # 按日期由舊到新排序
    for sid in inst_history:
        inst_history[sid].sort(key=lambda x: x["date"])
    for sid in margin_history:
        margin_history[sid].sort(key=lambda x: x["date"])

    log.info(f"[TW] TWSE 批量資料：三大法人覆蓋 {len(inst_history)} 支，融資券 {len(margin_history)} 支")
    return inst_history, margin_history


# ──────────────────────────────────────────────
#  FinMind：OHLCV 價格資料（僅用於此）
# ──────────────────────────────────────────────
async def _finmind_price(
    session: aiohttp.ClientSession,
    stock_id: str,
    start_date: str,
    retries: int = 3,
) -> list[dict]:
    global _api_sem, _api_delay
    _init_rate_limiter()

    params = {
        "dataset":    "TaiwanStockPrice",
        "data_id":    stock_id,
        "start_date": start_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    for attempt in range(retries):
        async with _api_sem:
            try:
                async with session.get(
                    FINMIND_BASE, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 429:
                        wait = 60 * (attempt + 1)
                        log.warning(f"[TW] FinMind rate limit {stock_id}，等 {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        return []
                    j = await resp.json()
                    if j.get("status") == 402 or "Limit" in str(j.get("msg", "")):
                        wait = 60 * (attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if j.get("status") != 200:
                        return []
                    await asyncio.sleep(_api_delay)
                    return j.get("data", [])
            except asyncio.TimeoutError:
                await asyncio.sleep(5 * (attempt + 1))
            except Exception as e:
                log.error(f"[TW] FinMind price {stock_id} error: {e}")
                return []
    return []


# ──────────────────────────────────────────────
#  個股完整資料整合
# ──────────────────────────────────────────────
async def fetch_one_stock(
    session: aiohttp.ClientSession,
    stock_id: str,
    name: str = "",
    days: int = 60,
    inst_history:   dict | None = None,
    margin_history: dict | None = None,
) -> TwStockData:
    """
    抓取單一股票 OHLCV，並套用預先抓好的三大法人和融資券資料。
    inst_history / margin_history 由 fetch_twse_bulk() 提供。
    """
    result = TwStockData(stock_id=stock_id, name=name or stock_id)
    start  = (date.today() - timedelta(days=days + 45)).isoformat()

    # ── OHLCV（FinMind）────────────────────────
    price_data = await _finmind_price(session, stock_id, start)
    if not price_data:
        result.fetch_ok  = False
        result.error_msg = "無價格資料"
        return result

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
    result.trade_value = float(latest.get("Trading_money", 0))

    if len(result.closes) >= 2 and result.closes[-2] > 0:
        result.change_pct = (result.closes[-1] - result.closes[-2]) / result.closes[-2] * 100

    # ── 三大法人（TWSE T86，股數 → 換算成元）─────
    inst_hist = (inst_history or {}).get(stock_id, [])
    if inst_hist:
        latest_inst = inst_hist[-1]
        cp = result.close or 1.0
        # 股數 × 收盤價 = 約略金額（元）
        result.foreign_net        = latest_inst["foreign"] * cp
        result.trust_net          = latest_inst["trust"]   * cp
        result.dealer_net         = latest_inst["dealer"]  * cp
        result.institutional_net  = latest_inst["total"]   * cp

        # 外資連續買超天數
        streak, last_sign = 0, None
        for day in reversed(inst_hist):
            sign = 1 if day["foreign"] > 0 else -1
            if last_sign is None:
                last_sign = sign
            if sign == last_sign:
                streak += sign
            else:
                break
        result.foreign_streak = streak
    else:
        log.debug(f"[TW] {stock_id} 無三大法人資料（TWSE 可能尚未發佈）")

    # ── 融資融券（TWSE MI_MARGN）───────────────
    mgn_hist = (margin_history or {}).get(stock_id, [])
    if mgn_hist:
        latest_mgn = mgn_hist[-1]
        result.margin_balance = latest_mgn["margin_bal"]
        result.short_balance  = latest_mgn["short_bal"]

        # 5 日前對比（計算變化 %）
        if len(mgn_hist) >= 6:
            prev = mgn_hist[-6]
            if prev["margin_bal"] > 0:
                result.margin_change_pct = (
                    (result.margin_balance - prev["margin_bal"]) / prev["margin_bal"] * 100
                )
            if prev["short_bal"] > 0:
                result.short_change_pct = (
                    (result.short_balance - prev["short_bal"]) / prev["short_bal"] * 100
                )
    else:
        log.debug(f"[TW] {stock_id} 無融資融券資料（TWSE 可能尚未發佈）")

    return result


# ──────────────────────────────────────────────
#  取得掃描股票清單
# ──────────────────────────────────────────────
async def fetch_top_stocks_by_volume(
    session: aiohttp.ClientSession,
    top_n: int = 80,
) -> list[tuple[str, str]]:
    for delta in range(3):
        try_date = (date.today() - timedelta(days=delta)).strftime("%Y%m%d")
        url = (
            f"https://www.twse.com.tw/exchangeReport/MI_INDEX20"
            f"?response=json&date={try_date}&type=IND"
        )
        try:
            async with session.get(url, headers=_TWSE_HEADERS, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                if resp.status != 200:
                    continue
                j = await resp.json(content_type=None)
                rows = j.get("data", [])
                if not rows:
                    continue
                stocks = []
                for row in rows[:top_n]:
                    if len(row) >= 3:
                        sid  = str(row[1]).strip()
                        name = str(row[2]).strip()
                        if sid.isdigit() and len(sid) == 4:
                            stocks.append((sid, name))
                if stocks:
                    log.info(f"[TW] TWSE 成交量排行 {try_date}：{len(stocks)} 支")
                    return stocks
        except Exception as e:
            log.warning(f"[TW] MI_INDEX20 {try_date} 失敗: {e}")

    log.warning("[TW] 改用 CORE_TW50_STOCKS 預設清單")
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
    掃描台股，回傳完整資料列表。

    流程：
      1. TWSE 批量抓取三大法人 + 融資券（20 次 API，覆蓋全市場 × 10 日）
      2. FinMind 抓取各股 OHLCV（每支 1 次 API）
      3. 整合資料回傳
    """
    _init_rate_limiter()
    cfg = _get_rate_config()
    log.info(f"[TW] 開始掃描台股 top {top_n}...")

    stock_sem = asyncio.Semaphore(cfg["sem_count"])

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 步驟 1：批量抓 TWSE 三大法人 + 融資券（並行，速度快）
        inst_history, margin_history = await fetch_twse_bulk(session, days=10)

        # 步驟 2：取掃描清單
        stock_list = await fetch_top_stocks_by_volume(session, top_n)
        log.info(f"[TW] 開始抓 {len(stock_list)} 支股票 OHLCV...")

        # 步驟 3：每支股票抓 OHLCV（FinMind），注入 TWSE 資料
        async def fetch_with_limit(sid, name):
            async with stock_sem:
                return await fetch_one_stock(
                    session, sid, name,
                    inst_history=inst_history,
                    margin_history=margin_history,
                )

        tasks   = [fetch_with_limit(sid, name) for sid, name in stock_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    stocks, fail_count = [], 0
    for r in results:
        if isinstance(r, TwStockData) and r.fetch_ok and r.close > 0:
            stocks.append(r)
        else:
            fail_count += 1
            if isinstance(r, Exception):
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
        print(f"FINMIND_TOKEN = {'已設定' if FINMIND_TOKEN else '未設定'}")
        stocks = await fetch_all_tw_stocks(top_n=5)
        for s in stocks:
            print(f"\n=== {s.stock_id} {s.name} ===")
            print(f"  資料日期: {s.data_date}")
            print(f"  收盤: {s.close}  漲跌: {s.change_pct:+.2f}%")
            print(f"  成交金額: {s.trade_value/1e8:.2f} 億")
            print(f"  外資: {s.foreign_net/1e8:+.2f}億  連續: {s.foreign_streak:+d} 天")
            print(f"  三大合計: {s.institutional_net/1e8:+.2f}億")
            print(f"  融資餘額: {s.margin_balance:,}  變化: {s.margin_change_pct:+.1f}%")
            print(f"  融券餘額: {s.short_balance:,}  變化: {s.short_change_pct:+.1f}%")

    asyncio.run(test())
