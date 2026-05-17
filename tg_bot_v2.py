"""
全民 TG 妖幣策略 Bot V2.2
==========================
V2 重點功能:
- 🔋 提早預警: 抓「尚未啟動」的妖幣
- 📐 OB+FVG 結構分析
- 🎯 完整交易建議 (進場/止損/止盈/槓桿/單型)
- 🤖 /status 查詢 Bot 運作狀態

V2.1 新增 (TradingView 串接):
- 📡 接收 TradingView Webhook 信號並推送到 TG
- 📊 所有標的卡片自動附 TradingView 圖表連結
- 🗺️ /trade /detail /structure 附多時框圖表連結
- 新指令: /sub_tv /unsub_tv /tv_status

V2.2 新增 (台股版):
- 🇹🇼 台股妖股雷達，資料來源: FinMind + TWSE
- 三大法人、融資融券領先指標
- 倉位建議 % 取代槓桿
- 新指令: /tw_scan /tw_squeeze /tw_foreign /tw_top10
          /tw_trade /tw_detail /tw_status /tw_sub /tw_unsub
"""
import asyncio
import gc
import logging
import os
import re
import time
from datetime import datetime, time as dtime, timezone, timedelta
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
import db as _db
from yaobi_scorer_v2 import (
    fetch_all_metrics_v2, apply_filters_v2,
    find_pre_pump, find_pre_dump, find_squeeze,
    DEFAULT_FILTERS_V2, CoinMetricsV2,
    passes_consistency_gate,
    find_smart_money,
)

# ── V2.1: TradingView 整合模組 ─────────────────────────────
from tv_chart_utils import get_tv_chart_url, format_trade_chart_block
from tradingview_webhook import (
    TradingViewWebhookHandler,
    start_webhook_server,
    cmd_sub_tv as _cmd_sub_tv,
    cmd_unsub_tv as _cmd_unsub_tv,
    cmd_tv_status as _cmd_tv_status,
)

# ── V2.2: 台股模組 ─────────────────────────────────────────
from tw_stock_scorer import (
    TwStockMetrics,
    fetch_all_tw_metrics,
    fetch_single_tw_metrics,
    find_tw_pre_pump,
    find_tw_squeeze,
    find_tw_institutional_buy,
    find_tw_joint_buy,
    apply_tw_filters,
    DEFAULT_TW_FILTERS,
)
# ── V2.3: 美股模組 ─────────────────────────────────────────
from us_stock_scorer import (
    UsStockMetrics,
    fetch_all_us_metrics,
    find_us_squeeze,
    find_us_short_squeeze,
    find_us_momentum,
    find_us_rs_leaders,
    find_us_accum,
    apply_us_filters,
    DEFAULT_US_FILTERS,
)
from us_data_fetcher import refresh_inst_cache, get_inst_pct
# ──────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("yaobi_v2")

BOT_TOKEN = os.getenv("BOT_TOKEN", "請填入_你的_token")

USER_FILTERS: dict = {}
SUBSCRIBERS: set = set()
PRE_PUMP_SUBSCRIBERS: set = set()
LAST_SCAN: dict = {"time": 0, "data": []}
CACHE_TTL = 180   # 3 分鐘

# V2.1: TradingView Webhook 全域實例（post_init 裡初始化）
tv_handler: TradingViewWebhookHandler | None = None
_webhook_runner = None   # aiohttp AppRunner，shutdown 時清理

# V2.2: 台股快取與訂閱
TW_CACHE: dict = {"time": 0, "data": []}
TW_CACHE_TTL   = 600   # 10 分鐘
TW_SUBSCRIBERS: set = set()
TW_USER_FILTERS: dict = {}

# 台股交易時段
TW_MARKET_OPEN  = dtime(9, 0)
TW_MARKET_CLOSE = dtime(13, 30)
TW_TZ = timezone(timedelta(hours=8))   # 台灣時區 UTC+8

# V2.3: 美股快取與訂閱
US_CACHE: dict = {"time": 0, "data": []}
US_CACHE_TTL   = 600   # 10 分鐘
US_SUBSCRIBERS: set = set()
US_USER_FILTERS: dict = {}

# 美股交易時段（自動判斷夏令/冬令）
US_MARKET_OPEN  = dtime(9, 30)
US_MARKET_CLOSE = dtime(16, 0)

def _et_tz() -> timezone:
    """自動判斷美東時區：3-11月 EDT(UTC-4)，其餘 EST(UTC-5)"""
    m = datetime.now(timezone.utc).month
    return timezone(timedelta(hours=-4 if 3 <= m <= 11 else -5))

ET_TZ = _et_tz()  # 啟動時設定，每次重啟自動更新

# ============================================================
# Phase 3: 自選股 & 警報
# ============================================================
# WATCHLISTS[user_id] = {"2330", "BTC", "AAPL", ...}
WATCHLISTS: dict[int, set] = {}

# WATCH_CONDITIONS[user_id] = {
#   "foreign_streak": 3,   # 外資連買 ≥ N 天才推送
#   "pre_warn_pct":   70,  # 評分達到滿分 X% 就預警
# }
WATCH_CONDITIONS: dict[int, dict] = {}

# 紀錄已推送過的警報（避免重複）key = (user_id, symbol, alert_type) → timestamp
_SENT_ALERTS: dict = {}  # key→送出時間，TTL 24小時
_SENT_ALERTS_TTL = 86400  # 24小時後自動過期

def _prune_sent_alerts():
    """清除超過 TTL 的警報紀錄，防止記憶體洩漏"""
    import time as _t
    now = _t.time()
    expired = [k for k, ts in _SENT_ALERTS.items() if now - ts > _SENT_ALERTS_TTL]
    for k in expired:
        del _SENT_ALERTS[k]

# Phase 2 擴充：自訂 TP/SL 暫存（user_id → 待確認進場資訊）
_PENDING_CUSTOM: dict[int, dict] = {}

# TP/SL 互動警報暫存 {pos_id: {uid, sym, trigger, cur, pnl, ...}}
PENDING_ALERTS: dict[int, dict] = {}
# 使用者手動輸入新止損暫存 {user_id: pos_id}
_PENDING_SL_INPUT: dict[int, int] = {}

# BTC 大盤趨勢狀態（供加密預警使用）
_BTC_TREND: dict = {"change_24h": 0.0, "above_ma20": True, "last_update": 0}

# ============================================================
# 共用
# ============================================================
def _update_btc_trend(coins: list) -> None:
    """更新 BTC 大盤趨勢狀態，並同步給評分模組使用"""
    global _BTC_TREND
    for c in coins:
        if c.symbol == "BTCUSDT":
            _BTC_TREND["change_24h"] = c.price_change_pct
            _BTC_TREND["above_ma20"] = c.total_score > 40
            _BTC_TREND["last_update"] = time.time()
            # 同步給 yaobi_scorer_v2 評分乘數使用
            try:
                import yaobi_scorer_v2 as _ysv
                _ysv._BTC_CHANGE_24H = c.price_change_pct
            except Exception:
                pass
            break


def _btc_trend_warning() -> str:
    """若 BTC 24h < -3%，回傳警告文字"""
    if _BTC_TREND["change_24h"] < -3.0:
        return f"⚠️ 大盤偏空（BTC {_BTC_TREND['change_24h']:.1f}%），訊號可靠性降低\n"
    return ""


async def get_scan(force=False) -> list[CoinMetricsV2]:
    now = asyncio.get_event_loop().time()
    if not force and now - LAST_SCAN["time"] < CACHE_TTL and LAST_SCAN["data"]:
        return LAST_SCAN["data"]
    # 評分前先更新 BTC 漲跌數據，確保乘數正確（避免第一次掃描時 _BTC_CHANGE_24H=0）
    try:
        import aiohttp as _aio, yaobi_scorer_v2 as _ysv
        async with _aio.ClientSession() as _s:
            async with _s.get(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                params={"symbol": "BTCUSDT"},
                timeout=_aio.ClientTimeout(total=5)
            ) as _r:
                if _r.status == 200:
                    _btc = await _r.json()
                    _ysv._BTC_CHANGE_24H = float(_btc.get("priceChangePercent", 0))
    except Exception:
        pass  # 失敗不影響掃描，沿用上次值
    data = await fetch_all_metrics_v2(top_n=100)
    LAST_SCAN.update({"time": now, "data": data})
    _update_btc_trend(data)
    gc.collect()
    # ML 真實特徵儲存（前 30 高分）
    try:
        import time as _t
        from db import save_ml_scan
        _dow = datetime.now().weekday()
        for _rank, c in enumerate(sorted(data, key=lambda x: x.total_score, reverse=True)[:30], 1):
            save_ml_scan(
                symbol=c.symbol, market="crypto",
                total_score=c.total_score, early_score=c.score_early,
                confidence=c.confidence, price=c.last_price,
                change_pct=c.price_change_pct,
                feat1=c.funding_rate,           # 真實 funding rate
                feat2=c.long_short_ratio,       # 真實多空比
                feat3=c.top_trader_ls_ratio,    # 真實頂級多空比
                feat4=getattr(c, "oi_change_pct", 0.0),  # OI 變化
                btc_change_24h=_BTC_TREND.get("change_24h", 0.0),
                consistency_score=sum([
                    c.score_early > 20,
                    c.funding_rate < 0,
                    c.long_short_ratio > 1.1,
                    c.score_onchain > 5,
                    c.score_structure > 60,
                ]),
                dow=_dow, rank_in_session=_rank,
            )
    except Exception as _e:
        log.debug(f"[ML] crypto 儲存失敗: {_e}")
    return data

def get_user_filter(uid):
    return {**DEFAULT_FILTERS_V2, **USER_FILTERS.get(uid, {})}

def get_tw_filter(uid):
    return {**DEFAULT_TW_FILTERS, **TW_USER_FILTERS.get(uid, {})}

def get_us_filter(uid):
    return {**DEFAULT_US_FILTERS, **US_USER_FILTERS.get(uid, {})}

def fmt_card(c: CoinMetricsV2, rank=None, show_triggers=True) -> str:
    """
    格式化單一標的卡片。
    V2.1 改動：移除原本的 └ 結尾邏輯，統一在最後加 TradingView 圖表連結。
    """
    rank_str = f"#{rank} " if rank else ""
    tag_str = "  ".join(c.tags) if c.tags else ""
    lines = [
        f"{rank_str}*{c.base}/USDT*  {c.direction}",
        f"├ 總分 *{c.total_score:.0f}*  早分 *{c.score_early:.0f}*  結構 *{c.score_structure:.0f}*",
        f"├ 24h: {c.price_change_pct:+.2f}%  價: ${c.last_price:,.4f}",
        f"├ 量: ${c.quote_volume/1e6:,.0f}M  費率: {c.funding_rate*100:+.3f}%",
        f"├ 信心: {c.confidence:.0%}  多空比: {c.long_short_ratio:.2f}",
    ]
    if c.nearest_support or c.nearest_resistance:
        s = f"${c.nearest_support:,.4f}" if c.nearest_support else "—"
        r = f"${c.nearest_resistance:,.4f}" if c.nearest_resistance else "—"
        lines.append(f"├ 支撐: {s}  阻力: {r}")
    if show_triggers and c.triggers:
        for t in c.triggers[:3]:
            lines.append(f"├ {t}")
    if tag_str:
        lines.append(f"├ {tag_str}")
    # V2.1: TradingView 圖表連結（永遠是最後一行）
    chart_url = get_tv_chart_url(c.base, timeframe="60")
    lines.append(f"└ [📊 TV 圖表]({chart_url})")
    return "\n".join(lines)

def fmt_list(coins, title, show_triggers=True):
    if not coins:
        return f"*{title}*\n\n目前沒有符合條件的標的 🌙"
    out = [f"*{title}*  _{datetime.now(TW_TZ):%H:%M}_\n"]
    for i, c in enumerate(coins[:8], 1):
        out.append(fmt_card(c, i, show_triggers))
        out.append("")
    out.append("_資料源: 幣安合約 API_")
    return "\n".join(out)

# ============================================================
# 交易建議生成器
# ============================================================
def generate_trade_advice(m: CoinMetricsV2) -> str:
    price = m.last_price
    # === 方向判斷 ===
    is_long  = "PRE_PUMP" in m.early_bias or "🚀" in m.direction or "🔋" in m.direction
    is_short = "PRE_DUMP" in m.early_bias or "📉" in m.direction or "⚠️" in m.direction
    direction_str = "做多 🟢" if is_long else "做空 🔴" if is_short else "觀望 ⚪"

    # === 信心等級 ===
    conf = m.confidence
    if conf >= 0.8:
        conf_str = "非常高 ⭐⭐⭐"
    elif conf >= 0.65:
        conf_str = "高 ⭐⭐"
    elif conf >= 0.5:
        conf_str = "中等 ⭐"
    else:
        conf_str = "偏低 ⚠️"

    # === 槓桿建議 ===
    is_major = m.base in {"BTC", "ETH", "BNB", "SOL", "XRP"}
    if conf < 0.5:
        lev_safe, lev_std, lev_max = 0, 0, 0
        lev_note = "信心不足，不建議開倉"
    elif conf < 0.65:
        lev_safe, lev_std, lev_max = (3, 5, 8) if is_major else (2, 3, 5)
        lev_note = "輕倉試探"
    elif conf < 0.8:
        lev_safe, lev_std, lev_max = (5, 10, 15) if is_major else (3, 5, 8)
        lev_note = "標準倉位"
    else:
        lev_safe, lev_std, lev_max = (10, 15, 20) if is_major else (5, 8, 10)
        lev_note = "可加重倉位"

    # === 單型判斷 ===
    has_squeeze = any("BB 壓縮" in t for t in m.triggers)
    has_sleep   = any("沉睡" in t for t in m.triggers)
    has_oi      = any("OI 暴增" in t for t in m.triggers)
    has_mtf     = any("時框" in t for t in m.triggers)
    if has_squeeze or has_sleep:
        trade_type = "短單 ⚡ (預期 1~4 小時)"
    elif has_oi and has_mtf:
        trade_type = "長單 📈 (預期 1~3 天)"
    elif has_oi:
        trade_type = "中單 🕐 (預期 4~24 小時)"
    else:
        trade_type = "短中單 (預期 2~12 小時)"

    # === 進場/止損/止盈計算 ===
    support    = m.nearest_support
    resistance = m.nearest_resistance

    if is_long and support:
        entry_low  = support * 0.998
        entry_high = support * 1.015
        stop_loss  = support * 0.975
        tp1 = resistance if resistance else price * 1.08
        tp2 = tp1 * 1.05
        rr  = (tp1 - entry_high) / (entry_high - stop_loss)
        in_ob = entry_low <= price <= entry_high * 1.02
        entry_note = "✅ 當前價格已在進場區！" if in_ob else f"⏳ 等待回測，距進場區 {(price - entry_high) / price * 100:.1f}%"
        trade_section = (
            f"*方向*: {direction_str}\n"
            f"*單型*: {trade_type}\n"
            f"*信心*: {conf_str}\n\n"
            f"━━━ 進場區間 ━━━\n"
            f"📍 理想進場: `${entry_low:,.4f}` ~ `${entry_high:,.4f}`\n"
            f"📍 FVG 50%: `${(entry_low+entry_high)/2:,.4f}` ← 最佳點\n"
            f"{entry_note}\n\n"
            f"━━━ 風險管理 ━━━\n"
            f"🛑 止損: `${stop_loss:,.4f}` (破此出場)\n"
            f"🎯 止盈1: `${tp1:,.4f}` (+{(tp1-price)/price*100:.1f}%)\n"
            f"🎯 止盈2: `${tp2:,.4f}` (+{(tp2-price)/price*100:.1f}%)\n"
            f"📊 風報比: `1 : {rr:.1f}` {'✅' if rr >= 2 else '⚠️ 偏低'}\n\n"
            f"━━━ 槓桿建議 ━━━\n"
            f"🟢 保守: `{lev_safe}x`\n"
            f"🟡 標準: `{lev_std}x`\n"
            f"🔴 激進: `{lev_max}x`\n"
            f"💡 {lev_note}\n"
        )
    elif is_short and resistance:
        entry_low  = resistance * 0.985
        entry_high = resistance * 1.002
        stop_loss  = resistance * 1.025
        tp1 = support if support else price * 0.92
        tp2 = tp1 * 0.95
        rr  = (entry_low - tp1) / (stop_loss - entry_low)
        in_ob = entry_low <= price <= entry_high
        entry_note = "✅ 當前價格已在進場區！" if in_ob else f"⏳ 等待反彈，距進場區 {(entry_low - price) / price * 100:.1f}%"
        trade_section = (
            f"*方向*: {direction_str}\n"
            f"*單型*: {trade_type}\n"
            f"*信心*: {conf_str}\n\n"
            f"━━━ 進場區間 ━━━\n"
            f"📍 理想進場: `${entry_low:,.4f}` ~ `${entry_high:,.4f}`\n"
            f"📍 FVG 50%: `${(entry_low+entry_high)/2:,.4f}` ← 最佳點\n"
            f"{entry_note}\n\n"
            f"━━━ 風險管理 ━━━\n"
            f"🛑 止損: `${stop_loss:,.4f}` (破此出場)\n"
            f"🎯 止盈1: `${tp1:,.4f}` (-{(price-tp1)/price*100:.1f}%)\n"
            f"🎯 止盈2: `${tp2:,.4f}` (-{(price-tp2)/price*100:.1f}%)\n"
            f"📊 風報比: `1 : {rr:.1f}` {'✅' if rr >= 2 else '⚠️ 偏低'}\n\n"
            f"━━━ 槓桿建議 ━━━\n"
            f"🟢 保守: `{lev_safe}x`\n"
            f"🟡 標準: `{lev_std}x`\n"
            f"🔴 激進: `{lev_max}x`\n"
            f"💡 {lev_note}\n"
        )
    else:
        trade_section = (
            f"*方向*: {direction_str}\n"
            f"*信心*: {conf_str}\n\n"
            f"⚠️ 目前結構不明確，建議觀望\n"
            f"等待更清晰的 OB 支撐/阻力位形成後再進場\n"
        )

    # === 注意事項 ===
    warnings = []
    if abs(m.price_change_pct) > 10:
        warnings.append(f"⚠️ 24h 已波動 {m.price_change_pct:+.1f}%，追高追低風險較大")
    if abs(m.funding_rate) > 0.001:
        warnings.append(f"⚠️ 資金費率極端 ({m.funding_rate*100:+.3f}%)，注意擠兌風險")
    if conf < 0.5:
        warnings.append("⚠️ 信心偏低，建議等更多訊號確認")
    if lev_std == 0:
        warnings.append("🚫 建議此時不開倉")
    warning_str = "\n".join(warnings) if warnings else "✅ 無特殊警告"

    return (
        f"💡 *{m.base}/USDT 交易建議*\n\n"
        f"當前價: `${price:,.4f}`\n\n"
        f"{trade_section}\n"
        f"━━━ 注意事項 ━━━\n"
        f"{warning_str}\n\n"
        f"_⚠️ 純技術分析參考，非投資建議，請自行控管風險_"
    )

# ============================================================
# V2.2: 台股工具函式
# ============================================================
def tw_market_status() -> str:
    now = datetime.now(TW_TZ)   # 使用台灣時區 UTC+8
    if now.weekday() >= 5:
        return "⛔ 週末休市"
    t = now.time()
    if t < TW_MARKET_OPEN:
        return "⏰ 尚未開盤（09:00 開盤）"
    if t > TW_MARKET_CLOSE:
        return "🔒 今日收盤"
    return "🟢 交易中"

# ============================================================
# V2.3: 美股工具函式
# ============================================================
def us_market_status() -> str:
    now = datetime.now(_et_tz())  # 美東時區（自動夏令/冬令）
    if now.weekday() >= 5:
        return "⛔ 週末休市"
    t = now.time()
    if t < US_MARKET_OPEN:
        return "⏰ 尚未開盤（09:30 ET）"
    if t > US_MARKET_CLOSE:
        return "🔒 今日收盤"
    return "🟢 交易中"

async def get_us_scan(force=False) -> list[UsStockMetrics]:
    now = asyncio.get_event_loop().time()
    if not force and now - US_CACHE["time"] < US_CACHE_TTL and US_CACHE["data"]:
        return US_CACHE["data"]
    data = await fetch_all_us_metrics(top_n=80)
    US_CACHE.update({"time": now, "data": data})
    gc.collect()
    # ML 真實特徵儲存（前 30 高分）
    try:
        from db import save_ml_scan
        _dow = datetime.now().weekday()
        for _rank, m in enumerate(sorted(data, key=lambda x: x.total_score, reverse=True)[:30], 1):
            save_ml_scan(
                symbol=m.ticker, market="us",
                total_score=m.total_score, early_score=m.early_score,
                confidence=m.confidence, price=m.close,
                change_pct=m.change_pct,
                feat1=m.rs_rating,      # RS Rating
                feat2=m.accum_score,    # 法人累積分
                feat3=m.momentum_score, # 動能分
                feat4=m.inst_pct,       # 法人持股
                btc_change_24h=_BTC_TREND.get("change_24h", 0.0),
                consistency_score=0,    # 美股無加密指標
                dow=_dow, rank_in_session=_rank,
            )
    except Exception as _e:
        log.debug(f"[ML] us 儲存失敗: {_e}")
    return data

def fmt_us_card(m: UsStockMetrics, rank=None, show_triggers=True) -> str:
    rank_str  = f"#{rank} " if rank else ""
    tag_str   = "  ".join(m.tags) if m.tags else ""
    cap_str   = (f"${m.market_cap/1e12:.2f}T" if m.market_cap >= 1e12
                 else f"${m.market_cap/1e9:.0f}B" if m.market_cap >= 1e9
                 else f"${m.market_cap/1e6:.0f}M")
    lines = [
        f"{rank_str}*{m.ticker} {m.name}*  {m.direction}",
        f"├ 總分 *{m.total_score:.0f}*  早分 *{m.early_score:.0f}*  信心 *{m.confidence:.0%}*",
        f"├ 收盤: ${m.close:,.2f}  漲跌: {m.change_pct:+.2f}%",
        f"├ 量比: {m.volume_ratio:.1f}x  市值: {cap_str}",
        f"├ 法人持股: {m.inst_pct:.1%}  空頭比: {m.short_float:.1%}",
        f"├ RSI: {m.rsi:.0f}  Beta: {m.beta:.1f}",
    ]
    if m.support or m.resistance:
        s = f"${m.support:,.2f}"    if m.support    else "—"
        r = f"${m.resistance:,.2f}" if m.resistance else "—"
        lines.append(f"├ 支撐: {s}  阻力: {r}")
    if show_triggers and m.triggers:
        for t in m.triggers[:3]:
            lines.append(f"├ {t}")
    if tag_str:
        lines.append(f"├ {tag_str}")
    chart_url = get_tv_chart_url(m.ticker, timeframe="D", market="us")
    lines.append(f"└ [📊 TV 圖表]({chart_url})")
    return "\n".join(lines)

def fmt_us_list(stocks, title, show_triggers=True) -> str:
    if not stocks:
        return f"*{title}*\n\n目前沒有符合條件的標的 🌙"
    mkt = us_market_status()
    out = [f"*{title}*  _{datetime.now(TW_TZ):%H:%M}_  {mkt}\n"]
    for i, m in enumerate(stocks[:8], 1):
        out.append(fmt_us_card(m, i, show_triggers))
        out.append("")
    out.append("_資料源: Yahoo Finance (yfinance)_")
    return "\n".join(out)

def generate_us_trade_advice(m: UsStockMetrics) -> str:
    price   = m.close
    is_bull = any(e in m.direction for e in ["🚀", "🔵", "📈"])
    dir_str = "做多 🟢" if is_bull else "觀望 ⚪"
    conf    = m.confidence
    if conf >= 0.8:    conf_str = "非常高 ⭐⭐⭐"
    elif conf >= 0.65: conf_str = "高 ⭐⭐"
    elif conf >= 0.5:  conf_str = "中等 ⭐"
    else:              conf_str = "偏低 ⚠️"
    pos = m.position_pct
    pos_block = (
        "🚫 信心不足，暫時觀望" if pos == 0 else
        f"━━━ 倉位建議（現股無槓桿）━━━\n"
        f"🟢 保守: 總資金 `{pos//2}%`\n"
        f"🟡 標準: 總資金 `{pos}%`\n"
        f"🔴 積極: 總資金 `{min(pos*2,30)}%`"
    )
    if is_bull and m.support:
        entry_low  = m.support * 0.998
        entry_high = m.support * 1.015
        stop_loss  = m.support * 0.975
        tp1 = m.resistance if m.resistance and m.resistance > price else price * 1.08
        tp2 = tp1 * 1.05
        rr  = (tp1 - entry_high) / (entry_high - stop_loss) if entry_high > stop_loss else 0
        in_zone    = entry_low <= price <= entry_high * 1.02
        entry_note = "✅ 當前在進場區！" if in_zone else f"⏳ 等待回測，距進場區 {(price-entry_high)/price*100:.1f}%"
        trade_block = (
            f"━━━ 進場區間 ━━━\n"
            f"📍 理想進場: `${entry_low:,.2f}` ~ `${entry_high:,.2f}`\n"
            f"{entry_note}\n\n"
            f"━━━ 風險管理 ━━━\n"
            f"🛑 停損: `${stop_loss:,.2f}`\n"
            f"🎯 目標1: `${tp1:,.2f}` (+{(tp1-price)/price*100:.1f}%)\n"
            f"🎯 目標2: `${tp2:,.2f}` (+{(tp2-price)/price*100:.1f}%)\n"
            f"📊 風報比: `1 : {rr:.1f}` {'✅' if rr >= 2 else '⚠️ 偏低'}\n\n"
        )
    else:
        trade_block = "⚠️ 結構不明確，建議觀望等待更清晰訊號\n\n"
    warnings = []
    if abs(m.change_pct) > 8:
        warnings.append(f"⚠️ 今日已漲跌 {m.change_pct:+.1f}%，波動偏大")
    if m.short_float >= 0.20:
        warnings.append(f"⚠️ 空頭比例高 {m.short_float:.0%}，可能劇烈波動")
    if m.beta >= 2.0:
        warnings.append(f"⚠️ Beta={m.beta:.1f}，高波動個股")
    if m.confidence < 0.4:
        warnings.append("⚠️ 信心偏低，建議等更多訊號")
    warning_str = "\n".join(warnings) if warnings else "✅ 無特殊警告"
    cap_str = (f"${m.market_cap/1e12:.2f}T" if m.market_cap >= 1e12
               else f"${m.market_cap/1e9:.0f}B")
    chart_block = format_trade_chart_block(m.ticker, "us")
    return (
        f"💡 *{m.ticker} {m.name} 交易建議*\n\n"
        f"收盤: `${price:,.2f}`  {us_market_status()}\n"
        f"方向: {dir_str}  信心: {conf_str}\n"
        f"產業: {m.sector or '—'}  市值: {cap_str}\n\n"
        f"{trade_block}"
        f"{pos_block}\n\n"
        f"━━━ 技術面 ━━━\n"
        f"RSI: {m.rsi:.0f}  Beta: {m.beta:.1f}\n"
        f"MA20: ${m.ma20:,.2f}  距52W高: {m.dist_52w_high_pct:.1f}%\n\n"
        f"━━━ 空頭部位 ━━━\n"
        f"空頭比: {m.short_float:.1%}  回補天數: {m.short_ratio:.1f} 天\n"
        f"法人持股: {m.inst_pct:.1%}\n\n"
        f"━━━ 注意事項 ━━━\n"
        f"{warning_str}\n"
        f"{chart_block}\n\n"
        f"_⚠️ 純技術分析，非投資建議，請自行控管風險_"
    )

# ── 美股指令 ──────────────────────────────────────────────
# ──────────────────────────────────────────────
#  等待訊息 helper（快取是否已暖）
# ──────────────────────────────────────────────
def _tw_wait_msg(action: str = "🔍 掃描台股") -> str:
    warm = bool(TW_CACHE.get("data"))
    if warm:
        return f"{action}中... ⚡ 快取命中，約 1~3 秒"
    return f"{action}中... ⏳ 首次掃描約 20~30 秒，請稍候"

def _us_wait_msg(action: str = "🔍 掃描美股") -> str:
    warm = bool(US_CACHE.get("data"))
    if warm:
        return f"{action}中... ⚡ 快取命中，約 1~3 秒"
    return f"{action}中... ⏳ 首次掃描約 90~150 秒，請稍候"

def _crypto_wait_msg(action: str = "🔍 掃描加密貨幣") -> str:
    warm = bool(LAST_SCAN.get("data"))
    if warm:
        return f"{action}中... ⚡ 快取命中，約 1~3 秒"
    return f"{action}中... ⏳ 首次掃描約 30~60 秒，請稍候"

async def cmd_us_scan(update, ctx):
    await update.message.reply_text(_us_wait_msg())
    stocks  = await get_us_scan()
    f = US_USER_FILTERS.get(update.effective_user.id, DEFAULT_US_FILTERS)
    filtered = apply_us_filters(stocks, f)
    text = fmt_us_list(filtered, f"🇺🇸 美股預備暴漲榜（{len(filtered)} 命中）")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_us_squeeze(update, ctx):
    await update.message.reply_text(_us_wait_msg("🎯 偵測美股 BB 壓縮"))
    stocks = await get_us_scan()
    sq = find_us_squeeze(stocks)
    text = fmt_us_list(sq, "🎯 美股 BB 壓縮蓄勢榜")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_us_short(update, ctx):
    await update.message.reply_text(_us_wait_msg("🎯 偵測軋空候選"))
    stocks = await get_us_scan()
    sq = find_us_short_squeeze(stocks)
    text = fmt_us_list(sq, "🎯 美股軋空候選榜（高空頭比+技術轉強）")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_us_momentum(update, ctx):
    await update.message.reply_text(_us_wait_msg())
    stocks = await get_us_scan()
    mo = find_us_momentum(stocks)
    text = fmt_us_list(mo, "📈 美股動能榜（RSI+均線排列）")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_us_top10(update, ctx):
    await update.message.reply_text(_us_wait_msg())
    stocks = await get_us_scan()
    text = fmt_us_list(stocks[:10], "🏆 美股綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_us_trade(update, ctx):
    args = ctx.args
    if not args:
        await update.message.reply_text("用法：`/us_trade AAPL`", parse_mode=ParseMode.MARKDOWN)
        return
    ticker = args[0].upper()
    await update.message.reply_text(f"💡 分析 {ticker} 中...")
    stocks = await get_us_scan()
    m = next((s for s in stocks if s.ticker == ticker), None)
    if m is None:
        # 不在快取中，單獨抓取
        from us_stock_scorer import score_us_stock
        from us_data_fetcher import fetch_one_us_stock
        raw = await fetch_one_us_stock(ticker)
        if not raw.fetch_ok:
            await update.message.reply_text(f"❌ 找不到 {ticker}，請確認代號")
            return
        m = score_us_stock(raw)
    text = generate_us_trade_advice(m)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=False)

async def cmd_us_detail(update, ctx):
    args = ctx.args
    if not args:
        await update.message.reply_text("用法：`/us_detail AAPL`", parse_mode=ParseMode.MARKDOWN)
        return
    ticker = args[0].upper()
    stocks = await get_us_scan()
    m = next((s for s in stocks if s.ticker == ticker), None)
    if m is None:
        await update.message.reply_text(f"❌ {ticker} 不在掃描清單中，請先執行 /us_scan")
        return
    chart_multi = get_tv_chart_url(m.ticker, "D", "us")
    msg = (
        f"🔬 *{m.ticker} {m.name} 詳細指標*\n\n"
        f"收盤: `${m.close:,.2f}`  漲跌: `{m.change_pct:+.2f}%`\n"
        f"量比: `{m.volume_ratio:.2f}x`  Beta: `{m.beta:.2f}`\n"
        f"市值: `{'${:.2f}T'.format(m.market_cap/1e12) if m.market_cap>=1e12 else '${:.0f}B'.format(m.market_cap/1e9)}`\n\n"
        f"*技術指標*\n"
        f"RSI: `{m.rsi:.1f}`  MA20: `${m.ma20:,.2f}`  MA60: `${m.ma60:,.2f}`\n"
        f"距52W高: `{m.dist_52w_high_pct:.1f}%`  52W低: `${m.week52_low:,.2f}`\n\n"
        f"*空頭部位*\n"
        f"空頭比例: `{m.short_float:.1%}`  回補天數: `{m.short_ratio:.1f}`\n\n"
        f"*法人*\n"
        f"持股比例: `{m.inst_pct:.1%}`\n\n"
        f"*評分明細*\n"
        f"BB壓縮: `{m.bb_score:.0f}`  量能: `{m.vol_score:.0f}`\n"
        f"動能: `{m.momentum_score:.0f}`  ATR: `{m.atr_score:.0f}`\n"
        f"軋空潛力: `{m.short_squeeze_score:.0f}`  法人: `{m.institution_score:.0f}`\n"
        f"OB+FVG: `{m.ob_fvg_score:.0f}`  *總分: `{m.total_score:.0f}`*\n\n"
        f"[📊 TV 圖表]({chart_multi})"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=False)

async def cmd_us_status(update, ctx):
    n = len(US_CACHE.get("data", []))
    mkt = us_market_status()
    et_now = datetime.now(_et_tz())
    msg = (
        f"🇺🇸 *美股 Bot 狀態*\n\n"
        f"市場: {mkt}\n"
        f"美東時間: {et_now:%Y-%m-%d %H:%M} ET\n"
        f"台灣時間: {datetime.now(TW_TZ):%H:%M}\n"
        f"快取股票數: {n} 支\n"
        f"訂閱人數: {len(US_SUBSCRIBERS)}\n\n"
        f"指令：/us\\_scan  /us\\_squeeze  /us\\_short\n"
        f"/us\\_momentum  /us\\_top10\n"
        f"/us\\_trade AAPL  /us\\_detail AAPL"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_us_sub(update, ctx):
    cid = update.effective_chat.id
    US_SUBSCRIBERS.add(cid)
    _db.save_subscriber("us", cid)
    await update.message.reply_text("🔔 已訂閱美股盤前預警！（週一到週五 21:00 台灣時間推送）")

async def cmd_us_unsub(update, ctx):
    cid = update.effective_chat.id
    US_SUBSCRIBERS.discard(cid)
    _db.delete_subscriber("us", cid)
    await update.message.reply_text("🔕 已取消美股訂閱")

async def push_us_premarket(ctx):
    """美股盤前預警：每日 21:00 台灣時間（= 09:00 ET 開盤前 30 分）"""
    if not US_SUBSCRIBERS:
        return
    now_tw = datetime.now(TW_TZ)
    if now_tw.weekday() >= 5:   # 跳過週末
        return
    log.info(f"[US] 推送美股盤前預警給 {len(US_SUBSCRIBERS)} 人")
    try:
        stocks = await fetch_all_us_metrics(top_n=80)
    except Exception as e:
        log.exception(e); return
    filtered = apply_us_filters(stocks)[:5]
    if not filtered:
        return
    parts = [f"🇺🇸 *美股盤前預警*  _{now_tw:%m/%d %H:%M}_\n"]
    for i, m in enumerate(filtered, 1):
        parts.append(fmt_us_card(m, i, show_triggers=True))
        parts.append("")
    parts.append("_資料源: Yahoo Finance (yfinance)_")
    text = "\n".join(parts)
    for cid in list(US_SUBSCRIBERS):
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN,
                                       disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"美股推送失敗 {cid}: {e}")
            US_SUBSCRIBERS.discard(cid)

async def get_tw_scan(force=False) -> list[TwStockMetrics]:
    now = asyncio.get_event_loop().time()
    if not force and now - TW_CACHE["time"] < TW_CACHE_TTL and TW_CACHE["data"]:
        return TW_CACHE["data"]
    data = await fetch_all_tw_metrics(top_n=100)
    TW_CACHE.update({"time": now, "data": data})
    gc.collect()
    # ML 真實特徵儲存（前 30 高分）
    try:
        from db import save_ml_scan
        _dow = datetime.now().weekday()
        for _rank, m in enumerate(sorted(data, key=lambda x: x.total_score, reverse=True)[:30], 1):
            save_ml_scan(
                symbol=m.stock_id, market="tw",
                total_score=m.total_score, early_score=m.early_score,
                confidence=m.confidence, price=m.close,
                change_pct=m.change_pct,
                feat1=m.foreign_net / 1e8,   # 外資淨買(億)
                feat2=m.foreign_streak,       # 連買天數
                feat3=m.score_bb,             # BB 壓縮分
                feat4=m.margin_change_pct,    # 融資變化
                btc_change_24h=_BTC_TREND.get("change_24h", 0.0),
                consistency_score=0,    # 台股無加密指標
                dow=_dow, rank_in_session=_rank,
            )
    except Exception as _e:
        log.debug(f"[ML] tw 儲存失敗: {_e}")
    return data

def fmt_tw_card(m: TwStockMetrics, rank=None, show_triggers=True) -> str:
    rank_str = f"#{rank} " if rank else ""
    tag_str  = "  ".join(m.tags) if m.tags else ""
    val_str  = f"{m.trade_value/1e8:.1f}億" if m.trade_value >= 1e8 else f"{m.trade_value/1e6:.0f}百萬"
    lines = [
        f"{rank_str}*{m.stock_id} {m.name}*  {m.direction}",
        f"├ 總分 *{m.total_score:.0f}*  領先 *{m.early_score:.0f}*  信心 *{m.confidence:.0%}*",
        f"├ 收盤: ${m.close:,.2f}  漲跌: {m.change_pct:+.2f}%",
        f"├ 成交: {val_str}",
        f"├ 外資: {m.foreign_net/1e8:+.1f}億  連續: {m.foreign_streak:+d}天",
        f"├ 融資變化: {m.margin_change_pct:+.1f}%  融券變化: {m.short_change_pct:+.1f}%",
    ]
    if m.support or m.resistance:
        s = f"${m.support:,.2f}"    if m.support    else "—"
        r = f"${m.resistance:,.2f}" if m.resistance else "—"
        lines.append(f"├ 支撐: {s}  阻力: {r}")
    if show_triggers and m.triggers:
        for t in m.triggers[:3]:
            lines.append(f"├ {t}")
    if tag_str:
        lines.append(f"├ {tag_str}")
    chart_url = get_tv_chart_url(m.stock_id, timeframe="D", market="tw")
    lines.append(f"└ [📊 TV 圖表]({chart_url})")
    return "\n".join(lines)

def fmt_tw_list(stocks, title, show_triggers=True) -> str:
    if not stocks:
        return f"*{title}*\n\n目前沒有符合條件的標的 🌙"
    mkt = tw_market_status()
    out = [f"*{title}*  _{datetime.now(TW_TZ):%H:%M}_  {mkt}\n"]
    for i, m in enumerate(stocks[:8], 1):
        out.append(fmt_tw_card(m, i, show_triggers))
        out.append("")
    out.append("_資料源: FinMind API + TWSE_")
    return "\n".join(out)

def generate_tw_trade_advice(m: TwStockMetrics) -> str:
    price   = m.close
    is_bull = any(e in m.direction for e in ["🚀", "🔋", "🔵"])
    is_bear = any(e in m.direction for e in ["📉", "⚠️"])
    dir_str = "做多 🟢" if is_bull else "做空 🔴" if is_bear else "觀望 ⚪"
    conf    = m.confidence
    if conf >= 0.8:    conf_str = "非常高 ⭐⭐⭐"
    elif conf >= 0.65: conf_str = "高 ⭐⭐"
    elif conf >= 0.5:  conf_str = "中等 ⭐"
    else:              conf_str = "偏低 ⚠️"
    pos = m.position_pct
    pos_block = (
        "🚫 信心不足，暫時觀望" if pos == 0 else
        f"━━━ 倉位建議（現股無槓桿）━━━\n"
        f"🟢 保守: 總資金 `{pos//2}%`\n"
        f"🟡 標準: 總資金 `{pos}%`\n"
        f"🔴 積極: 總資金 `{min(pos*2,30)}%`"
    )
    if is_bull and m.support:
        entry_low  = m.support * 0.998
        entry_high = m.support * 1.015
        stop_loss  = m.support * 0.975
        tp1 = m.resistance if m.resistance else price * 1.08
        tp2 = tp1 * 1.05
        rr  = (tp1 - entry_high) / (entry_high - stop_loss) if entry_high > stop_loss else 0
        in_zone    = entry_low <= price <= entry_high * 1.02
        entry_note = "✅ 當前在進場區！" if in_zone else f"⏳ 等待回測，距進場區 {(price-entry_high)/price*100:.1f}%"
        trade_block = (
            f"━━━ 進場區間 ━━━\n"
            f"📍 理想進場: `${entry_low:,.2f}` ~ `${entry_high:,.2f}`\n"
            f"📍 OB 50%: `${(entry_low+entry_high)/2:,.2f}` ← 最佳點\n"
            f"{entry_note}\n\n"
            f"━━━ 風險管理 ━━━\n"
            f"🛑 停損: `${stop_loss:,.2f}` (跌破出場)\n"
            f"🎯 目標1: `${tp1:,.2f}` (+{(tp1-price)/price*100:.1f}%)\n"
            f"🎯 目標2: `${tp2:,.2f}` (+{(tp2-price)/price*100:.1f}%)\n"
            f"📊 風報比: `1 : {rr:.1f}` {'✅' if rr >= 2 else '⚠️ 偏低'}\n\n"
        )
    else:
        trade_block = "⚠️ 結構不明確，建議觀望等待更清晰訊號\n\n"
    warnings = []
    if abs(m.change_pct) > 7:
        warnings.append(f"⚠️ 今日已漲跌 {m.change_pct:+.1f}%，接近漲跌停")
    if m.confidence < 0.4:
        warnings.append("⚠️ 信心偏低，建議等更多訊號")
    if m.trade_value < 5e8:
        warnings.append("⚠️ 成交金額偏低，注意流動性")
    warning_str = "\n".join(warnings) if warnings else "✅ 無特殊警告"
    return (
        f"💡 *{m.stock_id} {m.name} 交易建議*\n\n"
        f"收盤: `${price:,.2f}`  {tw_market_status()}\n"
        f"方向: {dir_str}\n"
        f"信心: {conf_str}\n\n"
        f"{trade_block}"
        f"{pos_block}\n\n"
        f"━━━ 法人動向 ━━━\n"
        f"外資: {m.foreign_net/1e8:+.1f}億  連續 {m.foreign_streak:+d} 天\n"
        f"三大合計: {m.institutional_net/1e8:+.1f}億\n"
        f"融資變化: {m.margin_change_pct:+.1f}%  融券變化: {m.short_change_pct:+.1f}%\n\n"
        f"━━━ 注意事項 ━━━\n"
        f"{warning_str}\n\n"
        f"_⚠️ 純技術分析，非投資建議，請自行控管風險_"
    )

# ── 台股指令 ──────────────────────────────────────────────
async def cmd_tw_scan(update, ctx):
    await update.message.reply_text(_tw_wait_msg())
    stocks   = await get_tw_scan()
    uid = update.effective_user.id
    f = get_tw_filter(uid)
    filtered = apply_tw_filters(find_tw_pre_pump(stocks), f)
    text = fmt_tw_list(filtered, f"🔋 台股預備暴漲榜 ({len(filtered)} 命中)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_squeeze(update, ctx):
    await update.message.reply_text(_tw_wait_msg("🎯 偵測台股 BB 壓縮"))
    stocks = await get_tw_scan()
    sq     = find_tw_squeeze(stocks)
    text   = fmt_tw_list(sq, "🎯 台股 BB 壓縮蓄勢榜")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_foreign(update, ctx):
    await update.message.reply_text(_tw_wait_msg())
    stocks  = await get_tw_scan()
    foreign = find_tw_institutional_buy(stocks)
    text    = fmt_tw_list(foreign, "🏦 外資連續買超榜（≥3天）")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_top10(update, ctx):
    await update.message.reply_text(_tw_wait_msg())
    stocks = await get_tw_scan()
    text   = fmt_tw_list(stocks[:10], "🏆 台股綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_trade(update, ctx):
    if not ctx.args:
        await update.message.reply_text("用法: `/tw_trade 2330`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].strip()
    await update.message.reply_text(f"💡 分析台股 {target} 中...")
    stocks = await get_tw_scan()
    m = next((s for s in stocks if s.stock_id == target), None)
    if not m:
        await update.message.reply_text(
            f"❌ 找不到 {target}，可能成交量太低未列入掃描\n請確認股票代號（台積電是 `2330`）",
            parse_mode=ParseMode.MARKDOWN)
        return
    advice  = generate_tw_trade_advice(m)
    advice += format_trade_chart_block(target, market="tw")
    await update.message.reply_text(advice, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_detail(update, ctx):
    if not ctx.args:
        await update.message.reply_text("用法: `/tw_detail 2330`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].strip()
    stocks = await get_tw_scan()
    m = next((s for s in stocks if s.stock_id == target), None)
    if not m:
        await update.message.reply_text(f"❌ 找不到 {target}")
        return
    msg = (
        f"📊 *{m.stock_id} {m.name} 全維度報告*\n\n"
        f"*綜合分數*: `{m.total_score:.1f}`\n"
        f"*領先分數*: `{m.early_score:.1f}` ← 尚未啟動信號\n"
        f"*方向判讀*: {m.direction}\n"
        f"*訊號信心*: {m.confidence:.0%}\n\n"
        f"*技術指標*\n"
        f"BB 壓縮 `{m.score_bb:.0f}`  量能階梯 `{m.score_vol_ladder:.0f}`\n"
        f"沉睡甦醒 `{m.score_sleep:.0f}`  波動率 `{m.score_atr:.0f}`\n"
        f"CVD 背離 `{m.score_cvd:.0f}`  OB+FVG `{m.score_ob_fvg:.0f}`\n\n"
        f"*台股特有*\n"
        f"法人動向 `{m.score_institution:.0f}`  融資融券 `{m.score_margin:.0f}`\n\n"
        f"*法人明細*\n"
        f"外資: `{m.foreign_net/1e8:+.2f}` 億  連續 `{m.foreign_streak:+d}` 天\n"
        f"三大法人合計: `{m.institutional_net/1e8:+.2f}` 億\n"
        f"融資餘額變化: `{m.margin_change_pct:+.1f}%`\n"
        f"融券餘額變化: `{m.short_change_pct:+.1f}%`\n"
    )
    if m.triggers:
        msg += "\n*觸發訊號*\n" + "\n".join(f"• {t}" for t in m.triggers)
    msg += f"\n\n💡 `/tw_trade {target}` 查看完整交易建議"
    msg += format_trade_chart_block(target, market="tw")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_tw_status(update, ctx):
    stocks    = TW_CACHE.get("data", [])
    scan_time = TW_CACHE.get("time", 0)
    last_str  = "尚未掃描" if scan_time == 0 else (
        f"{int(asyncio.get_event_loop().time()-scan_time)//60} 分鐘前"
    )
    msg = (
        f"🇹🇼 *台股 Bot 狀態*\n\n"
        f"{tw_market_status()}\n"
        f"上次掃描: `{last_str}`\n"
        f"掃描股票數: `{len(stocks)}`\n\n"
        f"*當前訊號*\n"
        f"🔋 預備暴漲: `{len(find_tw_pre_pump(stocks))}` 支\n"
        f"🎯 BB 壓縮: `{len(find_tw_squeeze(stocks))}` 支\n"
        f"🏦 外資連買: `{len(find_tw_institutional_buy(stocks))}` 支\n\n"
        f"*台股訂閱人數*: `{len(TW_SUBSCRIBERS)}` 人\n\n"
        f"_資料源: FinMind API + TWSE_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_tw_sub(update, ctx):
    cid = update.effective_chat.id
    TW_SUBSCRIBERS.add(cid)
    _db.save_subscriber("tw", cid)
    await update.message.reply_text(
        "🇹🇼 已訂閱台股預警！\n每日 08:50（開盤前）自動推送外資買超與蓄勢標的")

async def cmd_tw_unsub(update, ctx):
    cid = update.effective_chat.id
    TW_SUBSCRIBERS.discard(cid)
    _db.delete_subscriber("tw", cid)
    await update.message.reply_text("🔕 已取消台股訂閱")


# ── 台股法人聯手榜 ──────────────────────────────────────
async def cmd_tw_joint(update, ctx):
    """法人聯手榜：外資+投信同日買超"""
    await update.message.reply_text(_tw_wait_msg("🤝 查詢法人聯手"))
    stocks = await get_tw_scan()
    joint  = find_tw_joint_buy(stocks)
    if not joint:
        await update.message.reply_text("📭 今日無外資+投信同時買超的標的")
        return
    text = fmt_tw_list(joint[:8], f"🤝 台股法人聯手榜 ({len(joint)} 支)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

# ── 美股 RS Rating 榜 ──────────────────────────────────
async def cmd_us_rs(update, ctx):
    """RS Rating 領導股榜單"""
    await update.message.reply_text(_us_wait_msg("🏆 查詢 RS Rating 榜"))
    stocks = await get_us_scan()
    rs     = find_us_rs_leaders(stocks)
    if not rs:
        await update.message.reply_text("📭 暫無 RS80+ 的領導股")
        return
    lines = ["🏆 *美股 RS Rating 領導股榜*\n"]
    for i, m in enumerate(rs[:8], 1):
        bar = "🟢" if m.rs_rating >= 90 else "🔵"
        lines.append(
            bar + " *" + m.ticker + "* `RS=" + f"{m.rs_rating:.0f}" + "` 總分`" + f"{m.total_score:.0f}" + "`\n"
            "  " + m.direction + f"  52W高`{m.dist_52w_high_pct:+.1f}%`  `" + (lambda p: f"法人{p:.0%}" if p >= 0 else "法人載入中...")(get_inst_pct(m.ticker)) + "`\n"
        )
    lines.append("_RS Rating = 近 12 個月相對大盤強度_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_us_accum(update, ctx):
    """法人累積榜（A/D 比強）"""
    await update.message.reply_text(_us_wait_msg("🏦 查詢法人累積榜"))
    stocks = await get_us_scan()
    ac     = find_us_accum(stocks)
    if not ac:
        await update.message.reply_text("📭 暫無法人強力累積標的")
        return
    lines = ["🏦 *美股法人累積榜*\n"]
    for i, m in enumerate(ac[:8], 1):
        lines.append(
            f"*{i}. {m.ticker}* 累積分`{m.accum_score:.0f}` RS=`{m.rs_rating:.0f}`\n"
            f"  {m.direction}  `" + (lambda p: f"法人{p:.0%}" if p >= 0 else "法人載入中...")(get_inst_pct(m.ticker)) + "`\n"
        )
    lines.append("_A/D = 上漲日成交量 / 下跌日成交量，>1.2 = 法人在買_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ── 加密聰明錢 ──────────────────────────────────────────
async def cmd_smartmoney(update, ctx):
    """頂級交易者倉位異常 = 聰明錢信號"""
    try:
        coins = LAST_SCAN.get("data", [])
        if not coins:
            await update.message.reply_text("⏳ 快取尚未建立，請稍候 30 秒後再試")
            return
        smart = find_smart_money(coins)
        n_total = len(coins)
        n_tt = sum(1 for c in coins if getattr(c, "has_tt_data", False))
        if not smart:
            if n_tt == 0:
                msg_no = (f"📭 無聰明錢信號（掃描 {n_total} 幣）\n"
                          f"⚠️ 頂級多空資料尚未載入，背景掃描完成後（約 10 分鐘）再試")
            else:
                msg_no = (f"📭 目前無聰明錢信號\n"
                          f"📊 已掃描 {n_total} 幣，其中 {n_tt} 幣有頂級多空資料")
            await update.message.reply_text(msg_no)
            return
        n_total = len(coins)
        n_tt = sum(1 for c in coins if getattr(c, "has_tt_data", False))
        msg = f"🐋 *聰明錢偵測榜*（{n_tt}/{n_total} 幣有頂級資料）\n\n"
        for i, c in enumerate(smart[:6], 1):
            reason = getattr(c, "_smart_reason", "")
            tt = getattr(c, "top_trader_ls_ratio", 1.0)
            fr = getattr(c, "funding_rate", 0.0)
            msg += f"*{i}. {c.base}* `${c.last_price:,.4g}` {c.price_change_pct:+.1f}%\n"
            msg += f"  {reason}\n"
            msg += f"  FR=`{fr*100:.3f}%` 頂級多空=`{tt:.2f}`\n\n"
        msg += "_頂級交易者 = Binance 前 20% 資金量帳戶_"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error(f"[smartmoney] 錯誤: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 錯誤: {e}")


# ── 台股排程推送 ──────────────────────────────────────────
async def push_tw_morning(ctx):
    """每日 08:50 推送台股開盤前預警"""
    if not TW_SUBSCRIBERS:
        return
    try:
        stocks = await get_tw_scan(force=True)
    except Exception as e:
        log.error(f"台股早盤推送失敗: {e}")
        return
    pre   = find_tw_pre_pump(stocks)[:3]
    fore  = find_tw_institutional_buy(stocks)[:3]
    joint = find_tw_joint_buy(stocks)[:3]
    if not pre and not fore and not joint:
        return
    parts = [f"🇹🇼 *台股開盤前預警*  _{datetime.now(TW_TZ):%m/%d %H:%M}_\n"]
    if joint:
        parts.append("*🤝 法人聯手買超*")
        for i, m in enumerate(joint, 1):
            parts.append(fmt_tw_card(m, i, show_triggers=False))
            parts.append("")
    if pre:
        parts.append("*🔋 技術面蓄勢*")
        for i, m in enumerate(pre, 1):
            parts.append(fmt_tw_card(m, i, show_triggers=True))
            parts.append("")
    if fore:
        parts.append("*🏦 外資連續買超*")
        for i, m in enumerate(fore, 1):
            parts.append(fmt_tw_card(m, i, show_triggers=False))
            parts.append("")
    parts.append("💡 `/tw_trade 代號` 查看完整建議")
    text = "\n".join(parts)
    for cid in list(TW_SUBSCRIBERS):
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN,
                                       disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"台股推送失敗 {cid}: {e}")
            TW_SUBSCRIBERS.discard(cid)

# ============================================================
# V2.1: TradingView 指令 Wrapper
# （讓 async lambda 問題消失，且可直接存取全域 tv_handler）
# ============================================================
async def cmd_sub_tv(update, ctx):
    """/sub_tv — 訂閱 TradingView Webhook 信號"""
    if tv_handler is None:
        await update.message.reply_text("⚠️ Webhook 伺服器尚未就緒，請稍後再試")
        return
    await _cmd_sub_tv(update, ctx, handler=tv_handler)

async def cmd_unsub_tv(update, ctx):
    """/unsub_tv — 取消訂閱"""
    if tv_handler is None:
        await update.message.reply_text("⚠️ Webhook 伺服器尚未就緒")
        return
    await _cmd_unsub_tv(update, ctx, handler=tv_handler)

async def cmd_tv_status(update, ctx):
    """/tv_status — 查看 Webhook 狀態與最近信號"""
    if tv_handler is None:
        await update.message.reply_text("⚠️ Webhook 伺服器尚未就緒")
        return
    await _cmd_tv_status(update, ctx, handler=tv_handler)

# ============================================================
# 指令
# ============================================================
async def cmd_start(update, ctx):
    if not update.effective_message: return
    msg = (
        "👹 *妖幣雷達 V2.3*  _加密 × 台股 × 美股 × TradingView_\n\n"
        "🎯 核心改造: 抓「即將妖動」而非「已經妖動」\n\n"
        "*🪙 加密貨幣*\n"
        "/pre\\_pump 🔋  /pre\\_dump ⚠️  /squeeze 🎯\n"
        "/trade BTC 💡  /detail BTC  /structure BTC\n"
        "/scan  /top10  /pump  /dump  /status\n\n"
        "*🇹🇼 台股*\n"
        "/tw\\_scan 🔋  /tw\\_squeeze 🎯  /tw\\_foreign 🏦\n"
        "/tw\\_trade 2330 💡  /tw\\_detail 2330\n"
        "/tw\\_top10  /tw\\_status  /tw\\_sub\n\n"
        "*🇺🇸 美股*\n"
        "/us\\_scan 🔋  /us\\_squeeze 🎯  /us\\_short 🎯\n"
        "/us\\_trade AAPL 💡  /us\\_detail AAPL\n"
        "/us\\_momentum  /us\\_top10  /us\\_status  /us\\_sub\n\n"
        "*📡 TradingView*\n"
        "/sub\\_tv  /unsub\\_tv  /tv\\_status\n\n"
        "/help - 完整說明"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ── /help 指令內容字典 ────────────────────────────────────
_HELP_SECTIONS = {
    "crypto": (
        "*🪙 加密貨幣指令*\n\n"
        "`/pre_pump` 預備暴漲榜\n"
        "`/pre_dump` 預備暴跌榜\n"
        "`/squeeze` BB 壓縮蓄勢\n"
        "`/confidence` 高信心榜\n"
        "`/smartmoney` 🐋 聰明錢偵測\n"
        "`/scan` 個人化掃描\n"
        "`/top10` 綜合 Top 10\n"
        "`/pump` 看多榜  `/dump` 看空榜\n"
        "`/trade BTC` 💡 完整交易建議\n"
        "`/detail BTC` 詳細指標\n"
        "`/structure BTC` OB+FVG 結構分析"
    ),
    "tw": (
        "*🇹🇼 台股指令*\n\n"
        "`/tw_scan` 預備暴漲榜\n"
        "`/tw_squeeze` BB 壓縮蓄勢\n"
        "`/tw_foreign` 外資連買榜\n"
        "`/tw_joint` 🤝 法人聯手榜\n"
        "`/tw_top10` 台股 Top 10\n"
        "`/tw_trade 2330` 💡 完整交易建議\n"
        "`/tw_detail 2330` 詳細指標\n"
        "`/tw_status` 台股 Bot 狀態"
    ),
    "us": (
        "*🇺🇸 美股指令*\n\n"
        "`/us_scan` 預備暴漲榜\n"
        "`/us_squeeze` BB 壓縮蓄勢\n"
        "`/us_short` 軋空候選\n"
        "`/us_momentum` 動能榜\n"
        "`/us_rs` 📊 RS Rating 領導股\n"
        "`/us_accum` 🏛️ 法人累積榜\n"
        "`/us_top10` 美股 Top 10\n"
        "`/us_trade AAPL` 💡 完整交易建議\n"
        "`/us_detail AAPL` 詳細指標\n"
        "`/us_status` 美股 Bot 狀態"
    ),
    "general": (
        "*🔍 通用查詢*\n\n"
        "`/stock 2330` 個股查詢\n"
        "（台股數字代號 / 加密幣種 / 美股英文代號，自動判斷）\n\n"
        "`/status` Bot 運作狀態"
    ),
    "personal": (
        "*🎛️ 個人化操作*\n\n"
        "*篩選設定（加密）*\n"
        "`/set_score 55` 最低總分門檻（預設 55）\n"
        "`/set_early 40` 最低早分門檻（預設 40）\n"
        "`/set_max_change 15` 最大已動 %（預設 15）\n"
        "`/myfilters` 查看我的設定\n"
        "`/reset` 重設為預設值\n\n"
        "*⭐ 自選股 & 警報*\n"
        "`/watch BTC` 加入自選股（台股/加密/美股皆可）\n"
        "`/unwatch BTC` 移除自選股\n"
        "`/mywatchlist` 我的自選股即時狀態\n"
        "`/alert foreign 3` 外資連買 N 天警報\n"
        "`/alert pre 70` 評分達 N 分警報"
    ),
    "position": (
        "*📥 倉位追蹤*\n\n"
        "`/enter BTC 95000 l 10`\n"
        "進場記錄，參數：代號 價格 方向(l/s) 槓桿\n"
        "Bot 自動計算 TP/SL，Trailing Stop 背景監控\n\n"
        "`/close BTC 98000` 手動平倉\n"
        "`/positions` 查看開倉 & 近期平倉紀錄\n"
        "`/tpsl BTC 105000 88000` 自訂止盈止損"
    ),
    "sub": "sub_panel",   # 特殊標記，改用訂閱面板
}

def _help_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 加密貨幣", callback_data="help:crypto"),
            InlineKeyboardButton("🇹🇼 台股",    callback_data="help:tw"),
            InlineKeyboardButton("🇺🇸 美股",    callback_data="help:us"),
        ],
        [
            InlineKeyboardButton("🔍 通用查詢",   callback_data="help:general"),
            InlineKeyboardButton("🎛️ 個人化操作", callback_data="help:personal"),
        ],
        [
            InlineKeyboardButton("📥 倉位追蹤",  callback_data="help:position"),
            InlineKeyboardButton("🔔 訂閱推播",  callback_data="help:sub"),
        ],
        [
            InlineKeyboardButton("📖 圖文使用說明", url="https://xiang14786.github.io/yaobi-bot/"),
        ],
    ])

async def cmd_help(update, ctx):
    if not update.effective_message: return
    await update.effective_message.reply_text(
        "*🤖 妖幣雷達 V2.3*\n選擇想查看的分類：\n\n💡 指令底線可省略，例如 `/twscan` = `/tw\_scan`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_help_menu_keyboard()
    )

async def cb_help_section(update, ctx):
    """處理 /help 分類按鈕點擊"""
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    # 訂閱類別 → 直接顯示訂閱面板
    if key == "sub":
        cid = query.message.chat_id  # 群組訂閱送群組，私聊訂閱送私聊
        await query.edit_message_text(
            _sub_text(cid), parse_mode=ParseMode.MARKDOWN,
            reply_markup=_sub_keyboard_with_back(cid)
        )
        return
    text = _HELP_SECTIONS.get(key, "❌ 未知分類")
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ 返回選單", callback_data="help:menu")
    ]])
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)

async def cb_help_menu(update, ctx):
    """返回 /help 主選單"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*🤖 妖幣雷達 V2.3*\n選擇想查看的分類：",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_help_menu_keyboard()
    )

async def cmd_pre_pump(update, ctx):
    await update.message.reply_text(_crypto_wait_msg("🔋 偵測蓄勢拉升"))
    coins = await get_scan()
    pre = find_pre_pump(coins)
    text = fmt_list(pre, "🔋 預備暴漲榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_pre_dump(update, ctx):
    await update.message.reply_text(_crypto_wait_msg("⚠️ 偵測蓄勢下殺"))
    coins = await get_scan()
    pre = find_pre_dump(coins)
    text = fmt_list(pre, "⚠️ 預備暴跌榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_squeeze(update, ctx):
    await update.message.reply_text(_crypto_wait_msg("🎯 偵測壓縮蓄勢"))
    coins = await get_scan()
    sq = find_squeeze(coins)
    text = fmt_list(sq, "🎯 壓縮蓄勢榜 (BB+OI)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_confidence(update, ctx):
    await update.message.reply_text(_crypto_wait_msg())
    coins = await get_scan()
    high = [c for c in coins if c.confidence >= 0.6]
    high.sort(key=lambda c: c.confidence, reverse=True)
    text = fmt_list(high, "🏆 高信心度榜 (多訊號共振)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_scan(update, ctx):
    await update.message.reply_text(_crypto_wait_msg())
    coins = await get_scan()
    f = get_user_filter(update.effective_user.id)
    filtered = apply_filters_v2(coins, f)
    text = fmt_list(filtered, f"👹 妖幣雷達 V2 ({len(filtered)} 命中)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_top10(update, ctx):
    await update.message.reply_text(_crypto_wait_msg())
    coins = await get_scan()
    text = fmt_list(coins, "🏆 綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_pump(update, ctx):
    await update.message.reply_text(_crypto_wait_msg())
    coins = await get_scan()
    pumps = [c for c in coins if "🚀" in c.direction or "🔋" in c.direction]
    text = fmt_list(pumps, "🚀 看多榜 (含預警+延續)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_dump(update, ctx):
    await update.message.reply_text(_crypto_wait_msg())
    coins = await get_scan()
    dumps = [c for c in coins if "📉" in c.direction or "⚠️" in c.direction]
    text = fmt_list(dumps, "📉 看空榜 (含預警+延續)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_trade(update, ctx):
    """完整交易建議指令 — V2.1 附多時框圖表連結"""
    if not ctx.args:
        await update.message.reply_text("用法: `/trade BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].upper().replace("USDT", "")
    await update.message.reply_text(f"💡 分析 {target} 中...")
    coins = await get_scan()
    m = next((c for c in coins if c.base == target), None)
    if not m:
        await update.message.reply_text(f"❌ 找不到 {target}，可能成交額太低未列入掃描")
        return
    advice = generate_trade_advice(m)
    # V2.1: 附多時框圖表連結 (15m / 1h / 4h / 日線)
    advice += format_trade_chart_block(target)
    await update.message.reply_text(advice, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_detail(update, ctx):
    if not ctx.args:
        await update.message.reply_text("用法: `/detail BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].upper().replace("USDT", "")
    coins = await get_scan()
    m = next((c for c in coins if c.base == target), None)
    if not m:
        await update.message.reply_text(f"❌ 找不到 {target}")
        return
    msg = (
        f"📊 *{m.base}/USDT 全維度報告*\n\n"
        f"*綜合分數*: `{m.total_score:.1f}`\n"
        f"*早期分數*: `{m.score_early:.1f}`  ← 領先指標\n"
        f"*結構分數*: `{m.score_structure:.1f}`  ← OB+FVG\n"
        f"*方向判讀*: {m.direction}\n"
        f"*訊號信心*: {m.confidence:.0%}\n\n"
        f"*原 7 維*\n"
        f"價格動量 `{m.score_price:.0f}` ({m.price_change_pct:+.2f}%)\n"
        f"成交異動 `{m.score_volume:.0f}`\n"
        f"資金費率 `{m.score_funding:.0f}` ({m.funding_rate*100:+.4f}%)\n"
        f"多空失衡 `{m.score_ls:.0f}` (比 {m.long_short_ratio:.2f})\n"
        f"爆倉強度 `{m.score_liq:.0f}`\n"
        f"情緒極端 `{m.score_sentiment:.0f}`\n"
        f"鏈上代理 `{m.score_onchain:.0f}`\n\n"
    )
    if m.triggers:
        msg += "*觸發訊號*\n" + "\n".join(f"• {t}" for t in m.triggers) + "\n\n"
    if m.nearest_support or m.nearest_resistance:
        msg += "*結構位*\n"
        if m.nearest_support:
            msg += f"支撐 (Bullish OB): `${m.nearest_support:,.4f}`\n"
        if m.nearest_resistance:
            msg += f"阻力 (Bearish OB): `${m.nearest_resistance:,.4f}`\n"
    msg += f"\n💡 輸入 `/trade {m.base}` 查看完整交易建議"
    if m.tags:
        msg += "\n" + " ".join(m.tags)
    # V2.1: 附多時框圖表連結
    msg += format_trade_chart_block(m.base)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_structure(update, ctx):
    if not ctx.args:
        await update.message.reply_text("用法: `/structure BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].upper().replace("USDT", "")
    coins = await get_scan()
    m = next((c for c in coins if c.base == target), None)
    if not m:
        await update.message.reply_text(f"❌ 找不到 {target}")
        return
    price = m.last_price
    msg = (
        f"📐 *{m.base}/USDT 結構分析*\n\n"
        f"當前價: `${price:,.4f}`\n"
        f"結構分: `{m.score_structure:.0f}/100`\n"
        f"結構偏向: `{m.structure_bias}`\n\n"
    )
    if m.nearest_support:
        ob_top    = m.nearest_support
        ob_bottom = m.nearest_support * 0.98
        fvg_50    = (ob_top + ob_bottom) / 2
        dist      = (price - ob_top) / price * 100
        in_zone   = price <= ob_top * 1.02
        msg += (
            f"🟢 *Bullish OB (買入區)*\n"
            f"   上緣: `${ob_top:,.4f}`\n"
            f"   下緣: `${ob_bottom:,.4f}`\n"
            f"   FVG 50%: `${fvg_50:,.4f}` ← 最佳進場點\n"
            f"   {'✅ 當前在支撐區內！' if in_zone else f'距此區域: {dist:.2f}%'}\n\n"
        )
    if m.nearest_resistance:
        ob_top2    = m.nearest_resistance * 1.02
        ob_bottom2 = m.nearest_resistance
        fvg_50_2   = (ob_top2 + ob_bottom2) / 2
        dist2      = (m.nearest_resistance - price) / price * 100
        msg += (
            f"🔴 *Bearish OB (阻力區)*\n"
            f"   上緣: `${ob_top2:,.4f}`\n"
            f"   下緣: `${ob_bottom2:,.4f}`\n"
            f"   FVG 50%: `${fvg_50_2:,.4f}` ← 空單參考點\n"
            f"   距此區域: `{dist2:.2f}%`\n\n"
        )
    if m.nearest_support and m.nearest_resistance:
        potential = (m.nearest_resistance - price) / price * 100
        risk      = (price - m.nearest_support * 0.975) / price * 100
        rr        = potential / risk if risk > 0 else 0
        msg += (
            f"📊 *風報比參考*\n"
            f"   潛在獲利空間: `+{potential:.1f}%`\n"
            f"   風險空間: `-{risk:.1f}%`\n"
            f"   風報比: `1 : {rr:.1f}` {'✅' if rr >= 2 else '⚠️ 偏低'}\n\n"
        )
    msg += f"💡 輸入 `/trade {m.base}` 查看完整交易建議"
    # V2.1: 附多時框圖表連結
    msg += format_trade_chart_block(m.base)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_status(update, ctx):
    coins    = LAST_SCAN.get("data", [])
    scan_time = LAST_SCAN.get("time", 0)
    if scan_time == 0:
        last_scan_str = "尚未掃描"
    else:
        elapsed = int(asyncio.get_event_loop().time() - scan_time)
        if elapsed < 60:
            last_scan_str = f"{elapsed} 秒前"
        elif elapsed < 3600:
            last_scan_str = f"{elapsed // 60} 分鐘前"
        else:
            last_scan_str = f"{elapsed // 3600} 小時前"
    pre_pump  = find_pre_pump(coins)
    pre_dump  = find_pre_dump(coins)
    squeeze   = find_squeeze(coins)
    tv_subs   = tv_handler.subscriber_count() if tv_handler else 0
    tv_count  = len(tv_handler._signal_log)   if tv_handler else 0
    msg = (
        f"🤖 *Bot 運作狀態 V2.3*\n\n"
        f"✅ 運作中\n"
        f"🕐 上次掃描: `{last_scan_str}`\n"
        f"📊 掃描標的數: `{len(coins)}`\n\n"
        f"*當前訊號*\n"
        f"🔋 預備暴漲: `{len(pre_pump)}` 個\n"
        f"⚠️ 預備暴跌: `{len(pre_dump)}` 個\n"
        f"🎯 壓縮蓄勢: `{len(squeeze)}` 個\n\n"
        f"*訂閱人數*\n"
        f"預警訂閱: `{len(PRE_PUMP_SUBSCRIBERS)}` 人\n"
        f"榜單訂閱: `{len(SUBSCRIBERS)}` 人\n"
        f"📡 TV Webhook: `{tv_subs}` 人 (收到 {tv_count} 筆信號)\n\n"
        f"_下次預警推送約 30 分鐘一次_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ============================================================
# 篩選設定
# ============================================================
async def cmd_set_score(update, ctx):
    try:
        v = float(ctx.args[0]); assert 30 <= v <= 95
    except:
        await update.message.reply_text("用法: `/set_score 60`", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    USER_FILTERS.setdefault(uid, {})["min_total_score"] = v
    _db.save_user_filter(uid, "crypto", USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 最低總分 = {v}")

async def cmd_set_early(update, ctx):
    try:
        v = float(ctx.args[0]); assert 0 <= v <= 95
    except:
        await update.message.reply_text("用法: `/set_early 50`", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    USER_FILTERS.setdefault(uid, {})["min_early_score"] = v
    _db.save_user_filter(uid, "crypto", USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 最低早期分 = {v}")

async def cmd_set_max_change(update, ctx):
    try:
        v = float(ctx.args[0]); assert 1 <= v <= 50
    except:
        await update.message.reply_text("用法: `/set_max_change 12`", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    USER_FILTERS.setdefault(uid, {})["max_price_change_pct"] = v
    _db.save_user_filter(uid, "crypto", USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 最大已動幅度 = {v}%")

async def cmd_myfilters(update, ctx):
    uid = update.effective_user.id
    fc  = get_user_filter(uid)
    ftw = get_tw_filter(uid)
    fus = get_us_filter(uid)
    msg = (
        "*你的篩選設定*\n\n"
        "🪙 *加密*\n"
        f"  最低總分: `{fc['min_total_score']}`  最低早分: `{fc['min_early_score']}`\n"
        f"  最大已動: `{fc['max_price_change_pct']}%`\n\n"
        "🇹🇼 *台股*\n"
        f"  最低總分: `{ftw['min_total_score']}`  最低領先分: `{ftw['min_early_score']}`\n\n"
        "🇺🇸 *美股*\n"
        f"  最低總分: `{fus['min_score']}`  最低早分: `{fus['min_early']}`\n\n"
        "指令：`/set_score` `/set_early` `/set_max_change`（加密）\n"
        "`/set_tw_score` `/set_tw_early`（台股）\n"
        "`/set_us_score` `/set_us_early`（美股）\n"
        "`/reset` 重設所有為預設值"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_set_tw_score(update, ctx):
    try:
        v = float(ctx.args[0]); assert 20 <= v <= 95
    except:
        await update.message.reply_text("用法: `/set_tw_score 45`  (預設 45)", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    TW_USER_FILTERS.setdefault(uid, {})["min_total_score"] = v
    _db.save_user_filter(uid, "tw", TW_USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 台股最低總分 = {v}")

async def cmd_set_tw_early(update, ctx):
    try:
        v = float(ctx.args[0]); assert 0 <= v <= 80
    except:
        await update.message.reply_text("用法: `/set_tw_early 20`  (預設 20)", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    TW_USER_FILTERS.setdefault(uid, {})["min_early_score"] = v
    _db.save_user_filter(uid, "tw", TW_USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 台股最低領先分 = {v}")

async def cmd_set_us_score(update, ctx):
    try:
        v = float(ctx.args[0]); assert 20 <= v <= 95
    except:
        await update.message.reply_text("用法: `/set_us_score 45`  (預設 45)", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    US_USER_FILTERS.setdefault(uid, {})["min_score"] = v
    _db.save_user_filter(uid, "us", US_USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 美股最低總分 = {v}")

async def cmd_set_us_early(update, ctx):
    try:
        v = float(ctx.args[0]); assert 0 <= v <= 80
    except:
        await update.message.reply_text("用法: `/set_us_early 18`  (預設 18)", parse_mode=ParseMode.MARKDOWN); return
    uid = update.effective_user.id
    US_USER_FILTERS.setdefault(uid, {})["min_early"] = v
    _db.save_user_filter(uid, "us", US_USER_FILTERS[uid])
    await update.message.reply_text(f"✅ 美股最低早分 = {v}")

async def cmd_reset(update, ctx):
    uid = update.effective_user.id
    USER_FILTERS.pop(uid, None)
    TW_USER_FILTERS.pop(uid, None)
    US_USER_FILTERS.pop(uid, None)
    _db.delete_user_filter(uid, None)  # None = 清除該用戶所有市場
    await update.message.reply_text("✅ 三市場篩選已全部重設為預設值")

# ============================================================
# 訂閱
# ============================================================
# ── 訂閱管理面板 ──────────────────────────────────────────
def _sub_keyboard(cid: int, show_back: bool = False) -> InlineKeyboardMarkup:
    """產生訂閱面板 InlineKeyboard，已訂閱顯示 ✅"""
    def _btn(label, channel, subscribers):
        icon = "✅" if cid in subscribers else "☐"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"sub:toggle:{channel}")
    rows = [
        [
            _btn("加密預警 (30分)", "pre_pump", PRE_PUMP_SUBSCRIBERS),
            _btn("榜單推送 (1小時)", "general",  SUBSCRIBERS),
        ],
        [
            _btn("台股早盤 (08:50)", "tw", TW_SUBSCRIBERS),
            _btn("美股盤前 (21:00)", "us", US_SUBSCRIBERS),
        ],
        [InlineKeyboardButton("🔕 取消全部", callback_data="sub:unsub_all")],
    ]
    if show_back:
        rows.append([InlineKeyboardButton("◀️ 返回選單", callback_data="help:menu")])
    return InlineKeyboardMarkup(rows)

def _sub_keyboard_with_back(cid: int) -> InlineKeyboardMarkup:
    return _sub_keyboard(cid, show_back=True)

def _sub_text(cid: int) -> str:
    lines = ["*🔔 訂閱管理*", "點選按鈕切換訂閱狀態：", ""]
    items = [
        ("加密預警",  "每 30 分鐘推送預備暴漲/暴跌榜", cid in PRE_PUMP_SUBSCRIBERS),
        ("榜單推送",  "每小時推送加密 Top 5",          cid in SUBSCRIBERS),
        ("台股早盤",  "每日 08:50 推送台股開盤前預警",  cid in TW_SUBSCRIBERS),
        ("美股盤前",  "每日 21:00 推送美股盤前機會",    cid in US_SUBSCRIBERS),
    ]
    for name, desc, active in items:
        icon = "✅" if active else "☐"
        lines.append(f"{icon} *{name}* — {desc}")
    return "\n".join(lines)

async def cmd_sub(update, ctx):
    """顯示訂閱管理面板"""
    cid = update.effective_chat.id
    await update.message.reply_text(
        _sub_text(cid), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_sub_keyboard(cid)
    )

# 保留舊指令相容性
async def cmd_sub_pre(update, ctx):
    cid = update.effective_chat.id
    PRE_PUMP_SUBSCRIBERS.add(cid)
    _db.save_subscriber("pre_pump", cid)
    await update.message.reply_text("🔋 已訂閱！用 /sub 管理所有訂閱")

async def cmd_unsub_all(update, ctx):
    cid = update.effective_chat.id
    SUBSCRIBERS.discard(cid)
    PRE_PUMP_SUBSCRIBERS.discard(cid)
    if tv_handler:
        tv_handler.remove_subscriber(cid)
    TW_SUBSCRIBERS.discard(cid)
    US_SUBSCRIBERS.discard(cid)
    for ch in ("general", "pre_pump", "tw", "us"):
        _db.delete_subscriber(ch, cid)
    await update.message.reply_text("🔕 已取消全部訂閱")

async def cb_sub_toggle(update, ctx):
    """切換單項訂閱"""
    query = update.callback_query
    await query.answer()
    cid = query.message.chat_id   # 用 chat_id：群組訂閱送群組，私聊訂閱送私聊
    channel = query.data.split(":")[2]

    _map = {
        "pre_pump": PRE_PUMP_SUBSCRIBERS,
        "general":  SUBSCRIBERS,
        "tw":       TW_SUBSCRIBERS,
        "us":       US_SUBSCRIBERS,
    }
    subs = _map.get(channel)
    if subs is None:
        return
    if cid in subs:
        subs.discard(cid)
        _db.delete_subscriber(channel, cid)
    else:
        subs.add(cid)
        _db.save_subscriber(channel, cid)

    # 判斷是否從 help 進入（訊息有返回按鈕）
    msg = query.message
    has_back = any(
        btn.callback_data == "help:menu"
        for row in (msg.reply_markup.inline_keyboard if msg.reply_markup else [])
        for btn in row
    )
    await query.edit_message_text(
        _sub_text(cid), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_sub_keyboard(cid, show_back=has_back)
    )

async def cb_sub_unsub_all(update, ctx):
    """取消全部訂閱"""
    query = update.callback_query
    await query.answer("已取消全部訂閱")
    cid = query.message.chat_id   # 用 chat_id，與 cb_sub_toggle 一致
    for subs in (SUBSCRIBERS, PRE_PUMP_SUBSCRIBERS, TW_SUBSCRIBERS, US_SUBSCRIBERS):
        subs.discard(cid)
    for ch in ("general", "pre_pump", "tw", "us"):
        _db.delete_subscriber(ch, cid)
    await query.edit_message_text(
        _sub_text(cid), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_sub_keyboard(cid)
    )

# ============================================================
# 排程任務
# ============================================================
async def push_pre_warning(ctx):
    if not PRE_PUMP_SUBSCRIBERS:
        return
    log.info(f"推送預警給 {len(PRE_PUMP_SUBSCRIBERS)} 人")
    try:
        coins = await get_scan(force=True)
    except Exception as e:
        log.exception(e); return
    pre_pump_raw = find_pre_pump(coins)
    # 過濾：跌幅 < -8% 且早分 < 12 → 無動能支撐的下跌，不推
    # 新增：一致性門檻，至少 3/5 指標同向
    pre_pump = [
        c for c in pre_pump_raw
        if not (c.price_change_pct < -8 and c.score_early < 12)
        and passes_consistency_gate(c)
    ][:3]
    pre_dump = find_pre_dump(coins)[:3]
    if not pre_pump and not pre_dump:
        return
    parts = [f"🔋 *預警快訊*  _{datetime.now(TW_TZ):%H:%M}_\n"]
    btc_warn = _btc_trend_warning()
    if btc_warn:
        parts.append(btc_warn)
    if pre_pump:
        parts.append("*預備暴漲*")
        import aiohttp as _aio
        async with _aio.ClientSession() as _mtf_sess:
            for i, c in enumerate(pre_pump, 1):
                parts.append(fmt_card(c, i, show_triggers=True))
                mtf = await fetch_mtf_signal(c.base, session=_mtf_sess)
                if mtf:
                    parts.append(f"📊 多時框: {mtf}")
                parts.append("")
    if pre_dump:
        parts.append("*預備暴跌*")
        for i, c in enumerate(pre_dump, 1):
            parts.append(fmt_card(c, i, show_triggers=True))
            parts.append("")
    parts.append("💡 輸入 `/trade 幣種` 查看完整交易建議")
    text = "\n".join(parts)
    for cid in list(PRE_PUMP_SUBSCRIBERS):
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN,
                                       disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"推送失敗 {cid}: {e}")
            PRE_PUMP_SUBSCRIBERS.discard(cid)

async def push_general(ctx):
    if not SUBSCRIBERS: return
    try:
        coins = await get_scan(force=True)
    except: return
    text = fmt_list(coins[:5], "⏰ 每小時 Top 5", show_triggers=False)
    for cid in list(SUBSCRIBERS):
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN,
                                       disable_web_page_preview=True)
        except Exception as e:
            SUBSCRIBERS.discard(cid)

# ============================================================
# 背景預掃描（每 30 分鐘刷新快取，讓用戶發指令時秒回）
# ============================================================
# ── 市場時段感知掃描 ────────────────────────────────────
import time as _time
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

_CST = _tz(_td(hours=8))   # 台灣時區
_TW_LAST = 0.0
_US_LAST = 0.0
_TW_RUNNING = False
_US_RUNNING = False
_CRYPTO_RUNNING = False

def _tw_in_market() -> bool:
    """台股市場時段：週一~五 08:00~13:30 CST"""
    now = _dt.now(_CST)
    if now.weekday() >= 5:
        return False
    h = now.hour + now.minute / 60
    return 8.0 <= h < 13.5

def _us_in_market() -> bool:
    """美股市場時段：週一~五(台灣) 20:30~04:00 CST"""
    now = _dt.now(_CST)
    if now.weekday() >= 5:
        return False
    h = now.hour + now.minute / 60
    return h >= 20.5 or h < 4.0

async def background_tw_scan(ctx):
    """台股時段感知掃描：盤中每 10 分，盤外每 60 分"""
    global _TW_LAST, _TW_RUNNING
    interval = 600 if _tw_in_market() else 3600
    if _time.time() - _TW_LAST < interval:
        return
    if _TW_RUNNING:
        log.info("[背景] 台股掃描進行中，跳過本次")
        return
    _TW_RUNNING = True
    try:
        await get_tw_scan(force=True)
        _TW_LAST = _time.time()
        _tw_label = "盤中" if _tw_in_market() else "盤外"
        log.info(f"[背景] 台股快取已刷新（{_tw_label}）")
    except Exception as e:
        log.error(f"[背景] 台股掃描失敗: {e}")
    finally:
        _TW_RUNNING = False

async def background_us_scan(ctx):
    """美股時段感知掃描：盤中每 10 分，盤外每 60 分"""
    global _US_LAST, _US_RUNNING
    interval = 600 if _us_in_market() else 3600
    if _time.time() - _US_LAST < interval:
        return
    if _US_RUNNING:
        log.info("[背景] 美股掃描進行中，跳過本次")
        return
    _US_RUNNING = True
    try:
        stocks = await get_us_scan(force=True)
        _US_LAST = _time.time()
        _us_label = "盤中" if _us_in_market() else "盤外"
        log.info(f"[背景] 美股快取已刷新（{_us_label}）")
        tickers = [s.ticker for s in stocks if s.ticker]
        if tickers:
            refresh_inst_cache(tickers)
    except Exception as e:
        log.error(f"[背景] 美股掃描失敗: {e}")
    finally:
        _US_RUNNING = False

async def background_crypto_scan(ctx):
    """背景加密貨幣預掃描，每 3 分鐘刷新快取"""
    global _CRYPTO_RUNNING
    if _CRYPTO_RUNNING:
        log.info("[背景] 加密掃描進行中，跳過本次")
        return
    _CRYPTO_RUNNING = True
    try:
        await get_scan(force=True)
        log.info("[背景] 加密快取已刷新")
    except Exception as e:
        log.error(f"[背景] 加密掃描失敗: {e}")
    finally:
        _CRYPTO_RUNNING = False

# ============================================================
# Phase 3 — 4H 加密信號（Binance 免費 API）
# ============================================================
async def fetch_4h_signal(base: str) -> str:
    """
    從 Binance 抓 4H K 線，判斷 4H 方向與日線是否同向。
    回傳文字描述，供卡片追加顯示。
    """
    import aiohttp as _aio
    symbol = f"{base}USDT"
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit=20"
    try:
        async with _aio.ClientSession() as s:
            async with s.get(url, timeout=_aio.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return ""
                klines = await r.json()
        if len(klines) < 10:
            return ""
        closes = [float(k[4]) for k in klines]
        # EMA5 vs EMA10 on 4H
        def ema(data, n):
            k = 2 / (n + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e
        ema5  = ema(closes[-10:], 5)
        ema10 = ema(closes[-10:], 10)
        last  = closes[-1]
        prev  = closes[-2]
        pct4h = (last - prev) / prev * 100 if prev > 0 else 0
        if ema5 > ema10 and pct4h >= 0:
            return f"📶 4H 多頭排列 {pct4h:+.2f}%  ✅ 與日線同向"
        elif ema5 < ema10 and pct4h < 0:
            return f"📉 4H 空頭排列 {pct4h:+.2f}%  ⚠️ 趨勢一致偏空"
        else:
            return f"↔️ 4H 盤整 {pct4h:+.2f}%"
    except Exception:
        return ""


async def fetch_mtf_signal(base: str, session=None) -> str:
    """
    多時框方向信號：日線 + 4H + 1H
    使用 close vs MA20 判斷方向 (▲多 / ▼空)
    回傳格式如: 日線 ▲  4H ▲  1H ▼（等回調）
    session 可傳入共用，避免每次建立新連線
    """
    import aiohttp as _aio
    symbol = f"{base}USDT"
    fapi_base = "https://fapi.binance.com/fapi/v1/klines"
    timeout = _aio.ClientTimeout(total=10)

    def _ma20_dir(klines_raw):
        try:
            closes = [float(k[4]) for k in klines_raw]
            if len(closes) < 20:
                return None
            ma20 = sum(closes[-20:]) / 20
            return closes[-1] > ma20
        except Exception:
            return None

    async def _fetch_tf(s, interval):
        try:
            async with s.get(fapi_base,
                             params={"symbol": symbol, "interval": interval, "limit": 50},
                             timeout=timeout) as r:
                return await r.json() if r.status == 200 else []
        except Exception:
            return []

    try:
        own_session = session is None
        s = _aio.ClientSession() if own_session else session
        try:
            d_raw, h4_raw, h1_raw = await asyncio.gather(
                _fetch_tf(s, "1d"),
                _fetch_tf(s, "4h"),
                _fetch_tf(s, "1h"),
            )
        finally:
            if own_session:
                await s.close()

        d_bull  = _ma20_dir(d_raw)
        h4_bull = _ma20_dir(h4_raw)
        h1_bull = _ma20_dir(h1_raw)

        def arrow(b):
            return "?" if b is None else ("▲" if b else "▼")

        suffix = ""
        if h1_bull is False and (d_bull or h4_bull):
            suffix = "（等回調）"
        elif h1_bull and d_bull is False:
            suffix = "（逆勢反彈）"

        return f"日線 {arrow(d_bull)}  4H {arrow(h4_bull)}  1H {arrow(h1_bull)}{suffix}"
    except Exception:
        return ""


# ============================================================
# Phase 3 — 自選股指令
# ============================================================
async def cmd_watch(update, ctx):
    """/watch 2330 | /watch BTC | /watch AAPL — 加入自選股"""
    uid = update.effective_user.id
    if not ctx.args:
        wl = WATCHLISTS.get(uid, set())
        if not wl:
            await update.message.reply_text(
                "📋 *自選股清單*\n\n（空的）\n\n用 `/watch 2330` 加入台股\n"
                "用 `/watch BTC` 加入加密\n用 `/watch AAPL` 加入美股",
                parse_mode=ParseMode.MARKDOWN)
        else:
            lines = "\n".join(f"• `{s}`" for s in sorted(wl))
            await update.message.reply_text(
                f"📋 *你的自選股*\n\n{lines}\n\n"
                f"用 `/unwatch 代號` 移除",
                parse_mode=ParseMode.MARKDOWN)
        return
    sym = ctx.args[0].strip().upper().replace("USDT", "")
    # 若非台股，檢查是否有市場衝突
    if not re.match(r'^\d{4,6}[A-Z]?$', sym):
        market = await _resolve_market_conflict(update, sym, "watch")
        if market is None:
            return   # 已發送選擇鍵盤
    WATCHLISTS.setdefault(uid, set()).add(sym)
    _db.save_watch(uid, sym)
    await update.message.reply_text(
        f"✅ 已加入自選股：`{sym}`\n"
        f"背景每 30 分鐘會自動監控並在條件成立時通知你\n\n"
        f"用 `/alert` 設定警報條件",
        parse_mode=ParseMode.MARKDOWN)


async def cmd_unwatch(update, ctx):
    """/unwatch 2330 — 移除自選股"""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("用法：`/unwatch 2330`", parse_mode=ParseMode.MARKDOWN)
        return
    sym = ctx.args[0].strip().upper().replace("USDT", "")
    wl  = WATCHLISTS.get(uid, set())
    if sym in wl:
        wl.discard(sym)
        _db.delete_watch(uid, sym)
        await update.message.reply_text(f"🗑 已移除：`{sym}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ `{sym}` 不在你的自選股中", parse_mode=ParseMode.MARKDOWN)


async def cmd_mywatchlist(update, ctx):
    """/mywatchlist — 顯示自選股並附上即時狀態"""
    uid = update.effective_user.id
    wl  = WATCHLISTS.get(uid, set())
    if not wl:
        await update.message.reply_text(
            "📋 自選股清單是空的\n用 `/watch 2330` 加入", parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("🔍 掃描自選股中...")
    lines = [f"📋 *你的自選股即時狀態*\n"]
    tw_stocks  = await get_tw_scan()
    us_stocks  = await get_us_scan()
    coins      = await get_scan()

    for sym in sorted(wl):
        # 台股
        if re.match(r'^\d{4,6}[A-Z]?$', sym):
            m = next((s for s in tw_stocks if s.stock_id == sym), None)
            if not m:
                try:
                    m = await asyncio.wait_for(fetch_single_tw_metrics(sym), timeout=30)
                except Exception:
                    m = None
            if m:
                streak = int(m.foreign_streak) if m.foreign_streak else 0
                lines.append(
                    f"🇹🇼 *{sym} {m.name}*  {m.direction}\n"
                    f"  收盤 `{m.close:.2f}`  漲跌 `{m.change_pct:+.2f}%`\n"
                    f"  外資連續 `{streak:+d}天`  總分 `{m.total_score:.0f}`\n"
                )
            else:
                lines.append(f"🇹🇼 *{sym}*  _資料不足_\n")
            continue
        # 加密
        mc = next((c for c in coins if c.base == sym), None)
        if mc:
            lines.append(
                f"🪙 *{sym}/USDT*  {mc.direction}\n"
                f"  價格 `${mc.last_price:,.4f}`  24h `{mc.price_change_pct:+.2f}%`\n"
                f"  總分 `{mc.total_score:.0f}`  信心 `{mc.confidence:.0%}`\n"
            )
            continue
        # 美股
        mu = next((s for s in us_stocks if s.ticker == sym), None)
        if mu:
            lines.append(
                f"🇺🇸 *{sym}*  {mu.direction}\n"
                f"  收盤 `${mu.close:.2f}`  漲跌 `{mu.change_pct:+.2f}%`\n"
                f"  總分 `{mu.total_score:.0f}`  信心 `{mu.confidence:.0%}`\n"
            )
        else:
            lines.append(f"❓ *{sym}*  _找不到資料_\n")

    cond = WATCH_CONDITIONS.get(uid, {})
    cond_txt = (
        f"\n⚙️ *警報設定*\n"
        f"外資連買 ≥ `{cond.get('foreign_streak', 3)}天`\n"
        f"評分預警 ≥ `{cond.get('pre_warn_pct', 70)}%`\n"
        f"\n用 `/alert` 修改警報條件"
    )
    await update.message.reply_text(
        "\n".join(lines) + cond_txt,
        parse_mode=ParseMode.MARKDOWN)


async def cmd_alert(update, ctx):
    """/alert — 查看或設定警報條件
    用法：
      /alert               查看目前設定
      /alert foreign 3     外資連買 ≥ 3 天才推送
      /alert pre 75        評分達到 75% 時預警
    """
    uid  = update.effective_user.id
    cond = WATCH_CONDITIONS.setdefault(uid, {"foreign_streak": 3, "pre_warn_pct": 70})

    if not ctx.args:
        await update.message.reply_text(
            f"⚙️ *你的警報設定*\n\n"
            f"外資連買門檻：`{cond['foreign_streak']} 天`\n"
            f"評分預警門檻：`{cond['pre_warn_pct']}%`\n\n"
            f"修改範例：\n"
            f"`/alert foreign 5`  → 外資連買 ≥ 5 天\n"
            f"`/alert pre 80`     → 評分達 80% 才預警",
            parse_mode=ParseMode.MARKDOWN)
        return

    if len(ctx.args) < 2:
        await update.message.reply_text("用法：`/alert foreign 3` 或 `/alert pre 75`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    key, val_str = ctx.args[0].lower(), ctx.args[1]
    try:
        val = int(val_str)
    except ValueError:
        await update.message.reply_text("數值請輸入整數", parse_mode=ParseMode.MARKDOWN)
        return

    if key in ("foreign", "外資"):
        cond["foreign_streak"] = max(1, val)
        _db.save_alert_condition(uid, "foreign_streak", cond["foreign_streak"])
        await update.message.reply_text(
            f"✅ 外資連買門檻設為 `{cond['foreign_streak']} 天`",
            parse_mode=ParseMode.MARKDOWN)
    elif key in ("pre", "預警"):
        cond["pre_warn_pct"] = max(50, min(95, val))
        _db.save_alert_condition(uid, "pre_warn_pct", cond["pre_warn_pct"])
        await update.message.reply_text(
            f"✅ 評分預警門檻設為 `{cond['pre_warn_pct']}%`",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "未知設定\n`/alert foreign 3` 或 `/alert pre 75`",
            parse_mode=ParseMode.MARKDOWN)


# ============================================================
# Phase 3 — 背景自選股監控（每 30 分鐘）
# ============================================================
async def background_watchlist_monitor(ctx):
    """每 30 分鐘掃描所有用戶的自選股，觸發條件時推送通知"""
    _prune_sent_alerts()  # 清理過期警報，防記憶體洩漏
    if not WATCHLISTS:
        return

    # 取得各市場資料（共用快取，不重複抓）
    try:
        tw_stocks = await get_tw_scan()
        us_stocks = await get_us_scan()
        coins     = await get_scan()
    except Exception as e:
        log.error(f"[Watchlist] 掃描失敗: {e}")
        return

    # 定義「評分滿分」基準（台股/美股/加密各自不同）
    TW_MAX_SCORE = 100.0
    US_MAX_SCORE = 100.0
    CRYPTO_MAX   = 100.0

    for uid, wl in WATCHLISTS.items():
        cond = WATCH_CONDITIONS.get(uid, {"foreign_streak": 3, "pre_warn_pct": 70})
        streak_min  = cond.get("foreign_streak", 3)
        pre_warn    = cond.get("pre_warn_pct", 70) / 100

        alerts = []

        for sym in wl:
            # ── 台股 ──
            if re.match(r'^\d{4,6}[A-Z]?$', sym):
                m = next((s for s in tw_stocks if s.stock_id == sym), None)
                if not m:
                    continue
                streak = int(m.foreign_streak) if m.foreign_streak else 0
                score_pct = m.total_score / TW_MAX_SCORE

                # 外資連買警報
                key_streak = (uid, sym, "foreign_streak")
                if streak >= streak_min and key_streak not in _SENT_ALERTS:
                    _SENT_ALERTS[key_streak] = _time.time()
                    alerts.append(
                        f"🏦 *{sym} {m.name}* 外資連買 `{streak}天`\n"
                        f"   收盤 `{m.close:.2f}`  法人合計 `{m.institutional_net/1e8:+.2f}億`"
                    )
                elif streak < streak_min:
                    _SENT_ALERTS.pop(key_streak, None)

                # 預警：評分達到門檻但未達到「完全啟動」
                key_pre = (uid, sym, "pre_warn")
                if pre_warn <= score_pct < 0.90 and key_pre not in _SENT_ALERTS:
                    _SENT_ALERTS[key_pre] = _time.time()
                    alerts.append(
                        f"⚡ *{sym} {m.name}* 評分預警 `{m.total_score:.0f}分`（{score_pct:.0%}）\n"
                        f"   {m.direction}  漲跌 `{m.change_pct:+.2f}%`"
                    )
                elif score_pct < pre_warn:
                    _SENT_ALERTS.pop(key_pre, None)

            # ── 加密 ──
            elif mc := next((c for c in coins if c.base == sym), None):
                score_pct = mc.total_score / CRYPTO_MAX
                key_pre   = (uid, sym, "pre_warn")
                if pre_warn <= score_pct < 0.90 and key_pre not in _SENT_ALERTS:
                    _SENT_ALERTS[key_pre] = _time.time()
                    alerts.append(
                        f"⚡ *{sym}/USDT* 評分預警 `{mc.total_score:.0f}分`（{score_pct:.0%}）\n"
                        f"   {mc.direction}  價格 `${mc.last_price:,.4f}`"
                    )
                elif score_pct < pre_warn:
                    _SENT_ALERTS.pop(key_pre, None)

            # ── 美股 ──
            elif mu := next((s for s in us_stocks if s.ticker == sym), None):
                score_pct = mu.total_score / US_MAX_SCORE
                key_pre   = (uid, sym, "pre_warn")
                if pre_warn <= score_pct < 0.90 and key_pre not in _SENT_ALERTS:
                    _SENT_ALERTS[key_pre] = _time.time()
                    alerts.append(
                        f"⚡ *{sym}* 評分預警 `{mu.total_score:.0f}分`（{score_pct:.0%}）\n"
                        f"   {mu.direction}  收盤 `${mu.close:.2f}`"
                    )
                elif score_pct < pre_warn:
                    _SENT_ALERTS.pop(key_pre, None)

        if alerts:
            header = f"🔔 *自選股警報*（{datetime.now(TW_TZ).strftime('%m/%d %H:%M')} 台時）\n\n"
            msg    = header + "\n\n".join(alerts)
            msg   += "\n\n用 `/mywatchlist` 查看完整狀態"
            try:
                await ctx.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
                log.info(f"[Watchlist] 推送 {len(alerts)} 條警報給 user {uid}")
            except Exception as e:
                log.error(f"[Watchlist] 推送失敗 uid={uid}: {e}")


# ============================================================
# Phase 2 — 倉位追蹤
# ============================================================
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "8370118189"))  # 設在 Fly.io secrets

def _detect_market(symbol: str) -> str:
    """快速判斷市場類型"""
    if re.match(r'^\d{4,6}[A-Z]?$', symbol):
        return "tw"
    return "unknown"  # crypto / us 需要掃描確認

def _calc_tp_sl(entry: float, support: float, resistance: float,
                atr: float, market: str,
                direction: str = "long") -> tuple[float, float]:
    """
    根據支撐/阻力/ATR 計算 TP/SL，支援多空。
    回傳 (tp, sl)
    """
    if direction == "short":
        # 空單：TP 在下方，SL 在上方
        if market == "tw":
            tp = support     if support < entry else entry * 0.92
            sl = resistance  if resistance > entry else entry * 1.05
        elif market == "crypto":
            tp = entry - 2.5 * atr if atr > 0 else entry * 0.88
            sl = entry + 1.0 * atr if atr > 0 else entry * 1.05
        else:
            tp = entry - 2.0 * atr if atr > 0 else entry * 0.90
            sl = entry + 1.0 * atr if atr > 0 else entry * 1.05
    else:
        # 多單
        if market == "tw":
            tp = resistance if resistance > entry else entry * 1.08
            sl = support    if support   < entry else entry * 0.95
        elif market == "crypto":
            tp = entry + 2.5 * atr if atr > 0 else entry * 1.12
            sl = entry - 1.0 * atr if atr > 0 else entry * 0.95
        else:
            tp = entry + 2.0 * atr if atr > 0 else entry * 1.10
            sl = entry - 1.0 * atr if atr > 0 else entry * 0.95
    return round(tp, 4), round(sl, 4)

def _fmt_position_card(p: dict, current_price: float | None = None) -> str:
    entry     = p["entry_price"]
    tp        = p["tp_price"]
    sl        = p["sl_price"]
    direction = p.get("direction", "long")
    leverage  = p.get("leverage", 1)
    market_flag = {"tw": "🇹🇼", "us": "🇺🇸", "crypto": "🪙"}.get(p["market"], "📊")
    dir_str   = "📈 多" if direction == "long" else "📉 空"
    lev_str   = f" {leverage}x" if leverage > 1 else " 現貨"
    tp_pct    = (tp - entry) / entry * 100 * (1 if direction == "long" else -1)
    sl_pct    = (sl - entry) / entry * 100 * (1 if direction == "long" else -1)
    lines = [
        f"{market_flag} *{p['symbol']}*  {dir_str}{lev_str}  進場 `{entry:.4f}`",
        f"  TP `{tp:.4f}` ({tp_pct:+.1f}%)  SL `{sl:.4f}` ({sl_pct:+.1f}%)",
    ]
    if current_price:
        pnl = (current_price - entry) / entry * 100
        if direction == "short":
            pnl = -pnl
        pnl_lev = pnl * leverage
        lines.append(f"  現價 `{current_price:.4f}`  損益 `{pnl:+.2f}%`"
                     + (f"  (帶槓 `{pnl_lev:+.2f}%`)" if leverage > 1 else ""))
    return "\n".join(lines)


async def cmd_enter(update, ctx):
    """/enter BTC 81000 [l/s] [槓桿]
    範例：
      /enter BTC 81000        現貨多單
      /enter BTC 81000 l      做多
      /enter BTC 81000 l 10   做多 10 倍
      /enter BTC 81000 s      做空
      /enter BTC 81000 s 5    做空 5 倍
      /enter 2330 200         台股現貨多單
    """
    uid = update.effective_user.id
    if len((ctx.args or [])) < 2:
        await update.message.reply_text(
            "📥 *進場記錄*\n\n"
            "`/enter BTC 81000`      現貨多單\n"
            "`/enter BTC 81000 l 10` 做多 10 倍槓桿\n"
            "`/enter BTC 81000 s 5`  做空 5 倍槓桿\n"
            "`/enter 2330 200`       台股現貨",
            parse_mode=ParseMode.MARKDOWN)
        return

    raw_sym = ctx.args[0].strip().upper().replace("USDT", "")
    try:
        entry_price = float(ctx.args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ 進場價格格式錯誤", parse_mode=ParseMode.MARKDOWN)
        return

    # 解析方向和槓桿
    direction = "long"
    leverage  = 1
    if len(ctx.args) >= 3:
        d = ctx.args[2].lower()
        if d in ("s", "short"):
            direction = "short"
        elif d in ("l", "long"):
            direction = "long"
    if len(ctx.args) >= 4:
        try:
            leverage = max(1, int(ctx.args[3]))
        except ValueError:
            pass

    # 台股/美股強制多單
    if _detect_market(raw_sym) in ("tw",) or (
            not re.match(r'^\d{4,6}[A-Z]?$', raw_sym) and direction == "short"):
        if _detect_market(raw_sym) == "tw":
            direction = "long"
            leverage  = 1

    dir_label = "📈 做多" if direction == "long" else "📉 做空"
    lev_label = f" {leverage}x 槓桿" if leverage > 1 else " 現貨"
    await update.message.reply_text(
        f"🔍 分析 {raw_sym}  {dir_label}{lev_label}，計算建議 TP/SL...")

    # 判斷市場並取得指標
    support = resistance = atr = 0.0
    market  = _detect_market(raw_sym)

    if market == "tw":
        stocks = await get_tw_scan()
        m = next((s for s in stocks if s.stock_id == raw_sym), None)
        if not m:
            try:
                m = await asyncio.wait_for(fetch_single_tw_metrics(raw_sym), timeout=40)
            except Exception:
                m = None
        if m:
            support    = getattr(m, "support",    entry_price * 0.95)
            resistance = getattr(m, "resistance", entry_price * 1.08)
            atr        = getattr(m, "atr",        entry_price * 0.03)
    else:
        coins = await get_scan()
        mc    = next((c for c in coins if c.base == raw_sym), None)
        if mc:
            market     = "crypto"
            support    = getattr(mc, "nearest_support",    entry_price * 0.95) or entry_price * 0.95
            resistance = getattr(mc, "nearest_resistance", entry_price * 1.12) or entry_price * 1.12
            atr        = getattr(mc, "atr",                entry_price * 0.04) or entry_price * 0.04
        else:
            us_stocks = await get_us_scan()
            mu = next((s for s in us_stocks if s.ticker == raw_sym), None)
            if mu:
                market     = "us"
                support    = getattr(mu, "support",    entry_price * 0.95)
                resistance = getattr(mu, "resistance", entry_price * 1.10)
                atr        = getattr(mu, "atr",        entry_price * 0.03)
            else:
                await update.message.reply_text(
                    f"❌ 找不到 `{raw_sym}` 的資料", parse_mode=ParseMode.MARKDOWN)
                return

    tp, sl = _calc_tp_sl(entry_price, support, resistance, atr, market, direction)
    tp_pct = abs(tp - entry_price) / entry_price * 100
    sl_pct = abs(sl - entry_price) / entry_price * 100
    rr     = tp_pct / sl_pct if sl_pct > 0 else 0

    market_flag = {"tw": "🇹🇼", "us": "🇺🇸", "crypto": "🪙"}.get(market, "📊")
    tp_sign = "+" if direction == "long" else "-"
    sl_sign = "-" if direction == "long" else "+"
    lev_note = f"\n槓桿：`{leverage}x`  實際損益 ×{leverage}" if leverage > 1 else ""
    msg = (
        f"{market_flag} *{raw_sym}*  {dir_label}{lev_label}\n\n"
        f"進場：`{entry_price:.4f}`\n"
        f"建議 TP：`{tp:.4f}` ({tp_sign}{tp_pct:.1f}%)\n"
        f"建議 SL：`{sl:.4f}` ({sl_sign}{sl_pct:.1f}%)\n"
        f"風報比：`{rr:.1f}:1`{lev_note}\n\n"
        f"確認執行嗎？"
    )
    # callback_data: enter:ok:SYM:MKT:ENTRY:TP:SL:DIR:LEV
    cb_ok     = f"enter:ok:{raw_sym}:{market}:{entry_price}:{tp}:{sl}:{direction}:{leverage}"
    cb_custom = f"enter:custom:{raw_sym}:{market}:{entry_price}:{direction}:{leverage}"
    atr_str   = f"{atr:.6g}"
    cb_atr    = f"enter:atr_menu:{raw_sym}:{market}:{entry_price}:{atr_str}:{direction}:{leverage}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 依建議執行",  callback_data=cb_ok),
            InlineKeyboardButton("❌ 取消",        callback_data="enter:cancel"),
        ],
        [
            InlineKeyboardButton("✏️ 自訂 TP/SL", callback_data=cb_custom),
            InlineKeyboardButton("📐 ATR 倍數",   callback_data=cb_atr),
        ],
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def cb_enter(update, ctx):
    """處理 /enter 的 InlineKeyboard 回調"""
    q    = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data.split(":")

    if data[1] == "cancel":
        await q.edit_message_text("❌ 已取消")
        return

    if data[1] == "custom":
        # enter:custom:SYM:MKT:ENTRY:DIR:LEV
        _, _, symbol, market, entry_s, direction, lev_s = data
        _PENDING_CUSTOM[uid] = {
            "symbol": symbol, "market": market,
            "entry": float(entry_s), "direction": direction, "leverage": int(lev_s),
        }
        await q.edit_message_text(
            f"✏️ *自訂 TP/SL*\n\n"
            f"請輸入：\n`/tpsl {symbol} <止盈價> <止損價>`\n\n"
            f"例：`/tpsl {symbol} 85000 79000`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data[1] == "atr_menu":
        # enter:atr_menu:SYM:MKT:ENTRY:ATR:DIR:LEV
        _, _, symbol, market, entry_s, atr_s, direction, lev_s = data
        entry = float(entry_s)
        atr   = float(atr_s)
        base  = f"{symbol}:{market}:{entry_s}:{atr_s}:{direction}:{lev_s}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("×1",   callback_data=f"atr:{base}:1.0"),
            InlineKeyboardButton("×1.5", callback_data=f"atr:{base}:1.5"),
            InlineKeyboardButton("×2",   callback_data=f"atr:{base}:2.0"),
            InlineKeyboardButton("×3",   callback_data=f"atr:{base}:3.0"),
        ]])
        sl1 = entry - atr if direction == "long" else entry + atr
        tp1 = entry + 2*atr if direction == "long" else entry - 2*atr
        await q.edit_message_text(
            f"📐 *ATR 倍數止損*\n\n"
            f"ATR = `{atr:.4g}`\n"
            f"SL = 進場 ± N×ATR\n"
            f"TP = 進場 ± 2N×ATR  (1:2 風報比)\n\n"
            f"×1 範例：TP `{tp1:.4f}`  SL `{sl1:.4f}`\n\n"
            f"選擇 N 倍數：",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        return

    # enter:ok:SYM:MKT:ENTRY:TP:SL:DIR:LEV
    _, _, symbol, market, entry_s, tp_s, sl_s, direction, lev_s = data
    entry    = float(entry_s)
    tp       = float(tp_s)
    sl       = float(sl_s)
    leverage = int(lev_s)

    pos_id = _db.open_position(uid, symbol, market, entry, tp, sl, direction, leverage)
    market_flag = {"tw": "🇹🇼", "us": "🇺🇸", "crypto": "🪙"}.get(market, "📊")
    dir_label   = "📈 做多" if direction == "long" else "📉 做空"
    lev_label   = f" {leverage}x" if leverage > 1 else " 現貨"
    await q.edit_message_text(
        f"✅ 倉位已開啟 #{pos_id}\n\n"
        f"{market_flag} *{symbol}*  {dir_label}{lev_label}\n"
        f"進場：`{entry:.4f}`\n"
        f"TP：`{tp:.4f}`  SL：`{sl:.4f}`\n\n"
        f"背景每 30 分鐘自動追蹤，觸及 TP/SL 時通知你\n"
        f"用 `/close {symbol}` 手動平倉",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_atr_select(update, ctx):
    """處理 ATR 倍數選擇：atr:SYM:MKT:ENTRY:ATR:DIR:LEV:MULT"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    _, symbol, market, entry_s, atr_s, direction, lev_s, mult_s = parts
    entry    = float(entry_s)
    atr      = float(atr_s)
    leverage = int(lev_s)
    mult     = float(mult_s)

    if direction == "long":
        sl = round(entry - mult * atr, 4)
        tp = round(entry + 2 * mult * atr, 4)
    else:
        sl = round(entry + mult * atr, 4)
        tp = round(entry - 2 * mult * atr, 4)

    tp_pct  = abs(tp - entry) / entry * 100
    sl_pct  = abs(sl - entry) / entry * 100
    rr      = tp_pct / sl_pct if sl_pct > 0 else 0
    tp_sign = "+" if direction == "long" else "-"
    sl_sign = "-" if direction == "long" else "+"
    lev_note = f"\n槓桿：`{leverage}x`" if leverage > 1 else ""

    cb_ok = f"enter:ok:{symbol}:{market}:{entry_s}:{tp}:{sl}:{direction}:{lev_s}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 確認開倉", callback_data=cb_ok),
        InlineKeyboardButton("❌ 取消",     callback_data="enter:cancel"),
    ]])
    market_flag = {"tw": "🇹🇼", "us": "🇺🇸", "crypto": "🪙"}.get(market, "📊")
    dir_label   = "📈 多" if direction == "long" else "📉 空"
    await q.edit_message_text(
        f"{market_flag} *{symbol}*  {dir_label}  ATR×{mult_s}\n\n"
        f"進場：`{entry:.4f}`\n"
        f"TP：`{tp:.4f}` ({tp_sign}{tp_pct:.1f}%)  [2N×ATR]\n"
        f"SL：`{sl:.4f}` ({sl_sign}{sl_pct:.1f}%)  [{mult_s}×ATR]\n"
        f"風報比：`{rr:.1f}:1`{lev_note}\n\n"
        f"確認開倉？",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cmd_tpsl(update, ctx):
    """/tpsl BTC 85000 79000 — 自訂止盈止損"""
    uid = update.effective_user.id
    if len(ctx.args or []) < 3:
        await update.message.reply_text(
            "用法：`/tpsl BTC 85000 79000`\n"
            "先用 `/enter BTC 進場價` 並點「✏️ 自訂 TP/SL」再輸入此指令",
            parse_mode=ParseMode.MARKDOWN)
        return

    symbol = ctx.args[0].upper().replace("USDT", "")
    try:
        tp_input = float(ctx.args[1].replace(",", ""))
        sl_input = float(ctx.args[2].replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ 價格格式錯誤")
        return

    # 攔截手動止損輸入
    if uid in _PENDING_SL_INPUT:
        pid = _PENDING_SL_INPUT.pop(uid)
        try:
            new_sl = float(update.effective_message.text.strip())
            with _db._conn() as c:
                c.execute("UPDATE positions SET sl_price=? WHERE id=?", (new_sl, pid))
            PENDING_ALERTS.pop(pid, None)
            info = PENDING_ALERTS.get(pid, {})
            sym  = info.get("sym", "倉位")
            await update.effective_message.reply_text(
                f"✅ 止損已更新為 `{new_sl}`", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.effective_message.reply_text("⚠️ 請輸入有效數字")
            _PENDING_SL_INPUT[uid] = pid  # 放回等待
        return

    pending = _PENDING_CUSTOM.pop(uid, None)
    if not pending or pending["symbol"] != symbol:
        await update.message.reply_text(
            f"❌ 找不到 `{symbol}` 的待確認進場\n"
            f"請先用 `/enter {symbol} <進場價>` 並點選「✏️ 自訂 TP/SL」",
            parse_mode=ParseMode.MARKDOWN)
        return

    entry     = pending["entry"]
    market    = pending["market"]
    direction = pending["direction"]
    leverage  = pending["leverage"]

    tp_pct  = abs(tp_input - entry) / entry * 100
    sl_pct  = abs(sl_input - entry) / entry * 100
    rr      = tp_pct / sl_pct if sl_pct > 0 else 0
    tp_sign = "+" if direction == "long" else "-"
    sl_sign = "-" if direction == "long" else "+"

    pos_id      = _db.open_position(uid, symbol, market, entry, tp_input, sl_input, direction, leverage)
    market_flag = {"tw": "🇹🇼", "us": "🇺🇸", "crypto": "🪙"}.get(market, "📊")
    dir_label   = "📈 做多" if direction == "long" else "📉 做空"
    lev_label   = f" {leverage}x" if leverage > 1 else " 現貨"
    await update.message.reply_text(
        f"✅ 倉位已開啟 #{pos_id}\n\n"
        f"{market_flag} *{symbol}*  {dir_label}{lev_label}\n"
        f"進場：`{entry:.4f}`\n"
        f"TP：`{tp_input:.4f}` ({tp_sign}{tp_pct:.1f}%)\n"
        f"SL：`{sl_input:.4f}` ({sl_sign}{sl_pct:.1f}%)\n"
        f"風報比：`{rr:.1f}:1`\n\n"
        f"背景每 30 分鐘自動追蹤，觸及 TP/SL 時通知你\n"
        f"用 `/close {symbol}` 手動平倉",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_close(bot, uid: int, pos: dict, close_price: float):
    """實際執行平倉並回傳格式化訊息"""
    symbol    = pos["symbol"]
    direction = pos.get("direction", "long")
    leverage  = pos.get("leverage", 1)
    _db.close_position(pos["id"], close_price, pos["entry_price"], direction)
    if direction == "short":
        pnl = (pos["entry_price"] - close_price) / pos["entry_price"] * 100
    else:
        pnl = (close_price - pos["entry_price"]) / pos["entry_price"] * 100
    pnl_lev  = pnl * leverage
    emoji    = "🟢" if pnl >= 0 else "🔴"
    dir_str  = "📈 多" if direction == "long" else "📉 空"
    lev_str  = f" {leverage}x" if leverage > 1 else " 現貨"
    lev_note = f"\n帶槓損益：`{pnl_lev:+.2f}%`" if leverage > 1 else ""
    return (
        f"{emoji} *{symbol}* 平倉完成  {dir_str}{lev_str}\n\n"
        f"進場：`{pos['entry_price']:.4f}`\n"
        f"平倉：`{close_price:.4f}`\n"
        f"損益：`{pnl:+.2f}%`{lev_note}"
    )


async def _get_current_price(symbol: str, market: str) -> float | None:
    """從快取取得現價"""
    if market == "crypto":
        coins = await get_scan()
        mc = next((c for c in coins if c.base == symbol), None)
        return mc.last_price if mc else None
    elif market == "tw":
        stocks = await get_tw_scan()
        m = next((s for s in stocks if s.stock_id == symbol), None)
        return m.close if m else None
    elif market == "us":
        us = await get_us_scan()
        mu = next((s for s in us if s.ticker == symbol), None)
        return mu.close if mu else None
    return None


async def cmd_close(update, ctx):
    """/close BTC [平倉價格（可選）]"""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("用法：`/close BTC` 或 `/close BTC 82000`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    symbol = ctx.args[0].strip().upper().replace("USDT", "")

    # 解析平倉價格（可選）
    close_price_input: float | None = None
    if len(ctx.args) >= 2:
        try:
            close_price_input = float(ctx.args[1].replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ 平倉價格格式錯誤")
            return

    positions = _db.get_positions_by_symbol(uid, symbol)
    if not positions:
        await update.message.reply_text(f"❌ 找不到 `{symbol}` 的開倉紀錄",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    # 若只有一筆，直接平倉
    if len(positions) == 1:
        pos = positions[0]
        close_price = close_price_input or await _get_current_price(symbol, pos["market"]) or pos["entry_price"]
        msg = await _do_close(ctx.bot, uid, pos, close_price)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # 多筆同標的 → 顯示按鈕讓用戶選擇
    price_arg = str(close_price_input) if close_price_input else "auto"
    buttons = []
    for pos in positions:
        dir_str = "多" if pos.get("direction", "long") == "long" else "空"
        lev     = pos.get("leverage", 1)
        lev_str = f"{lev}x" if lev > 1 else "現貨"
        label   = f"{'📈' if dir_str=='多' else '📉'} {dir_str} {lev_str} | 進場 {pos['entry_price']:.4f}"
        buttons.append([InlineKeyboardButton(label,
                         callback_data=f"close_pick:{pos['id']}:{price_arg}")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="close_pick:cancel:0")])
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        f"*{symbol}* 有 {len(positions)} 筆開倉，選擇要平的那筆：",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cb_close_pick(update, ctx):
    """處理多倉選擇平倉的回調：close_pick:POS_ID:PRICE_OR_auto"""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "cancel":
        await q.edit_message_text("❌ 已取消")
        return

    pos_id     = int(parts[1])
    price_arg  = parts[2]
    uid        = q.from_user.id

    # 取得該倉位
    all_pos = _db.get_open_positions(uid)
    pos     = next((p for p in all_pos if p["id"] == pos_id), None)
    if not pos:
        await q.edit_message_text("❌ 找不到此倉位（可能已平倉）")
        return

    if price_arg == "auto":
        close_price = await _get_current_price(pos["symbol"], pos["market"]) or pos["entry_price"]
    else:
        close_price = float(price_arg)

    msg = await _do_close(ctx.bot, uid, pos, close_price)
    await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_positions(update, ctx):
    """/positions — 查看我的開倉 & 近期平倉"""
    uid   = update.effective_user.id
    # admin 可看全部
    if uid == ADMIN_ID and ctx.args and ctx.args[0] == "all":
        positions = _db.get_open_positions(user_id=None)
        header    = "👑 *所有用戶開倉*\n\n"
    else:
        positions = _db.get_open_positions(uid)
        header    = "📊 *我的開倉*\n\n"

    if not positions:
        closed = _db.get_closed_positions(uid, limit=5)
        if not closed:
            await update.message.reply_text(
                "📭 目前沒有開倉紀錄\n用 `/enter BTC 81000` 開始追蹤",
                parse_mode=ParseMode.MARKDOWN)
        else:
            lines = ["📊 *最近平倉紀錄*\n"]
            for p in closed:
                emoji = "🟢" if (p["pnl_pct"] or 0) >= 0 else "🔴"
                lines.append(f"{emoji} *{p['symbol']}*  `{p['pnl_pct']:+.2f}%`  {p['close_time'][:10]}")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # 取各市場快取
    coins     = await get_scan()
    tw_stocks = await get_tw_scan()
    us_stocks = await get_us_scan()

    lines = [header]
    for p in positions:
        sym = p["symbol"]
        cur = None
        if p["market"] == "crypto":
            mc  = next((c for c in coins if c.base == sym), None)
            cur = mc.last_price if mc else None
        elif p["market"] == "tw":
            m   = next((s for s in tw_stocks if s.stock_id == sym), None)
            cur = m.close if m else None
        elif p["market"] == "us":
            mu  = next((s for s in us_stocks if s.ticker == sym), None)
            cur = mu.close if mu else None
        lines.append(_fmt_position_card(p, cur))
        lines.append("")

    lines.append("用 `/close 代號` 平倉")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _check_reversal_signal(sym: str, market: str, direction: str,
                            coins, tw_stocks, us_stocks) -> tuple[bool, str]:
    """檢查是否有反轉訊號，回傳 (has_reversal, reason)"""
    if market == "crypto":
        mc = next((c for c in coins if c.base == sym), None)
        if not mc:
            return False, ""
        rsi = getattr(mc, "rsi", 50) or 50
        if direction == "long":
            if rsi > 75:
                return True, f"RSI 過熱 {rsi:.0f}"
            if mc.total_score < 35:
                return True, f"動能轉弱（總分 {mc.total_score:.0f}）"
        else:
            if rsi < 25:
                return True, f"RSI 超賣 {rsi:.0f}"
            if mc.total_score < 35:
                return True, f"動能轉弱（總分 {mc.total_score:.0f}）"
    elif market == "tw":
        m = next((s for s in tw_stocks if s.stock_id == sym), None)
        if not m:
            return False, ""
        rsi = getattr(m, "rsi", 50) or 50
        if direction == "long":
            if rsi > 75:
                return True, f"RSI 過熱 {rsi:.0f}"
            if getattr(m, "foreign_net", 0) < 0:
                return True, "外資轉賣"
        else:
            if rsi < 25:
                return True, f"RSI 超賣 {rsi:.0f}"
    elif market == "us":
        mu = next((s for s in us_stocks if s.ticker == sym), None)
        if not mu:
            return False, ""
        if direction == "long":
            if mu.rsi > 75:
                return True, f"RSI 過熱 {mu.rsi:.0f}"
            if mu.change_pct < -2:
                return True, f"今日下跌 {mu.change_pct:.1f}%"
        else:
            if mu.rsi < 25:
                return True, f"RSI 超賣 {mu.rsi:.0f}"
            if mu.change_pct > 2:
                return True, f"今日上漲 {mu.change_pct:.1f}%"
    return False, ""


# ── 背景倉位監控（每 30 分鐘）──
async def background_position_monitor(ctx):
    """檢查所有開倉是否觸及 TP/SL，並更新 Trailing Stop / Trailing TP"""
    positions = _db.get_open_positions()
    if not positions:
        return

    coins     = await get_scan()
    tw_stocks = await get_tw_scan()
    us_stocks = await get_us_scan()

    for p in positions:
        sym       = p["symbol"]
        entry     = p["entry_price"]
        tp        = p["tp_price"]
        sl        = p["sl_price"]
        high      = p["highest_price"] or entry
        uid       = p["user_id"]
        direction = p.get("direction", "long")

        # 取得現價
        cur = None
        if p["market"] == "crypto":
            mc  = next((c for c in coins if c.base == sym), None)
            cur = mc.last_price if mc else None
        elif p["market"] == "tw":
            m   = next((s for s in tw_stocks if s.stock_id == sym), None)
            cur = m.close if m else None
        elif p["market"] == "us":
            mu  = next((s for s in us_stocks if s.ticker == sym), None)
            cur = mu.close if mu else None

        if cur is None:
            continue

        # ── Trailing Stop（多單：現價創新高 SL 上移；空單：現價創新低 SL 下移）
        if direction == "long" and cur > high:
            _db.update_highest(p["id"], cur)
            trail_sl = entry + (cur - entry) * 0.5
            if trail_sl > sl:
                with _db._conn() as c:
                    c.execute("UPDATE positions SET sl_price=? WHERE id=?", (trail_sl, p["id"]))
                sl = trail_sl
        elif direction == "short" and cur < high:
            _db.update_highest(p["id"], cur)
            trail_sl = entry - (entry - cur) * 0.5
            if trail_sl < sl:
                with _db._conn() as c:
                    c.execute("UPDATE positions SET sl_price=? WHERE id=?", (trail_sl, p["id"]))
                sl = trail_sl

        # ── 方向感知的 TP/SL 觸發判斷
        tp_hit = (cur >= tp) if direction == "long" else (cur <= tp)
        sl_hit = (cur <= sl) if direction == "long" else (cur >= sl)

        if tp_hit:
            has_reversal, reason = _check_reversal_signal(
                sym, p["market"], direction, coins, tw_stocks, us_stocks)

            if has_reversal:
                # 有反轉訊號 → 詢問是否平倉
                pnl     = (cur - entry) / entry * 100 * (1 if direction == "long" else -1)
                pnl_lev = pnl * p.get("leverage", 1)
                lev_note = f"\n帶槓損益：`{pnl_lev:+.2f}%`" if p.get("leverage", 1) > 1 else ""
                pid = p["id"]
                if pid not in PENDING_ALERTS:
                    try:
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ 是，關閉倉位", callback_data=f"alert_close:{pid}"),
                            InlineKeyboardButton("📈 否，繼續持有", callback_data=f"alert_hold:{pid}"),
                        ]])
                        msg = await ctx.bot.send_message(uid,
                            f"🎯 *{sym}* 觸及止盈！\n\n"
                            f"TP `{tp:.4f}` 已達成\n現價 `{cur:.4f}`\n"
                            f"損益 `{pnl:+.2f}%` 🟢{lev_note}\n\n"
                            f"⚠️ *偵測到反轉訊號*：{reason}\n"
                            f"是否關閉倉位？",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                        PENDING_ALERTS[pid] = {
                            "uid": uid, "sym": sym, "trigger": "TP",
                            "cur": cur, "pnl": pnl, "entry": entry,
                            "direction": direction, "sl": sl, "tp": tp,
                            "leverage": p.get("leverage", 1), "market": p["market"],
                            "alert_count": 1, "last_alert_time": _time.time(),
                            "msg_id": msg.message_id,
                        }
                    except Exception:
                        pass
            else:
                # 無反轉訊號 → 追蹤止盈：自動上調 TP/SL
                atr_est = abs(tp - entry) * 0.4
                if p["market"] == "crypto":
                    mc = next((c for c in coins if c.base == sym), None)
                    if mc:
                        atr_est = getattr(mc, "atr", atr_est) or atr_est
                elif p["market"] == "us":
                    mu = next((s for s in us_stocks if s.ticker == sym), None)
                    if mu:
                        atr_est = getattr(mu, "atr", atr_est) or atr_est

                if direction == "long":
                    new_tp = round(cur + atr_est, 4)
                    new_sl = round(cur - atr_est * 0.5, 4)
                else:
                    new_tp = round(cur - atr_est, 4)
                    new_sl = round(cur + atr_est * 0.5, 4)

                with _db._conn() as c:
                    c.execute("UPDATE positions SET tp_price=?, sl_price=? WHERE id=?",
                              (new_tp, new_sl, p["id"]))

                pnl = (cur - entry) / entry * 100 * (1 if direction == "long" else -1)
                try:
                    await ctx.bot.send_message(uid,
                        f"📈 *{sym}* 觸及止盈！追蹤中\n\n"
                        f"現價 `{cur:.4f}`  浮盈 `{pnl:+.2f}%` 🟢\n"
                        f"無明顯反轉訊號，TP/SL 已自動上調：\n"
                        f"新 TP：`{new_tp:.4f}`\n"
                        f"新 SL（保底）：`{new_sl:.4f}`\n\n"
                        f"用 `/close {sym}` 手動鎖定獲利",
                        parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass

        elif sl_hit:
            pnl     = (cur - entry) / entry * 100 * (1 if direction == "long" else -1)
            pnl_lev = pnl * p.get("leverage", 1)
            lev_note = f"\n帶槓損益：`{pnl_lev:+.2f}%`" if p.get("leverage", 1) > 1 else ""
            pid = p["id"]
            if pid not in PENDING_ALERTS:
                try:
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ 是，關閉倉位", callback_data=f"alert_close:{pid}"),
                        InlineKeyboardButton("🔧 否，調整止損", callback_data=f"alert_adjust:{pid}"),
                    ]])
                    msg = await ctx.bot.send_message(uid,
                        f"🚨 *{sym}* 觸及止損！\n\n"
                        f"SL `{sl:.4f}` 已觸及\n現價 `{cur:.4f}`\n"
                        f"損益 `{pnl:+.2f}%` 🔴{lev_note}\n\n"
                        f"是否關閉倉位？",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                    PENDING_ALERTS[pid] = {
                        "uid": uid, "sym": sym, "trigger": "SL",
                        "cur": cur, "pnl": pnl, "entry": entry,
                        "direction": direction, "sl": sl, "tp": tp,
                        "leverage": p.get("leverage", 1), "market": p["market"],
                        "alert_count": 1, "last_alert_time": _time.time(),
                        "msg_id": msg.message_id,
                    }
                except Exception:
                    pass


# ============================================================
# 市場衝突處理（symbol 同時在加密＆美股）
# ============================================================
async def _resolve_market_conflict(update, symbol: str, action: str) -> str | None:
    """
    若 symbol 同時存在於加密和美股，發送 InlineKeyboard 請用戶選擇。
    action: 'stock' | 'watch' | 'enter'
    回傳 None 表示已發送選擇鍵盤（等待回調）。
    若無衝突，回傳判斷好的 market ('crypto' | 'us')。
    """
    coins     = await get_scan()
    us_stocks = await get_us_scan()
    in_crypto = any(c.base == symbol for c in coins)
    in_us     = any(s.ticker == symbol for s in us_stocks)

    if in_crypto and in_us:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🪙 加密貨幣", callback_data=f"mktsel:{action}:{symbol}:crypto"),
            InlineKeyboardButton("🇺🇸 美股",   callback_data=f"mktsel:{action}:{symbol}:us"),
        ]])
        await update.message.reply_text(
            f"⚠️ `{symbol}` 同時存在於加密貨幣和美股\n請選擇你要查詢的市場：",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        return None
    elif in_crypto:
        return "crypto"
    elif in_us:
        return "us"
    else:
        return "unknown"


async def cb_market_select(update, ctx):
    """處理市場選擇的回調：mktsel:ACTION:SYMBOL:MARKET"""
    q    = update.callback_query
    await q.answer()
    _, action, symbol, market = q.data.split(":")
    await q.edit_message_text(f"✅ 已選擇 {'加密貨幣' if market=='crypto' else '美股'} {symbol}")

    # 模擬執行對應指令
    class _FakeCtx:
        args = [symbol]
        bot  = q.get_bot()

    fake_update = update
    if action == "stock":
        # 直接發送對應卡片
        if market == "crypto":
            coins = await get_scan()
            mc    = next((c for c in coins if c.base == symbol), None)
            if mc:
                msg = fmt_card(mc, show_triggers=True)
                sig = await fetch_4h_signal(symbol)
                if sig:
                    msg += f"\n├ {sig}"
                msg += f"\n\n💡 `/trade {symbol}` 查看完整交易建議"
                await q.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           disable_web_page_preview=True)
        else:
            us = await get_us_scan()
            mu = next((s for s in us if s.ticker == symbol), None)
            if mu:
                msg = fmt_us_card(mu, show_triggers=True)
                msg += f"\n\n💡 `/us_trade {symbol}` 查看完整交易建議"
                await q.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           disable_web_page_preview=True)
    elif action == "watch":
        uid = q.from_user.id
        WATCHLISTS.setdefault(uid, set()).add(symbol)
        _db.save_watch(uid, symbol)
        await q.message.reply_text(
            f"✅ 已加入自選股：`{symbol}`（{'加密' if market=='crypto' else '美股'}）",
            parse_mode=ParseMode.MARKDOWN)


# ============================================================
# 通用個股查詢 /stock
# ============================================================
async def cmd_stock(update, ctx):
    """/stock 2330 | /stock BTC | /stock AAPL — 通用個股查詢"""
    if not ctx.args:
        await update.message.reply_text(
            "📊 *通用個股查詢*\n\n"
            "`/stock 2330` — 台股\n"
            "`/stock BTC`  — 加密貨幣\n"
            "`/stock AAPL` — 美股",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    raw    = ctx.args[0].strip()
    target = raw.upper().replace("USDT", "")

    # ── 判斷市場：純數字或數字+字母(ETF) → 台股
    if re.match(r'^\d{4,6}[A-Z]?$', target):
        await update.message.reply_text(f"🇹🇼 查詢台股 {target}…（首次約需 1-2 分鐘）")
        try:
            stocks = await asyncio.wait_for(get_tw_scan(), timeout=120)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "⏱ 台股資料抓取逾時，伺服器背景掃描中\n請 1-2 分鐘後再試",
                parse_mode=ParseMode.MARKDOWN)
            return
        except Exception as e:
            log.error(f"[/stock] TW scan error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 台股掃描失敗: {e}")
            return
        m = next((s for s in stocks if s.stock_id == target), None)
        if not m:
            # 掃描清單未包含此股，直接查詢
            await update.message.reply_text(f"🔍 {target} 不在掃描清單，改為直接查詢...")
            try:
                m = await asyncio.wait_for(fetch_single_tw_metrics(target), timeout=60)
            except asyncio.TimeoutError:
                m = None
            except Exception as e:
                log.error(f"[/stock] single fetch error {target}: {e}", exc_info=True)
                m = None
            if not m:
                await update.message.reply_text(
                    f"❌ 找不到 {target}，請確認股票代號（台積電是 `2330`）",
                    parse_mode=ParseMode.MARKDOWN)
                return
        try:
            streak = int(m.foreign_streak) if m.foreign_streak is not None else 0
            msg = (
                f"📊 *{m.stock_id} {m.name}*  {m.direction}\n\n"
                f"收盤: `{m.close:.2f}`  漲跌: `{m.change_pct:+.2f}%`\n"
                f"成交: `{m.trade_value/1e8:.1f}億`\n\n"
                f"*法人動向*\n"
                f"外資: `{m.foreign_net/1e8:+.2f}億`  連續 `{streak:+d}天`\n"
                f"三大合計: `{m.institutional_net/1e8:+.2f}億`\n\n"
                f"*融資融券*\n"
                f"融資變化: `{m.margin_change_pct:+.1f}%`  "
                f"融券變化: `{m.short_change_pct:+.1f}%`\n\n"
                f"*技術*\n"
                f"總分: `{m.total_score:.0f}`  信心: `{m.confidence:.0%}`\n"
            )
            _sup = f"{m.support:.2f}" if m.support else "—"
            _res = f"{m.resistance:.2f}" if m.resistance else "—"
            msg += f"支撐: `{_sup}`  阻力: `{_res}`\n"
            if m.triggers:
                msg += "\n*訊號*\n" + "\n".join(f"• {t}" for t in m.triggers[:5])
            msg += f"\n\n💡 `/tw_trade {target}` 查看完整交易建議"
            msg += format_trade_chart_block(target, market="tw")
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                            disable_web_page_preview=True)
        except Exception as e:
            log.error(f"[/stock] TW format error for {target}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 格式化失敗: {e}")
        return

    # ── 先試加密貨幣
    try:
        coins = await asyncio.wait_for(get_scan(), timeout=30)
    except asyncio.TimeoutError:
        coins = []
    m_crypto = next((c for c in coins if c.base == target), None)
    if m_crypto:
        await update.message.reply_text(f"🪙 查詢加密貨幣 {target}...")
        try:
            msg      = fmt_card(m_crypto, show_triggers=True)
            sig_4h   = await fetch_4h_signal(target)
            if sig_4h:
                msg += f"\n├ {sig_4h}"
            msg += f"\n\n💡 `/trade {target}` 查看完整交易建議"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                            disable_web_page_preview=True)
        except Exception as e:
            log.error(f"[/stock] crypto format error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 格式化失敗: {e}")
        return

    # ── 試美股
    await update.message.reply_text(f"🇺🇸 查詢美股 {target}…（首次約需 1-2 分鐘）")
    try:
        us_stocks = await asyncio.wait_for(get_us_scan(), timeout=120)
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⏱ 美股資料抓取逾時，伺服器背景掃描中\n請 1-2 分鐘後再試",
            parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        log.error(f"[/stock] US scan error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 美股掃描失敗: {e}")
        return
    m_us = next((s for s in us_stocks if s.ticker == target), None)
    if not m_us:
        await update.message.reply_text(
            f"❌ 找不到 {target}\n"
            f"• 台股請用數字代號（如 `2330`）\n"
            f"• 加密貨幣請用幣種（如 `BTC`）\n"
            f"• 美股請用美股代號（如 `AAPL`）",
            parse_mode=ParseMode.MARKDOWN)
        return
    try:
        msg = fmt_us_card(m_us, show_triggers=True)
        msg += f"\n\n💡 `/us_trade {target}` 查看完整交易建議"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                        disable_web_page_preview=True)
    except Exception as e:
        log.error(f"[/stock] US format error for {target}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 格式化失敗: {e}")

# ============================================================
# 啟動 / 關閉 Hook
# ============================================================
async def post_init(app):
    global tv_handler, _webhook_runner, WATCHLISTS, WATCH_CONDITIONS

    # DB 初始化並載入持久化資料
    _db.init_db()
    WATCHLISTS       = _db.load_watchlists()
    WATCH_CONDITIONS = _db.load_alert_conditions()
    # 載入訂閱（跨部署持久化）
    _subs = _db.load_subscribers()
    SUBSCRIBERS.update(_subs["general"])
    PRE_PUMP_SUBSCRIBERS.update(_subs["pre_pump"])
    TW_SUBSCRIBERS.update(_subs["tw"])
    US_SUBSCRIBERS.update(_subs["us"])
    USER_FILTERS.update(_db.load_user_filters("crypto"))
    TW_USER_FILTERS.update(_db.load_user_filters("tw"))
    US_USER_FILTERS.update(_db.load_user_filters("us"))
    log.info(f"[DB] 載入自選股 {sum(len(v) for v in WATCHLISTS.values())} 筆，"
             f"警報設定 {len(WATCH_CONDITIONS)} 用戶，"
             f"訂閱 general={len(SUBSCRIBERS)} pre={len(PRE_PUMP_SUBSCRIBERS)} "
             f"tw={len(TW_SUBSCRIBERS)} us={len(US_SUBSCRIBERS)}")

    # V2.1: 初始化 TradingView Webhook Handler 並啟動 HTTP 伺服器
    tv_handler = TradingViewWebhookHandler(
        bot=app.bot,
        secret=os.getenv("TV_WEBHOOK_SECRET", "changeme_please"),
    )
    _webhook_runner = await start_webhook_server(
        handler=tv_handler,
        host="0.0.0.0",
        port=int(os.getenv("WEBHOOK_PORT", "8080")),
    )
    log.info("📡 TradingView Webhook 伺服器啟動完成")

    # 設定 Bot 選單
    await app.bot.set_my_commands([
        # 加密貨幣
        BotCommand("pre_pump",       "🔋 預備暴漲 (尚未啟動)"),
        BotCommand("pre_dump",       "⚠️ 預備暴跌"),
        BotCommand("squeeze",        "🎯 壓縮蓄勢"),
        BotCommand("confidence",     "🏆 高信心榜"),
        BotCommand("scan",           "個人化掃描"),
        BotCommand("top10",          "綜合 Top 10"),
        BotCommand("pump",           "看多榜"),
        BotCommand("dump",           "看空榜"),
        BotCommand("trade",          "💡 完整交易建議 /trade BTC"),
        BotCommand("detail",         "詳細指標 /detail BTC"),
        BotCommand("structure",      "OB+FVG /structure BTC"),
        BotCommand("status",         "🤖 Bot 運作狀態"),
        # TradingView
        BotCommand("sub_tv",         "📡 訂閱 TradingView 警報"),
        BotCommand("unsub_tv",       "取消 TradingView 訂閱"),
        BotCommand("tv_status",      "TV Webhook 狀態"),
        # 台股
        BotCommand("tw_scan",        "🇹🇼 台股預備暴漲榜"),
        BotCommand("tw_squeeze",     "🇹🇼 台股 BB 壓縮"),
        BotCommand("tw_foreign",     "🇹🇼 外資連買榜"),
        BotCommand("tw_top10",       "🇹🇼 台股 Top 10"),
        BotCommand("tw_trade",       "🇹🇼 台股交易建議 /tw_trade 2330"),
        BotCommand("tw_detail",      "🇹🇼 台股詳細指標 /tw_detail 2330"),
        BotCommand("tw_status",      "🇹🇼 台股 Bot 狀態"),
        BotCommand("tw_sub",         "🇹🇼 訂閱台股早盤預警"),
        BotCommand("tw_unsub",       "🇹🇼 取消台股訂閱"),
        # 美股
        BotCommand("us_scan",        "🇺🇸 美股預備暴漲榜"),
        BotCommand("us_squeeze",     "🇺🇸 美股 BB 壓縮"),
        BotCommand("us_short",       "🇺🇸 軋空候選榜"),
        BotCommand("us_momentum",    "🇺🇸 動能榜"),
        BotCommand("us_top10",       "🇺🇸 美股 Top 10"),
        BotCommand("us_trade",       "🇺🇸 美股交易建議 /us_trade AAPL"),
        BotCommand("us_detail",      "🇺🇸 美股詳細指標 /us_detail AAPL"),
        BotCommand("us_status",      "🇺🇸 美股 Bot 狀態"),
        BotCommand("us_sub",         "🇺🇸 訂閱美股盤前預警"),
        BotCommand("us_unsub",       "🇺🇸 取消美股訂閱"),
        # 通用查詢
        BotCommand("stock",          "📊 查詢個股 /stock 2330 | BTC | AAPL"),
        # Phase 3: 自選股
        BotCommand("watch",          "⭐ 加入自選股 /watch 2330"),
        BotCommand("unwatch",        "🗑 移除自選股 /unwatch 2330"),
        BotCommand("mywatchlist",    "📋 我的自選股即時狀態"),
        BotCommand("alert",          "⚙️ 設定警報條件 /alert foreign 3"),
        # Phase 2: 倉位追蹤
        BotCommand("enter",          "📥 進場記錄 /enter BTC 81000"),
        BotCommand("close",          "📤 平倉 /close BTC"),
        BotCommand("positions",      "📊 我的倉位"),
        BotCommand("tpsl",           "✏️ 自訂止盈止損 /tpsl BTC 85000 79000"),
        # 設定
        BotCommand("set_score",      "設總分門檻"),
        BotCommand("set_early",      "設早分門檻"),
        BotCommand("set_max_change", "設最大已動 %"),
        BotCommand("myfilters",      "我的篩選"),
        BotCommand("reset",          "重設"),
        BotCommand("sub_pre",        "🔋 訂閱預警(30分)"),
        BotCommand("sub",            "訂閱榜單(1小時)"),
        BotCommand("unsub_all",      "取消全部訂閱"),
        BotCommand("help",           "說明"),
    ])

async def post_shutdown(app):
    """V2.1: 關閉時清理 Webhook HTTP 伺服器"""
    global _webhook_runner
    if _webhook_runner:
        await _webhook_runner.cleanup()
        log.info("📡 TradingView Webhook 伺服器已關閉")

# ============================================================
# 啟動
# ============================================================

async def error_handler(update, ctx):
    """全域錯誤處理：過濾已知無害錯誤，嚴重錯誤才通知 admin"""
    err = ctx.error
    err_str = str(err)

    # 已知無害錯誤 → 只記 log，不打擾用戶和 admin
    ignored = [
        "Message is not modified",
        "Query is too old",
        "query id is invalid",
        "Timed out",
        "TimedOut",
    ]
    if any(x in err_str for x in ignored):
        log.warning(f"[已知錯誤，略過] {type(err).__name__}: {err_str[:80]}")
        return

    # 嚴重錯誤 → 記錄 + 通知 admin
    log.exception(f"[ERROR] {type(err).__name__}: {err_str}")
    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"⚠️ Bot Error\n`{type(err).__name__}: {err_str[:200]}`",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def background_ml_labeler(ctx):
    """每日自動標籤：5天前的掃描資料補上實際漲跌幅"""
    import aiohttp as _aio
    records = _db.get_unlabeled_ml_data(days_ago=5)
    if not records:
        return
    labeled = 0
    async with _aio.ClientSession() as session:
        for r in records:
            try:
                sym, mkt, old_price = r["symbol"], r["market"], r["price"]
                if old_price <= 0:
                    continue
                cur_price = None
                if mkt == "crypto":
                    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}"
                    async with session.get(url, timeout=_aio.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            cur_price = float((await resp.json())["price"])
                elif mkt == "tw":
                    # 從 TW 快取取現價
                    tw_data = TW_CACHE.get("data", [])
                    match = next((m for m in tw_data if m.stock_id == sym), None)
                    if match: cur_price = match.close
                elif mkt == "us":
                    us_data = US_CACHE.get("data", [])
                    match = next((m for m in us_data if m.ticker == sym), None)
                    if match: cur_price = match.close
                if cur_price and cur_price > 0:
                    outcome = (cur_price - old_price) / old_price * 100
                    _db.label_ml_data(r["id"], outcome)
                    labeled += 1
            except Exception:
                pass
    if labeled:
        log.info(f"[ML] 已標籤 {labeled} 筆訓練資料")


async def background_alert_reminder(ctx):
    """每 5 分鐘檢查未回應的 TP/SL 警報，最多提醒 3 次後自動平倉"""
    now = _time.time()
    to_remove = []
    for pid, info in list(PENDING_ALERTS.items()):
        elapsed = now - info["last_alert_time"]
        if elapsed < 300:  # 未到 5 分鐘
            continue
        uid   = info["uid"]
        sym   = info["sym"]
        count = info["alert_count"]
        trigger = info["trigger"]
        pnl   = info["pnl"]
        cur   = info["cur"]
        sl    = info["sl"]
        tp    = info["tp"]

        ordinals = {1: "第一次", 2: "第二次", 3: "第三次"}

        if count < 3:
            # 再次提醒
            try:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 是，關閉倉位", callback_data=f"alert_close:{pid}"),
                    InlineKeyboardButton("🔧 否，調整止損" if trigger == "SL" else "📈 否，繼續持有",
                                         callback_data=f"alert_adjust:{pid}" if trigger == "SL" else f"alert_hold:{pid}"),
                ]])
                emoji = "🚨" if trigger == "SL" else "🎯"
                await ctx.bot.send_message(uid,
                    f"{emoji} *{sym}* {trigger} 警報（{ordinals.get(count+1, '')}）\n\n"
                    f"現價附近 `{cur:.4f}`  損益 `{pnl:+.2f}%`\n"
                    f"仍未處理，請盡快決定！",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                PENDING_ALERTS[pid]["alert_count"] += 1
                PENDING_ALERTS[pid]["last_alert_time"] = now
            except Exception as e:
                log.warning(f"[Alert] 提醒失敗: {e}")
        else:
            # 已提醒 3 次 → 繼續等待，每 5 分鐘持續提醒直到使用者操作
            try:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 是，關閉倉位", callback_data=f"alert_close:{pid}"),
                    InlineKeyboardButton("🔧 否，調整止損" if trigger == "SL" else "📈 否，繼續持有",
                                         callback_data=f"alert_adjust:{pid}" if trigger == "SL" else f"alert_hold:{pid}"),
                ]])
                emoji = "🚨" if trigger == "SL" else "🎯"
                await ctx.bot.send_message(uid,
                    f"{emoji} *{sym}* {trigger} 警報（持續提醒）\n\n"
                    f"現價附近 `{cur:.4f}`  損益 `{pnl:+.2f}%`\n"
                    f"⚠️ 請盡快處理，系統不會自動平倉",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                PENDING_ALERTS[pid]["last_alert_time"] = now
            except Exception as e:
                log.warning(f"[Alert] 持續提醒失敗: {e}")

    for pid in to_remove:
        PENDING_ALERTS.pop(pid, None)


async def cb_alert_close(update, ctx):
    """使用者確認關閉倉位"""
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split(":")[1])
    uid = query.from_user.id
    info = PENDING_ALERTS.pop(pid, None)
    if not info:
        await query.edit_message_text("⚠️ 此警報已處理或失效")
        return
    pos = _db.get_open_positions(uid)
    p   = next((x for x in pos if x["id"] == pid), None)
    if not p:
        await query.edit_message_text("⚠️ 找不到倉位，可能已平倉")
        return
    cur = info["cur"]
    _db.close_position(pid, cur, p["entry_price"], p["direction"])
    pnl = (cur - p["entry_price"]) / p["entry_price"] * 100 * (1 if p["direction"] == "long" else -1)
    await query.edit_message_text(
        f"✅ *{info['sym']}* 倉位已關閉\n損益 `{pnl:+.2f}%`",
        parse_mode=ParseMode.MARKDOWN)


async def cb_alert_adjust(update, ctx):
    """使用者選擇調整止損"""
    query = update.callback_query
    await query.answer()
    pid  = int(query.data.split(":")[1])
    uid  = query.from_user.id
    info = PENDING_ALERTS.get(pid)
    if not info:
        await query.edit_message_text("⚠️ 此警報已處理或失效")
        return
    cur = info["cur"]
    sym = info["sym"]
    direction = info["direction"]
    # 建議新止損：空留 2% 緩衝
    if direction == "long":
        suggest_sl = round(cur * 0.98, 4)
    else:
        suggest_sl = round(cur * 1.02, 4)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ 確認新止損 {suggest_sl}", callback_data=f"alert_newsl:{pid}:{suggest_sl}"),
        InlineKeyboardButton("✏️ 手動輸入", callback_data=f"alert_inputsl:{pid}"),
    ]])
    await query.edit_message_text(
        f"🔧 *{sym}* 調整止損\n\n"
        f"現價 `{cur:.4f}`\n"
        f"建議新止損：`{suggest_sl}` (±2%)\n\n"
        f"選擇確認或手動輸入新止損價格：",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def cb_alert_newsl(update, ctx):
    """確認使用建議的新止損"""
    query = update.callback_query
    await query.answer()
    parts   = query.data.split(":")
    pid     = int(parts[1])
    new_sl  = float(parts[2])
    uid     = query.from_user.id
    info    = PENDING_ALERTS.get(pid)
    if not info:
        await query.edit_message_text("⚠️ 此警報已處理或失效")
        return
    with _db._conn() as c:
        c.execute("UPDATE positions SET sl_price=? WHERE id=?", (new_sl, pid))
    PENDING_ALERTS.pop(pid, None)
    await query.edit_message_text(
        f"✅ *{info['sym']}* 止損已更新\n新 SL：`{new_sl}`",
        parse_mode=ParseMode.MARKDOWN)


async def cb_alert_inputsl(update, ctx):
    """使用者選擇手動輸入止損"""
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split(":")[1])
    uid = query.from_user.id
    _PENDING_SL_INPUT[uid] = pid
    await query.edit_message_text(
        f"✏️ 請直接輸入新的止損價格（數字即可）：")


async def cb_alert_hold(update, ctx):
    """使用者選擇繼續持有（TP 觸發後）"""
    query = update.callback_query
    await query.answer()
    pid  = int(query.data.split(":")[1])
    info = PENDING_ALERTS.pop(pid, None)
    if not info:
        await query.edit_message_text("⚠️ 此警報已處理或失效")
        return
    await query.edit_message_text(
        f"📈 *{info['sym']}* 繼續持有\n止盈警報已解除，背景持續監控中",
        parse_mode=ParseMode.MARKDOWN)


async def cmd_ml_status(update, ctx):
    """顯示 ML 訓練資料收集進度"""
    import time as _t
    stats = _db.get_ml_stats()
    total   = stats["total"]
    labeled = stats["labeled"]
    by_mkt  = stats["by_market"]
    pct = labeled / total * 100 if total else 0

    # 查最近一筆收集時間（未標籤 = bot 自動收集的真實資料）
    with _db._conn() as c:
        row_recent = c.execute(
            "SELECT scan_ts, market FROM ml_scan_data WHERE outcome_pct IS NULL ORDER BY scan_ts DESC LIMIT 1"
        ).fetchone()
        today_count = c.execute(
            "SELECT COUNT(*) FROM ml_scan_data WHERE scan_ts > ? AND outcome_pct IS NULL",
            (int(_t.time()) - 86400,)
        ).fetchone()[0]

    if row_recent:
        from datetime import datetime, timezone
        last_ts = datetime.fromtimestamp(row_recent["scan_ts"]).strftime("%m/%d %H:%M")
        last_mkt = row_recent["market"]
        recent_str = f"最近收集：`{last_ts}` ({last_mkt})  今日新增：`{today_count}` 筆"
    else:
        recent_str = "尚無 bot 自動收集資料"

    msg = (
        f"🤖 *ML 訓練資料收集進度*\n\n"
        f"總筆數：`{total}`  已標籤：`{labeled}` ({pct:.0f}%)\n"
        f"加密：`{by_mkt.get('crypto', 0)}`  "
        f"台股：`{by_mkt.get('tw', 0)}`  "
        f"美股：`{by_mkt.get('us', 0)}`\n\n"
        f"📡 {recent_str}\n\n"
        f"_每次背景掃描自動儲存前 30 高分標的_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def main():
    if BOT_TOKEN.startswith("請填入"):
        raise SystemExit("請先設定 BOT_TOKEN 環境變數")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)   # V2.1
        .build()
    )

    cmds = [
        ("start",          cmd_start),
        ("help",           cmd_help),
        ("pre_pump",       cmd_pre_pump),
        ("pre_dump",       cmd_pre_dump),
        ("squeeze",        cmd_squeeze),
        ("confidence",     cmd_confidence),
        ("scan",           cmd_scan),
        ("top10",          cmd_top10),
        ("pump",           cmd_pump),
        ("dump",           cmd_dump),
        ("trade",          cmd_trade),
        ("detail",         cmd_detail),
        ("structure",      cmd_structure),
        ("status",         cmd_status),
        # V2.1: TradingView 指令
        ("sub_tv",         cmd_sub_tv),
        ("unsub_tv",       cmd_unsub_tv),
        ("tv_status",      cmd_tv_status),
        # V2.2: 台股指令
        ("tw_scan",        cmd_tw_scan),
        ("tw_squeeze",     cmd_tw_squeeze),
        ("tw_foreign",     cmd_tw_foreign),
        ("tw_top10",       cmd_tw_top10),
        ("tw_trade",       cmd_tw_trade),
        ("tw_detail",      cmd_tw_detail),
        ("tw_status",      cmd_tw_status),
        ("tw_sub",         cmd_tw_sub),
        ("tw_unsub",       cmd_tw_unsub),
        ("tw_joint",       cmd_tw_joint),    # 法人聯手榜
        # V2.3: 美股指令
        ("us_scan",        cmd_us_scan),
        ("us_squeeze",     cmd_us_squeeze),
        ("us_short",       cmd_us_short),
        ("us_momentum",    cmd_us_momentum),
        ("us_top10",       cmd_us_top10),
        ("us_trade",       cmd_us_trade),
        ("us_detail",      cmd_us_detail),
        ("us_status",      cmd_us_status),
        ("us_sub",         cmd_us_sub),
        ("us_unsub",       cmd_us_unsub),
        ("us_rs",          cmd_us_rs),       # RS Rating 榜
        ("us_accum",       cmd_us_accum),    # 法人累積榜
        # 篩選設定
        ("set_score",      cmd_set_score),
        ("set_early",      cmd_set_early),
        ("set_max_change", cmd_set_max_change),
        ("myfilters",      cmd_myfilters),
        ("set_tw_score",   cmd_set_tw_score),
        ("set_tw_early",   cmd_set_tw_early),
        ("set_us_score",   cmd_set_us_score),
        ("set_us_early",   cmd_set_us_early),
        ("settw_score",    cmd_set_tw_score),
        ("settw_early",    cmd_set_tw_early),
        ("setus_score",    cmd_set_us_score),
        ("setus_early",    cmd_set_us_early),
        ("reset",          cmd_reset),
        # 訂閱
        ("sub_pre",        cmd_sub_pre),
        ("sub",            cmd_sub),
        ("unsub_all",      cmd_unsub_all),
        # V2.3: 通用查詢
        ("stock",          cmd_stock),
        ("smartmoney",     cmd_smartmoney),  # 聰明錢榜單
        # Phase 3: 自選股 & 警報
        ("watch",          cmd_watch),
        ("unwatch",        cmd_unwatch),
        ("mywatchlist",    cmd_mywatchlist),
        ("alert",          cmd_alert),
        # Phase 2: 倉位追蹤
        ("enter",          cmd_enter),
        ("close",          cmd_close),
        ("positions",      cmd_positions),
        # ── 無底線別名（方便手機輸入）──
        ("prepump",        cmd_pre_pump),
        ("predump",        cmd_pre_dump),
        ("twscan",         cmd_tw_scan),
        ("twsqueeze",      cmd_tw_squeeze),
        ("twforeign",      cmd_tw_foreign),
        ("twtop10",        cmd_tw_top10),
        ("twtrade",        cmd_tw_trade),
        ("twdetail",       cmd_tw_detail),
        ("twstatus",       cmd_tw_status),
        ("twsub",          cmd_tw_sub),
        ("twunsub",        cmd_tw_unsub),
        ("twjoint",        cmd_tw_joint),
        ("usscan",         cmd_us_scan),
        ("ussqueeze",      cmd_us_squeeze),
        ("usshort",        cmd_us_short),
        ("usmomentum",     cmd_us_momentum),
        ("ustop10",        cmd_us_top10),
        ("ustrade",        cmd_us_trade),
        ("usdetail",       cmd_us_detail),
        ("usstatus",       cmd_us_status),
        ("ussub",          cmd_us_sub),
        ("usunsub",        cmd_us_unsub),
        ("usrs",           cmd_us_rs),
        ("usaccum",        cmd_us_accum),
        ("setscore",       cmd_set_score),
        ("setearly",       cmd_set_early),
        ("setmax",         cmd_set_max_change),
        ("subpre",         cmd_sub_pre),
        ("unsuball",       cmd_unsub_all),
        ("mywatchlist",    cmd_mywatchlist),
        ("tpsl",           cmd_tpsl),
        ("ml_status",      cmd_ml_status),
    ]
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))

    # InlineKeyboard 回調
    app.add_handler(CallbackQueryHandler(cb_sub_toggle,    pattern=r"^sub:toggle:"))
    app.add_handler(CallbackQueryHandler(cb_sub_unsub_all, pattern=r"^sub:unsub_all$"))
    app.add_handler(CallbackQueryHandler(cb_help_section,  pattern=r"^help:(?!menu)"))
    app.add_handler(CallbackQueryHandler(cb_help_menu,     pattern=r"^help:menu$"))
    app.add_handler(CallbackQueryHandler(cb_enter,         pattern=r"^enter:"))
    app.add_handler(CallbackQueryHandler(cb_close_pick,    pattern=r"^close_pick:"))
    app.add_handler(CallbackQueryHandler(cb_atr_select,    pattern=r"^atr:"))
    app.add_handler(CallbackQueryHandler(cb_market_select, pattern=r"^mktsel:"))
    app.add_handler(CallbackQueryHandler(cb_alert_close,   pattern=r"^alert_close:"))
    app.add_handler(CallbackQueryHandler(cb_alert_adjust,  pattern=r"^alert_adjust:"))
    app.add_handler(CallbackQueryHandler(cb_alert_newsl,   pattern=r"^alert_newsl:"))
    app.add_handler(CallbackQueryHandler(cb_alert_inputsl, pattern=r"^alert_inputsl:"))
    app.add_handler(CallbackQueryHandler(cb_alert_hold,    pattern=r"^alert_hold:"))
    app.add_error_handler(error_handler)

    # 加密版排程
    app.job_queue.run_repeating(push_pre_warning, interval=1800, first=120)
    app.job_queue.run_repeating(push_general,     interval=3600, first=300)
    # 台股排程：每日 08:50 台灣時間（= UTC 00:50）
    app.job_queue.run_daily(push_tw_morning, time=dtime(0, 50))
    # 美股排程：每日 21:00 台灣時間（= UTC 13:00，美東 09:00 開盤前）
    app.job_queue.run_daily(push_us_premarket, time=dtime(13, 0))
    # 背景預掃描（快取暖身，讓用戶發指令時秒回）
    app.job_queue.run_repeating(background_tw_scan,          interval=300,  first=60)   # 每5分觸發，內部判斷時段
    app.job_queue.run_repeating(background_us_scan,          interval=300,  first=180)  # 每5分觸發，內部判斷時段
    app.job_queue.run_repeating(background_crypto_scan,      interval=180,  first=30)   # 3分鐘
    # Phase 3: 自選股監控（每 30 分鐘）
    app.job_queue.run_repeating(background_watchlist_monitor, interval=1800, first=300)
    # Phase 2: 倉位監控（每 30 分鐘）
    app.job_queue.run_repeating(background_position_monitor,  interval=1800, first=360)
    app.job_queue.run_repeating(background_ml_labeler,        interval=86400, first=3600)  # 每天標籤一次
    app.job_queue.run_repeating(background_alert_reminder,    interval=300,   first=60)   # 每5分鐘提醒未處理警報

    log.info("🤖 妖幣 Bot V2.3 啟動（加密 + TradingView + 台股 + 美股）")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[FATAL] Bot 啟動失敗: {e}", flush=True)
        traceback.print_exc()
        raise SystemExit(1)
