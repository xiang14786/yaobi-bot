"""
tv_chart_utils.py
=================
TradingView 圖表連結產生工具

功能：
  - 根據交易對名稱，自動產生對應的 TradingView 圖表連結
  - 支援幣安現貨、合約（PERP）、台股（TWSE）、美股（NASDAQ/NYSE）
  - 支援多時框連結一次產生

在 tg_bot_v2.py、yaobi_scorer_v2.py 等任何地方直接 import 使用即可：
    from tv_chart_utils import get_tv_chart_url, get_tv_chart_url_multi, append_chart_link
"""

from urllib.parse import urlencode

# ──────────────────────────────────────────────
#  時框對照表（TradingView 格式）
# ──────────────────────────────────────────────
TIMEFRAME_MAP = {
    "1m":  "1",
    "3m":  "3",
    "5m":  "5",
    "15m": "15",
    "30m": "30",
    "1h":  "60",
    "2h":  "120",
    "4h":  "240",
    "6h":  "360",
    "8h":  "480",
    "12h": "720",
    "1d":  "D",
    "1w":  "W",
    "1M":  "M",
    # 也接受純數字字串
    "1":   "1",
    "5":   "5",
    "15":  "15",
    "60":  "60",
    "240": "240",
    "D":   "D",
    "W":   "W",
}

# ──────────────────────────────────────────────
#  交易所前綴推斷
# ──────────────────────────────────────────────
def _infer_exchange_symbol(symbol: str, market: str = "crypto_perp") -> str:
    """
    把標的代碼轉換成 TradingView 的 EXCHANGE:SYMBOL 格式。

    market 參數：
        "crypto_perp"  → 幣安合約（預設）, e.g. BTCUSDT → BINANCE:BTCUSDTPERP
        "crypto_spot"  → 幣安現貨,           e.g. BTCUSDT → BINANCE:BTCUSDT
        "tw"           → 台股,               e.g. 2330   → TWSE:2330
        "us"           → 美股（自動判斷）,   e.g. AAPL   → NASDAQ:AAPL
    """
    symbol = symbol.upper().replace("/", "").replace("-", "")

    if market == "crypto_perp":
        # 移除已有的 PERP 或 .P 後綴，統一加 PERP
        base = symbol.replace("PERP", "").replace(".P", "")
        # 確保以 USDT 結尾（合約主流計價）
        if not base.endswith("USDT"):
            base = base + "USDT"
        return f"BINANCE:{base}PERP"

    elif market == "crypto_spot":
        base = symbol.replace("PERP", "").replace(".P", "")
        if not base.endswith("USDT"):
            base = base + "USDT"
        return f"BINANCE:{base}"

    elif market == "tw":
        # 台股代碼（數字 or 英數字）
        return f"TWSE:{symbol}"

    elif market == "us":
        # 簡單判斷：ETF/知名股通常在 NYSE 或 NASDAQ
        # TradingView 可接受 NASDAQ:AAPL，搜不到會自動跳
        # 或直接用 symbol 讓 TV 自行搜尋
        return f"NASDAQ:{symbol}"

    else:
        # 直接回傳，讓 TradingView 自行解析
        return symbol


# ──────────────────────────────────────────────
#  主要 API
# ──────────────────────────────────────────────
def get_tv_chart_url(
    symbol: str,
    timeframe: str = "60",
    market: str = "crypto_perp",
    exchange_symbol: str | None = None,
) -> str:
    """
    產生單一 TradingView 圖表 URL。

    Args:
        symbol:           交易對，e.g. "BTCUSDT"、"2330"、"AAPL"
        timeframe:        時框，e.g. "15m"、"1h"、"4h"、"D"
        market:           市場類型："crypto_perp" / "crypto_spot" / "tw" / "us"
        exchange_symbol:  若已知完整格式（"BINANCE:BTCUSDTPERP"）可直接傳入，略過推斷

    Returns:
        TradingView 圖表 URL 字串
    """
    tv_symbol = exchange_symbol or _infer_exchange_symbol(symbol, market)
    tv_tf = TIMEFRAME_MAP.get(timeframe, timeframe)   # 若已是 TV 格式直接用

    params = urlencode({
        "symbol": tv_symbol,
        "interval": tv_tf,
    })
    return f"https://www.tradingview.com/chart/?{params}"


def get_tv_chart_url_multi(
    symbol: str,
    timeframes: list[str] | None = None,
    market: str = "crypto_perp",
) -> str:
    """
    產生多時框圖表連結，回傳 Markdown 格式字串（可直接嵌入 TG 訊息）。

    例：get_tv_chart_url_multi("BTCUSDT", ["15", "60", "240"])
    → "[15m](url) | [1h](url) | [4h](url)"
    """
    if timeframes is None:
        timeframes = ["15", "60", "240"]

    TF_LABEL = {
        "1": "1m", "3": "3m", "5": "5m", "15": "15m",
        "30": "30m", "60": "1h", "120": "2h", "240": "4h",
        "360": "6h", "480": "8h", "720": "12h",
        "D": "日", "W": "週", "M": "月",
    }

    links = []
    for tf in timeframes:
        tv_tf = TIMEFRAME_MAP.get(tf, tf)
        label = TF_LABEL.get(tv_tf, tf)
        url = get_tv_chart_url(symbol, tf, market)
        links.append(f"[{label}]({url})")

    return " | ".join(links)


def get_tv_chart_url_snapshot(symbol: str, market: str = "crypto_perp") -> str:
    """
    產生 TradingView 搜尋頁連結（適合不確定精確格式時使用）。
    點開後使用者在 TV 上搜尋該標的。
    """
    clean = symbol.upper().replace("/", "").replace("-", "")
    return f"https://www.tradingview.com/symbols/{clean}/"


# ──────────────────────────────────────────────
#  訊息格式化工具
# ──────────────────────────────────────────────
def append_chart_link(
    message: str,
    symbol: str,
    timeframes: list[str] | None = None,
    market: str = "crypto_perp",
    label: str = "📊 TradingView 圖表",
) -> str:
    """
    在現有 Bot 訊息末尾附加 TradingView 圖表連結區塊。

    在 tg_bot_v2.py 所有回覆函式的最後，把回傳的 msg 字串包一層即可：
        msg = append_chart_link(msg, symbol)

    Args:
        message:    原始訊息字串
        symbol:     交易對
        timeframes: 要附上的時框列表（預設 15m / 1h / 4h）
        market:     市場類型
        label:      顯示標題

    Returns:
        附加圖表連結後的新訊息字串
    """
    tfs = timeframes or ["15", "60", "240"]
    multi_link = get_tv_chart_url_multi(symbol, tfs, market)
    chart_block = f"\n━━━━━━━━━━━━━━━━━━\n{label}：{multi_link}"
    return message + chart_block


def format_trade_chart_block(symbol: str, market: str = "crypto_perp") -> str:
    """
    產生適合放在 /trade 回覆末尾的完整圖表區塊字串。

    範例輸出：
        ━━━ 圖表 ━━━
        📊 TradingView：[15m](url) | [1h](url) | [4h](url) | [日](url)
        🔍 搜尋頁：https://www.tradingview.com/symbols/BTCUSDTPERP/
    """
    multi = get_tv_chart_url_multi(symbol, ["15", "60", "240", "D"], market)
    snapshot = get_tv_chart_url_snapshot(symbol, market)
    return (
        f"\n━━━ 圖表 ━━━\n"
        f"📊 TradingView：{multi}\n"
        f"🔍 [標的頁面]({snapshot})"
    )


# ──────────────────────────────────────────────
#  快速測試（直接執行此檔時）
# ──────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        ("BTCUSDT",  "crypto_perp",  ["15", "60", "240"]),
        ("ETHUSDT",  "crypto_spot",  ["15", "60"]),
        ("SOLUSDT",  "crypto_perp",  ["15", "60", "240", "D"]),
        ("2330",     "tw",           ["D", "W"]),
        ("AAPL",     "us",           ["60", "D"]),
    ]

    for symbol, market, tfs in test_cases:
        print(f"\n=== {symbol} ({market}) ===")
        print("單一 URL:", get_tv_chart_url(symbol, "60", market))
        print("多時框  :", get_tv_chart_url_multi(symbol, tfs, market))
        print("搜尋頁  :", get_tv_chart_url_snapshot(symbol, market))
