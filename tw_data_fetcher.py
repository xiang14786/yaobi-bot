"""
tw_data_fetcher.py
==================
台股資料抓取模組（全使用 FinMind API）

資料來源：FinMind API（需 token）
  - OHLCV 價格：TaiwanStockPrice
  - 三大法人：TaiwanStockInstitutionalInvestors
  - 融資融券：TaiwanStockMarginPurchaseShortSale

免費 token 限制：600 次/分鐘
掃描流程（三個分批，避免 422 rate limit）：
  批次 1 → 三大法人（50 支）
  批次 2 → 融資融券（50 支）
  批次 3 → OHLCV（50 支）

環境變數：FINMIND_TOKEN
"""

import os
import asyncio
import logging
import aiohttp
import requests as _requests
import warnings as _warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

# 用 ThreadPoolExecutor 執行同步 requests（TWSE 三大法人用）
_sync_executor = ThreadPoolExecutor(max_workers=2)

log = logging.getLogger("tw_fetcher")

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"


# ──────────────────────────────────────────────
#  全域限流設定
# ──────────────────────────────────────────────
def _get_rate_config():
    if FINMIND_TOKEN:
        return dict(sem_count=5, delay=0.12)   # 600次/分 → 每批5並行，間隔120ms
    else:
        return dict(sem_count=1, delay=2.1)    # 30次/分

_api_sem:   asyncio.Semaphore | None = None
_api_delay: float = 2.1

def _init_rate_limiter():
    global _api_sem, _api_delay
    cfg = _get_rate_config()
    if _api_sem is None:
        _api_sem   = asyncio.Semaphore(cfg["sem_count"])
        _api_delay = cfg["delay"]
        mode = f"有 token（600次/分），並行={cfg['sem_count']}，間隔={cfg['delay']}s" \
               if FINMIND_TOKEN else "無 token（30次/分）"
        log.info(f"[TW] FinMind 限流：{mode}")


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
    # 三大法人（單位：元）
    foreign_net:       float = 0.0
    trust_net:         float = 0.0
    dealer_net:        float = 0.0
    institutional_net: float = 0.0
    foreign_streak:    int   = 0
    # 融資融券（單位：張）
    margin_buy:        int   = 0
    margin_sell:       int   = 0
    margin_balance:    int   = 0
    margin_change_pct: float = 0.0
    short_sell:        int   = 0
    short_balance:     int   = 0
    short_change_pct:  float = 0.0
    data_date:  str  = ""
    fetch_ok:   bool = True
    error_msg:  str  = ""


# ──────────────────────────────────────────────
#  FinMind 通用呼叫（帶重試）
# ──────────────────────────────────────────────
async def _finmind_fetch(
    session:    aiohttp.ClientSession,
    dataset:    str,
    stock_id:   str,
    start_date: str,
    retries:    int = 3,
) -> list[dict]:
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
        async with _api_sem:
            try:
                async with session.get(
                    FINMIND_BASE, params=params,
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 429:
                        wait = 60 * (attempt + 1)
                        log.warning(f"[TW] FinMind 429 {dataset}/{stock_id}，等 {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 422:
                        log.warning(f"[TW] FinMind 422 {dataset}/{stock_id}（方案不支援或參數錯誤）")
                        return []
                    if resp.status != 200:
                        log.warning(f"[TW] FinMind HTTP {resp.status} {dataset}/{stock_id}")
                        return []
                    j = await resp.json()
                    if j.get("status") == 402 or "Limit" in str(j.get("msg", "")):
                        wait = 60 * (attempt + 1)
                        log.warning(f"[TW] FinMind 速率超限 {dataset}/{stock_id}，等 {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if j.get("status") != 200:
                        log.debug(f"[TW] FinMind status={j.get('status')} {dataset}/{stock_id}")
                        return []
                    await asyncio.sleep(_api_delay)
                    return j.get("data", [])
            except asyncio.TimeoutError:
                log.warning(f"[TW] FinMind timeout {dataset}/{stock_id} (attempt {attempt+1})")
                await asyncio.sleep(5 * (attempt + 1))
            except Exception as e:
                log.error(f"[TW] FinMind error {dataset}/{stock_id}: {e}")
                return []
    return []


# ──────────────────────────────────────────────
#  三大法人（同步 requests，跑在 ThreadPoolExecutor）
#  來源 1：opendata.twse.com.tw（TWSE 開放資料，不同 CDN）
#  來源 2：www.twse.com.tw exchangeReport/T86（備用）
#  用 requests 而不是 aiohttp，繞過 aiohttp 的 SSL/proxy 問題
# ──────────────────────────────────────────────
def _fetch_t86_sync() -> dict[str, dict]:
    """
    同步抓取三大法人。
    策略：先抓 TWSE 首頁取得 session cookies，再帶 cookies 請求 T86 JSON。
    模擬真實瀏覽器行為，繞過 TWSE 的非瀏覽器請求偵測。
    """
    _warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    HEADERS_BROWSER = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate",   # 不含 br，requests 不支援 Brotli 解壓
        "Connection":      "keep-alive",
    }
    HEADERS_XHR = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate",   # 不含 br，requests 不支援 Brotli 解壓
        "Referer":         "https://www.twse.com.tw/zh/trading/fund/T86.html",
        "Connection":      "keep-alive",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _parse_int(s) -> int:
        try:
            return int(str(s).replace(",", "").replace("--", "0").strip() or 0)
        except Exception:
            return 0

    import time as _time

    # ── 方法：Session + 先拿 cookie ──────────────────────────
    sess = _requests.Session()
    try:
        # 第一步：抓首頁建立 session（取得 cookies）
        sess.get("https://www.twse.com.tw/zh/", headers=HEADERS_BROWSER,
                 timeout=10, verify=False)
        _time.sleep(0.5)  # 模擬人類行為停頓

        # 第二步：找最近有資料的交易日（最多試 5 天）
        d = date.today()
        tried = 0
        while tried < 5:
            if d.weekday() < 5:
                ds = d.strftime("%Y%m%d")
                url = (
                    f"https://www.twse.com.tw/rwd/zh/fund/T86"
                    f"?date={ds}&selectType=ALLBUT0999&response=json"
                )
                r = sess.get(url, headers=HEADERS_XHR, timeout=15, verify=False)
                log.info(f"[TW] T86 session/{ds}: HTTP {r.status_code}, 前60字: {r.text[:60]!r}")

                if r.status_code == 200:
                    try:
                        j = r.json()
                    except Exception:
                        log.debug(f"[TW] T86 session/{ds}: JSON 解析失敗，跳過")
                        tried += 1
                        d -= timedelta(days=1)
                        continue
                    data_rows = j.get("data", [])
                    fields    = j.get("fields", [])
                    log.info(f"[TW] T86 session/{ds}: stat={j.get('stat')!r}, {len(data_rows)} 列, 欄數={len(data_rows[0]) if data_rows else 0}, fields={fields[:5]}")
                    if j.get("stat") in ("OK", "ok") and data_rows:
                        # 動態用 fields 陣列找正確欄位索引
                        def _find_col(keywords):
                            for i, f in enumerate(fields):
                                if all(k in f for k in keywords):
                                    return i
                            return -1

                        idx_foreign = _find_col(["外資", "買賣超"])
                        idx_trust   = _find_col(["投信", "買賣超"])
                        idx_dealer  = _find_col(["自營商", "買賣超"])
                        idx_total   = _find_col(["三大法人"])

                        # fallback：根據欄數判斷格式
                        n_cols = len(data_rows[0]) if data_rows else 0
                        if idx_foreign < 0:
                            idx_foreign = 4
                            idx_trust   = 10 if n_cols >= 21 else 7
                            idx_dealer  = 19 if n_cols >= 21 else 8
                            idx_total   = 20 if n_cols >= 21 else 11
                        log.info(f"[TW] T86 欄位索引: 外資={idx_foreign}, 投信={idx_trust}, 自營={idx_dealer}, 合計={idx_total}")

                        result: dict[str, dict] = {}
                        for row in data_rows:
                            sid = str(row[0]).strip()
                            if not sid.isdigit():
                                continue
                            n = len(row)
                            result[sid] = {
                                "foreign": _parse_int(row[idx_foreign]) if idx_foreign < n else 0,
                                "trust":   _parse_int(row[idx_trust])   if idx_trust   < n else 0,
                                "dealer":  _parse_int(row[idx_dealer])  if idx_dealer  < n else 0,
                                "total":   _parse_int(row[idx_total])   if idx_total   < n else 0,
                            }
                        if result:
                            log.info(f"[TW] T86 session/{ds}: 成功解析 {len(result)} 支")
                            return result
                    else:
                        log.debug(f"[TW] T86 session/{ds}: stat={j.get('stat')!r}，無資料")
                tried += 1
            d -= timedelta(days=1)

    except Exception as e:
        log.warning(f"[TW] T86 session 方法失敗: {e}")
    finally:
        sess.close()

    log.warning("[TW] T86 所有方法均失敗，三大法人不可用")
    return {}


def _fetch_t86_for_date(target_date: date) -> dict[str, dict] | None:
    """
    抓指定日期的 T86 資料。若無資料（假日/停市）回傳 None。
    """
    import warnings as _w, time as _t
    _w.filterwarnings("ignore", message="Unverified HTTPS request")

    HEADERS_BROWSER = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    HEADERS_XHR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.twse.com.tw/zh/trading/fund/T86.html",
        "Connection": "keep-alive",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _parse_int(s) -> int:
        try:
            return int(str(s).replace(",", "").replace("--", "0").strip() or 0)
        except Exception:
            return 0

    ds = target_date.strftime("%Y%m%d")
    sess = _requests.Session()
    try:
        sess.get("https://www.twse.com.tw/zh/", headers=HEADERS_BROWSER, timeout=10, verify=False)
        _t.sleep(0.3)
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json"
        r = sess.get(url, headers=HEADERS_XHR, timeout=15, verify=False)
        if r.status_code != 200:
            return None
        try:
            j = r.json()
        except Exception:
            return None
        if j.get("stat") not in ("OK", "ok"):
            return None
        data_rows = j.get("data", [])
        fields    = j.get("fields", [])
        if not data_rows:
            return None

        def _find_col(keywords):
            for i, f in enumerate(fields):
                if all(k in f for k in keywords):
                    return i
            return -1

        idx_foreign = _find_col(["外資", "買賣超"])
        idx_trust   = _find_col(["投信", "買賣超"])
        idx_dealer  = _find_col(["自營商", "買賣超"])
        idx_total   = _find_col(["三大法人"])
        n_cols = len(data_rows[0]) if data_rows else 0
        if idx_foreign < 0:
            idx_foreign = 4
            idx_trust   = 10 if n_cols >= 21 else 7
            idx_dealer  = 19 if n_cols >= 21 else 8
            idx_total   = 20 if n_cols >= 21 else 11

        result = {}
        for row in data_rows:
            sid = str(row[0]).strip()
            if not sid.isdigit():
                continue
            n = len(row)
            result[sid] = {
                "foreign": _parse_int(row[idx_foreign]) if idx_foreign < n else 0,
                "trust":   _parse_int(row[idx_trust])   if idx_trust   < n else 0,
                "dealer":  _parse_int(row[idx_dealer])  if idx_dealer  < n else 0,
                "total":   _parse_int(row[idx_total])   if idx_total   < n else 0,
            }
        return result if result else None
    except Exception as e:
        log.debug(f"[TW] T86 {ds} 失敗: {e}")
        return None
    finally:
        sess.close()


def _fetch_t86_multiday_sync(days: int = 5) -> dict[str, list]:
    """
    同步抓最近 N 個交易日的 T86，
    回傳 {stock_id: [{"date":..., "foreign":..., ...}, ...]} 多天歷史
    """
    import time as _t
    result: dict[str, list] = {}
    trading_days_found = 0
    d = date.today()

    while trading_days_found < days and d >= date.today() - timedelta(days=30):
        if d.weekday() < 5:  # 跳過週末
            data = _fetch_t86_for_date(d)
            if data:
                ds = d.isoformat()
                for sid, vals in data.items():
                    if sid not in result:
                        result[sid] = []
                    result[sid].insert(0, {"date": ds, **vals})  # 舊→新排列
                trading_days_found += 1
                log.info(f"[TW] T86 {ds}: 成功取得 {len(data)} 支")
                _t.sleep(0.5)  # 避免過快
            else:
                log.debug(f"[TW] T86 {d.isoformat()}: 無資料（假日或停市）")
        d -= timedelta(days=1)

    log.info(f"[TW] T86 多日：取得 {trading_days_found} 個交易日，共 {len(result)} 支")
    return result


async def _fetch_inst_bulk(
    session:    aiohttp.ClientSession,  # 保留相容性，實際不使用
    stock_ids:  list[str],
    start_date: str,
) -> dict[str, list]:
    """
    非同步包裝：在 ThreadPool 執行同步 requests 抓 T86 多天歷史，
    回傳 {stock_id: [{"date":..., "foreign":..., ...}, ...]} 格式
    """
    loop = asyncio.get_event_loop()
    inst: dict[str, list] = await loop.run_in_executor(
        _sync_executor, _fetch_t86_multiday_sync, 10
    )

    set_ids = set(stock_ids)
    matched = sum(1 for sid in inst if sid in set_ids)
    log.info(f"[TW] 三大法人：共 {len(inst)} 支，掃描清單命中 {matched} 支")
    return inst


# ──────────────────────────────────────────────
#  批量抓取融資融券（FinMind）
# ──────────────────────────────────────────────
async def _fetch_margin_bulk(
    session:    aiohttp.ClientSession,
    stock_ids:  list[str],
    start_date: str,
) -> dict[str, list]:
    """
    抓取所有股票的融資融券資料。
    回傳 {stock_id: [{"date":..., "margin_bal":..., "short_bal":...}, ...]}
    """
    log.info(f"[TW] 批量抓融資融券 {len(stock_ids)} 支（FinMind）...")
    sem = asyncio.Semaphore(_get_rate_config()["sem_count"])

    async def fetch_one(sid):
        async with sem:
            rows = await _finmind_fetch(session, "TaiwanStockMarginPurchaseShortSale", sid, start_date)
            await asyncio.sleep(_get_rate_config()["delay"])
            return sid, rows

    results = await asyncio.gather(*[fetch_one(sid) for sid in stock_ids], return_exceptions=True)

    margin: dict[str, list] = {}
    ok_count = 0
    for r in results:
        if isinstance(r, Exception):
            continue
        sid, rows = r
        if not rows:
            continue
        ok_count += 1
        entries = []
        for row in sorted(rows, key=lambda x: x.get("date", "")):
            entries.append({
                "date":       row.get("date", "")[:10],
                "margin_bal": int(row.get("MarginPurchaseBalance", 0) or 0),
                "short_bal":  int(row.get("ShortSaleBalance", 0) or 0),
            })
        if entries:
            margin[sid] = entries

    log.info(f"[TW] 融資融券：{ok_count}/{len(stock_ids)} 支有資料")
    return margin


# ──────────────────────────────────────────────
#  個股 OHLCV + 整合資料
# ──────────────────────────────────────────────
async def fetch_one_stock(
    session: aiohttp.ClientSession,
    stock_id: str,
    name: str = "",
    days: int = 60,
    inst_history:   dict | None = None,
    margin_history: dict | None = None,
) -> TwStockData:
    result = TwStockData(stock_id=stock_id, name=name or stock_id)
    start  = (date.today() - timedelta(days=days + 45)).isoformat()

    # ── OHLCV ────────────────────────────────
    price_data = await _finmind_fetch(session, "TaiwanStockPrice", stock_id, start)
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

    # ── 三大法人 ──────────────────────────────
    inst_hist = (inst_history or {}).get(stock_id, [])
    if inst_hist:
        latest_inst = inst_hist[-1]
        cp = result.close or 1.0
        # FinMind 三大法人單位是「股」，×收盤價 = 元
        result.foreign_net        = latest_inst["foreign"] * cp
        result.trust_net          = latest_inst["trust"]   * cp
        result.dealer_net         = latest_inst["dealer"]  * cp
        result.institutional_net  = latest_inst["total"]   * cp

        # 外資連續買超天數
        streak, last_sign = 0, None
        for day in reversed(inst_hist):
            sign = 1 if day["foreign"] > 0 else (-1 if day["foreign"] < 0 else 0)
            if sign == 0:
                break
            if last_sign is None:
                last_sign = sign
            if sign == last_sign:
                streak += sign
            else:
                break
        result.foreign_streak = streak

    # ── 融資融券 ──────────────────────────────
    mgn_hist = (margin_history or {}).get(stock_id, [])
    if mgn_hist:
        latest_mgn = mgn_hist[-1]
        result.margin_balance = latest_mgn["margin_bal"]
        result.short_balance  = latest_mgn["short_bal"]

        if len(mgn_hist) >= 2:
            prev = mgn_hist[-2]   # 前一個交易日
            if prev["margin_bal"] > 0:
                result.margin_change_pct = (
                    (result.margin_balance - prev["margin_bal"]) / prev["margin_bal"] * 100
                )
            if prev["short_bal"] > 0:
                result.short_change_pct = (
                    (result.short_balance - prev["short_bal"]) / prev["short_bal"] * 100
                )

    return result


# ──────────────────────────────────────────────
#  取得掃描股票清單（TWSE 成交量排行）
# ──────────────────────────────────────────────
async def fetch_top_stocks_by_volume(
    session: aiohttp.ClientSession,
    top_n: int = 80,
) -> list[tuple[str, str]]:
    """嘗試用 TWSE 取成交量前 N，失敗則 fallback 到 CORE_TW50_STOCKS"""
    for delta in range(5):
        try_date = (date.today() - timedelta(days=delta)).strftime("%Y%m%d")
        url = (
            f"https://www.twse.com.tw/exchangeReport/MI_INDEX20"
            f"?response=json&date={try_date}&type=IND"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.twse.com.tw/zh/",
        }
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10),
                                   ssl=False) as resp:
                if resp.status != 200:
                    continue
                raw = await resp.text()
                if not raw or raw.strip()[0] != "{":
                    continue
                import json as _json
                j = _json.loads(raw)
                rows = j.get("data", [])
                if not rows:
                    continue
                stocks = []
                for row in rows[:top_n]:
                    if len(row) >= 3:
                        sid  = str(row[1]).strip()
                        name = str(row[2]).strip()
                        if sid.isdigit() and 4 <= len(sid) <= 6:
                            stocks.append((sid, name))
                if stocks:
                    log.info(f"[TW] TWSE 成交量排行 {try_date}：{len(stocks)} 支")
                    return stocks
        except Exception as e:
            log.debug(f"[TW] MI_INDEX20 {try_date} 失敗（非嚴重）: {e}")

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

    流程（三個分批，嚴格限流）：
      批次 1 → FinMind 三大法人（all stocks）
      批次 2 → FinMind 融資融券（all stocks）
      批次 3 → FinMind OHLCV（all stocks）
    """
    _init_rate_limiter()
    log.info(f"[TW] 開始掃描台股 top {top_n}...")

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:

        # 步驟 1：取股票清單
        stock_list = await fetch_top_stocks_by_volume(session, top_n)
        stock_ids  = [sid for sid, _ in stock_list]
        name_map   = {sid: name for sid, name in stock_list}
        log.info(f"[TW] 掃描清單：{len(stock_ids)} 支")

        # 最近 10 個交易日的起始日期
        inst_start  = (date.today() - timedelta(days=20)).isoformat()
        ohlcv_start = (date.today() - timedelta(days=105)).isoformat()

        # 步驟 2：批量三大法人
        inst_history = await _fetch_inst_bulk(session, stock_ids, inst_start)

        # 步驟 3：批量融資融券
        margin_history = await _fetch_margin_bulk(session, stock_ids, inst_start)

        # 步驟 4：每支股票 OHLCV
        log.info(f"[TW] 開始抓 {len(stock_ids)} 支股票 OHLCV...")
        cfg = _get_rate_config()
        stock_sem = asyncio.Semaphore(cfg["sem_count"])

        async def fetch_with_limit(sid):
            async with stock_sem:
                result = await fetch_one_stock(
                    session, sid, name_map.get(sid, sid),
                    inst_history=inst_history,
                    margin_history=margin_history,
                )
                await asyncio.sleep(cfg["delay"])
                return result

        tasks   = [fetch_with_limit(sid) for sid in stock_ids]
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
