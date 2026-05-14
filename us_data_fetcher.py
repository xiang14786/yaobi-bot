"""
us_data_fetcher.py
==================
美股資料抓取模組 — 使用 yfinance（完全免費）

資料來源：
  - yfinance (Yahoo Finance)
    → 不需 API Key，直接 pip install yfinance
    → OHLCV 日線（最近 90 日）
    → 法人持股比例、空頭部位（每週更新）
    → 平均成交量、市值、Beta

使用方式：
    from us_data_fetcher import fetch_all_us_stocks, UsStockData
    stocks = await fetch_all_us_stocks(top_n=50)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("us_fetcher")

# 執行緒池（yfinance 是同步函式，用 executor 跑）
_executor = ThreadPoolExecutor(max_workers=6)

# 美東時區
ET_TZ = timezone(timedelta(hours=-4))   # EDT（夏令，約 3-11 月）


# ──────────────────────────────────────────────
#  資料結構
# ──────────────────────────────────────────────
@dataclass
class UsStockData:
    """單一美股的原始資料彙整"""
    ticker:     str        # 股票代號，e.g. "AAPL"
    name:       str = ""   # 公司名稱
    sector:     str = ""   # 產業分類
    # 最新日價格
    close:      float = 0.0
    open_:      float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    volume:     int   = 0
    avg_volume: int   = 0      # 10 日平均成交量
    volume_ratio: float = 0.0  # 今日量 / 均量
    # 漲跌幅
    change_pct: float = 0.0
    # K 線歷史（最近 60 日，用於技術指標）
    closes:     list[float] = field(default_factory=list)
    highs:      list[float] = field(default_factory=list)
    lows:       list[float] = field(default_factory=list)
    volumes:    list[int]   = field(default_factory=list)
    # 法人與空頭部位（Yahoo Finance 每週更新）
    inst_pct:      float = 0.0   # 法人持股比例 0~1
    short_float:   float = 0.0   # 空頭比例（佔流通股）0~1
    short_ratio:   float = 0.0   # 空頭 days to cover
    # 基本面
    market_cap:    float = 0.0   # 市值（元）
    beta:          float = 1.0
    pe_ratio:      float = 0.0
    week52_high:   float = 0.0
    week52_low:    float = 0.0
    # 資料日期
    data_date:  str  = ""
    # 狀態
    fetch_ok:   bool = True
    error_msg:  str  = ""


# ──────────────────────────────────────────────
#  單一股票抓取（在執行緒中同步執行）
# ──────────────────────────────────────────────
def _fetch_one_sync(ticker: str, period: str = "3mo") -> UsStockData:
    """
    同步版本，由 ThreadPoolExecutor 執行。
    yfinance 是同步函式，不能直接 await。
    """
    import yfinance as yf

    result = UsStockData(ticker=ticker)
    try:
        t = yf.Ticker(ticker)

        # ── OHLCV 歷史 ──
        hist = t.history(period=period, auto_adjust=True)
        if hist.empty:
            result.fetch_ok  = False
            result.error_msg = "無歷史資料"
            return result

        # 取最近 60 根日 K（已是調整後）
        hist = hist.tail(60)
        result.closes  = [float(v) for v in hist["Close"].tolist()]
        result.highs   = [float(v) for v in hist["High"].tolist()]
        result.lows    = [float(v) for v in hist["Low"].tolist()]
        result.volumes = [int(v)   for v in hist["Volume"].tolist()]

        latest = hist.iloc[-1]
        result.data_date = str(hist.index[-1].date())
        result.close     = float(latest["Close"])
        result.open_     = float(latest["Open"])
        result.high      = float(latest["High"])
        result.low       = float(latest["Low"])
        result.volume    = int(latest["Volume"])

        # 漲跌幅
        if len(result.closes) >= 2 and result.closes[-2] > 0:
            result.change_pct = (result.closes[-1] - result.closes[-2]) / result.closes[-2] * 100

        # ── 基本資料（info 有時會超時，用 try 保護）──
        try:
            info = t.info or {}
            result.name        = info.get("shortName", ticker)
            result.sector      = info.get("sector", "")
            result.avg_volume  = int(info.get("averageVolume10days", 0) or 0)
            result.inst_pct    = float(
                info.get("heldPercentInstitutions") or
                info.get("institutionPercentHeld") or 0
            )
            result.short_float = float(info.get("shortPercentOfFloat", 0) or 0)
            result.short_ratio = float(info.get("shortRatio", 0) or 0)
            result.market_cap  = float(info.get("marketCap", 0) or 0)
            result.beta        = float(info.get("beta", 1.0) or 1.0)
            result.pe_ratio    = float(info.get("trailingPE", 0) or 0)
            result.week52_high = float(info.get("fiftyTwoWeekHigh", 0) or 0)
            result.week52_low  = float(info.get("fiftyTwoWeekLow", 0) or 0)

            if result.avg_volume > 0 and result.volume > 0:
                result.volume_ratio = result.volume / result.avg_volume
        except Exception as e:
            log.debug(f"[US] {ticker} info 取得失敗（非嚴重）: {e}")
            result.name = ticker  # 至少用代號

    except Exception as e:
        result.fetch_ok  = False
        result.error_msg = str(e)[:80]
        log.error(f"[US] {ticker} 抓取失敗: {e}")

    return result


# ──────────────────────────────────────────────
#  非同步包裝
# ──────────────────────────────────────────────
async def fetch_one_us_stock(ticker: str) -> UsStockData:
    """非同步包裝，供 Bot 呼叫"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_one_sync, ticker)


async def fetch_all_us_stocks(
    tickers: list[str] | None = None,
    top_n: int = 60,
    concurrency: int = 6,
) -> list[UsStockData]:
    """
    批量抓取美股資料。
    tickers=None 時使用 SP500_TOP_STOCKS 預設清單。
    concurrency 控制同時幾個請求（建議 4-8）。
    """
    if tickers is None:
        tickers = SP500_TOP_STOCKS[:top_n]

    log.info(f"[US] 開始抓取 {len(tickers)} 支美股（並行={concurrency}）...")
    sem = asyncio.Semaphore(concurrency)

    async def fetch_with_sem(t):
        async with sem:
            result = await fetch_one_us_stock(t)
            await asyncio.sleep(0.2)   # 避免過快
            return result

    tasks   = [fetch_with_sem(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    stocks = []
    for r in results:
        if isinstance(r, UsStockData) and r.fetch_ok and r.close > 0:
            stocks.append(r)
        elif isinstance(r, Exception):
            log.error(f"[US] 異常: {r}")

    log.info(f"[US] 完成，有效 {len(stocks)} 支")
    return stocks


# ──────────────────────────────────────────────
#  S&P 500 預設掃描清單（按市值 / 常見交易量排序）
# ──────────────────────────────────────────────
SP500_TOP_STOCKS: list[str] = [
    # 科技 & 半導體
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "AMD", "INTC", "QCOM", "AVGO", "TXN", "MU", "AMAT", "LRCX",
    "KLAC", "ADI", "MRVL", "ON", "SMCI",
    # 軟體 & 雲端
    "CRM", "ORCL", "ADBE", "NOW", "INTU", "PANW", "SNDK",
    "PLTR", "UBER", "ABNB",
    # 金融
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW",
    "AXP", "V", "MA", "PYPL", "COF",
    # 醫療 & 生技
    "UNH", "JNJ", "ABBV", "LLY", "MRK", "PFE", "AMGN",
    "GILD", "VRTX", "REGN", "ISRG", "MDT", "SYK", "BSX",
    # 消費 & 零售
    "AMZN", "WMT", "COST", "HD", "TGT", "NKE", "MCD",
    "SBUX", "LOW", "TJX",
    # 能源 & 工業
    "XOM", "CVX", "LIN", "CAT", "HON", "GE", "BA", "LMT",
    "RTX", "UPS", "DE", "ETN",
    # 電信 & 媒體
    "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
    # 其他大型
    "BRK-B", "PG", "KO", "PEP", "PM", "MO", "SO", "NEE",
    "SPGI", "CME", "ICE",
]


# ──────────────────────────────────────────────
#  快速測試
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)

    async def test():
        stocks = await fetch_all_us_stocks(top_n=5)
        for s in stocks:
            print(f"\n=== {s.ticker} {s.name} ===")
            print(f"  收盤: ${s.close:.2f}  漲跌: {s.change_pct:+.2f}%")
            print(f"  成交量: {s.volume:,}  量比: {s.volume_ratio:.2f}x")
            print(f"  法人持股: {s.inst_pct:.1%}  空頭比例: {s.short_float:.1%}")
            print(f"  市值: ${s.market_cap/1e9:.0f}B  Beta: {s.beta:.2f}")
            print(f"  52W High: ${s.week52_high:.2f}  Low: ${s.week52_low:.2f}")
            print(f"  K 線筆數: {len(s.closes)}")

    asyncio.run(test())
