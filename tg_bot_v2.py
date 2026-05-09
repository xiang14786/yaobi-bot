"""
全民 TG 妖幣策略 Bot V2.1
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
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)
from yaobi_scorer_v2 import (
    fetch_all_metrics_v2, apply_filters_v2,
    find_pre_pump, find_pre_dump, find_squeeze,
    DEFAULT_FILTERS_V2, CoinMetricsV2,
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
CACHE_TTL = 120

# V2.1: TradingView Webhook 全域實例（post_init 裡初始化）
tv_handler: TradingViewWebhookHandler | None = None
_webhook_runner = None   # aiohttp AppRunner，shutdown 時清理

# ============================================================
# 共用
# ============================================================
async def get_scan(force=False) -> list[CoinMetricsV2]:
    now = asyncio.get_event_loop().time()
    if not force and now - LAST_SCAN["time"] < CACHE_TTL and LAST_SCAN["data"]:
        return LAST_SCAN["data"]
    data = await fetch_all_metrics_v2(top_n=60)
    LAST_SCAN.update({"time": now, "data": data})
    return data

def get_user_filter(uid):
    return {**DEFAULT_FILTERS_V2, **USER_FILTERS.get(uid, {})}

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
    out = [f"*{title}*  _{datetime.now():%H:%M}_\n"]
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
    msg = (
        "👹 *妖幣雷達 V2.1*  _提早預警 × TradingView_\n\n"
        "🎯 核心改造: 抓「即將妖動」而非「已經妖動」\n\n"
        "*核心指令*\n"
        "/pre\\_pump - 🔋 預備暴漲 (尚未啟動)\n"
        "/pre\\_dump - ⚠️ 預備暴跌\n"
        "/squeeze - 🎯 壓縮蓄勢榜\n"
        "/confidence - 🏆 高信心榜\n"
        "/scan - 預設條件掃描\n\n"
        "*查詢*\n"
        "/detail BTC - 詳細指標\n"
        "/trade BTC - 💡 完整交易建議\n"
        "/structure BTC - OB+FVG 結構\n"
        "/status - 🤖 Bot 運作狀態\n\n"
        "*📡 TradingView*\n"
        "/sub\\_tv - 訂閱 TV 警報推送\n"
        "/unsub\\_tv - 取消訂閱\n"
        "/tv\\_status - Webhook 狀態\n\n"
        "/help - 完整說明"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update, ctx):
    msg = (
        "*完整指令*\n\n"
        "*🎯 提早預警*\n"
        "`/pre_pump` 即將暴漲\n"
        "`/pre_dump` 即將暴跌\n"
        "`/squeeze` BB 壓縮 + OI 建倉\n"
        "`/confidence` 多訊號共振\n\n"
        "*📊 查詢*\n"
        "`/scan` 個人篩選掃描\n"
        "`/top10` 綜合 Top 10\n"
        "`/pump` 看多榜\n"
        "`/dump` 看空榜\n"
        "`/detail BTC` 單幣詳細指標\n"
        "`/trade BTC` 💡 完整交易建議\n"
        "`/structure BTC` OB+FVG 結構\n"
        "`/status` Bot 運作狀態\n\n"
        "*📡 TradingView*\n"
        "`/sub_tv` 訂閱 TV Webhook 信號\n"
        "`/unsub_tv` 取消 TV 訂閱\n"
        "`/tv_status` Webhook 狀態與最近信號\n\n"
        "*🔧 個人化篩選*\n"
        "`/set_score 60` 最低總分\n"
        "`/set_early 50` 最低早分\n"
        "`/set_max_change 12` 最大已動 %\n"
        "`/myfilters` 查看設定\n"
        "`/reset` 重設\n\n"
        "*🔔 訂閱*\n"
        "`/sub_pre` 預警推送 (30 分鐘)\n"
        "`/sub` 榜單推送 (1 小時)\n"
        "`/unsub_all` 取消全部\n\n"
        "⚠️ 不構成投資建議，風險自負"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_pre_pump(update, ctx):
    await update.message.reply_text("🔋 偵測蓄勢拉升中...")
    coins = await get_scan()
    pre = find_pre_pump(coins)
    text = fmt_list(pre, "🔋 預備暴漲榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_pre_dump(update, ctx):
    await update.message.reply_text("⚠️ 偵測蓄勢下殺中...")
    coins = await get_scan()
    pre = find_pre_dump(coins)
    text = fmt_list(pre, "⚠️ 預備暴跌榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_squeeze(update, ctx):
    await update.message.reply_text("🎯 偵測壓縮蓄勢...")
    coins = await get_scan()
    sq = find_squeeze(coins)
    text = fmt_list(sq, "🎯 壓縮蓄勢榜 (BB+OI)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_confidence(update, ctx):
    coins = await get_scan()
    high = [c for c in coins if c.confidence >= 0.6]
    high.sort(key=lambda c: c.confidence, reverse=True)
    text = fmt_list(high, "🏆 高信心度榜 (多訊號共振)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_scan(update, ctx):
    await update.message.reply_text("🔍 掃描中...")
    coins = await get_scan()
    f = get_user_filter(update.effective_user.id)
    filtered = apply_filters_v2(coins, f)
    text = fmt_list(filtered, f"👹 妖幣雷達 V2 ({len(filtered)} 命中)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_top10(update, ctx):
    coins = await get_scan()
    text = fmt_list(coins, "🏆 綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_pump(update, ctx):
    coins = await get_scan()
    pumps = [c for c in coins if "🚀" in c.direction or "🔋" in c.direction]
    text = fmt_list(pumps, "🚀 看多榜 (含預警+延續)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_dump(update, ctx):
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
        elapsed = int(time.time() - scan_time)
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
        f"🤖 *Bot 運作狀態 V2.1*\n\n"
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
    USER_FILTERS.setdefault(update.effective_user.id, {})["min_total_score"] = v
    await update.message.reply_text(f"✅ 最低總分 = {v}")

async def cmd_set_early(update, ctx):
    try:
        v = float(ctx.args[0]); assert 0 <= v <= 95
    except:
        await update.message.reply_text("用法: `/set_early 50`", parse_mode=ParseMode.MARKDOWN); return
    USER_FILTERS.setdefault(update.effective_user.id, {})["min_early_score"] = v
    await update.message.reply_text(f"✅ 最低早期分 = {v}")

async def cmd_set_max_change(update, ctx):
    try:
        v = float(ctx.args[0]); assert 1 <= v <= 50
    except:
        await update.message.reply_text("用法: `/set_max_change 12`", parse_mode=ParseMode.MARKDOWN); return
    USER_FILTERS.setdefault(update.effective_user.id, {})["max_price_change_pct"] = v
    await update.message.reply_text(f"✅ 最大已動幅度 = {v}%")

async def cmd_myfilters(update, ctx):
    f = get_user_filter(update.effective_user.id)
    msg = (
        "*你的篩選設定*\n\n"
        f"最低總分: `{f['min_total_score']}`\n"
        f"最低早分: `{f['min_early_score']}`\n"
        f"最大已動 %: `{f['max_price_change_pct']}`\n"
        f"最低成交額: `${f['min_quote_volume_usd']/1e6:.0f}M`\n"
        f"排除穩定幣: `{f['exclude_stablecoins']}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_reset(update, ctx):
    USER_FILTERS.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ 已重設")

# ============================================================
# 訂閱
# ============================================================
async def cmd_sub_pre(update, ctx):
    PRE_PUMP_SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("🔋 已訂閱預警！每 30 分鐘自動推送預備暴漲/暴跌榜")

async def cmd_sub(update, ctx):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("🔔 已訂閱！每小時推送 Top 5")

async def cmd_unsub_all(update, ctx):
    cid = update.effective_chat.id
    SUBSCRIBERS.discard(cid)
    PRE_PUMP_SUBSCRIBERS.discard(cid)
    if tv_handler:
        tv_handler.remove_subscriber(cid)
    await update.message.reply_text("🔕 已取消全部訂閱（含 TradingView 警報）")

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
    pre_pump = find_pre_pump(coins)[:3]
    pre_dump = find_pre_dump(coins)[:3]
    if not pre_pump and not pre_dump:
        return
    parts = [f"🔋 *預警快訊*  _{datetime.now():%H:%M}_\n"]
    if pre_pump:
        parts.append("*預備暴漲*")
        for i, c in enumerate(pre_pump, 1):
            parts.append(fmt_card(c, i, show_triggers=True))
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
# 啟動 / 關閉 Hook
# ============================================================
async def post_init(app):
    global tv_handler, _webhook_runner

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
        BotCommand("sub_tv",         "📡 訂閱 TradingView 警報"),
        BotCommand("unsub_tv",       "取消 TradingView 訂閱"),
        BotCommand("tv_status",      "TV Webhook 狀態"),
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
        # 篩選設定
        ("set_score",      cmd_set_score),
        ("set_early",      cmd_set_early),
        ("set_max_change", cmd_set_max_change),
        ("myfilters",      cmd_myfilters),
        ("reset",          cmd_reset),
        # 訂閱
        ("sub_pre",        cmd_sub_pre),
        ("sub",            cmd_sub),
        ("unsub_all",      cmd_unsub_all),
    ]
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))

    app.job_queue.run_repeating(push_pre_warning, interval=1800, first=120)
    app.job_queue.run_repeating(push_general,     interval=3600, first=300)

    log.info("🤖 妖幣 Bot V2.1 啟動（含 TradingView 整合）")
    app.run_polling()

if __name__ == "__main__":
    main()
