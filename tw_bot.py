"""
tw_bot.py
=========
台股版 Telegram Bot

特點：
  - 資料來源：FinMind API（三大法人、融資融券）+ TWSE
  - 評分複用加密版 6 個通用指標 + 台股 2 個特有指標
  - 倉位建議 % 取代槓桿
  - 交易時段限制（09:00~13:30，週一到週五）
  - 整合 TradingView 圖表連結（複用 tv_chart_utils.py）

新增指令：
  /tw_scan       台股預備暴漲榜
  /tw_squeeze    BB 壓縮蓄勢榜
  /tw_foreign    外資連續買超榜
  /tw_top10      台股綜合 Top 10
  /tw_trade 2330 完整交易建議
  /tw_detail 2330 詳細指標
  /tw_status     Bot 狀態

部署：與加密版共用同一個 Railway 專案，
      只需額外設定 FINMIND_TOKEN 環境變數（可選）
"""

import asyncio
import logging
import os
import time
from datetime import datetime, time as dtime

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from tw_stock_scorer import (
    TwStockMetrics,
    fetch_all_tw_metrics,
    apply_tw_filters,
    find_tw_pre_pump,
    find_tw_squeeze,
    find_tw_institutional_buy,
    DEFAULT_TW_FILTERS,
)
from tv_chart_utils import get_tv_chart_url, format_trade_chart_block

log = logging.getLogger("tw_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "請填入_你的_token")

# ── 快取 ──────────────────────────────────────
TW_CACHE: dict = {"time": 0, "data": []}
TW_CACHE_TTL = 600   # 10 分鐘（台股日頻資料，快取久一點）

# ── 訂閱 ──────────────────────────────────────
TW_SUBSCRIBERS: set = set()
TW_USER_FILTERS: dict = {}

# ── 交易時段 ──────────────────────────────────
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(13, 30)


# ============================================================
# 工具函式
# ============================================================
def is_trading_hours() -> bool:
    """判斷是否為台股交易時間（週一到週五 09:00~13:30）"""
    now = datetime.now()
    if now.weekday() >= 5:          # 週六/日
        return False
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def market_status_str() -> str:
    now = datetime.now()
    if now.weekday() >= 5:
        return "⛔ 週末休市"
    t = now.time()
    if t < MARKET_OPEN:
        return f"⏰ 尚未開盤（{MARKET_OPEN.strftime('%H:%M')} 開盤）"
    if t > MARKET_CLOSE:
        return f"🔒 今日收盤（{MARKET_CLOSE.strftime('%H:%M')} 收盤）"
    return "🟢 交易中"


async def get_tw_scan(force=False) -> list[TwStockMetrics]:
    now = asyncio.get_event_loop().time()
    if not force and now - TW_CACHE["time"] < TW_CACHE_TTL and TW_CACHE["data"]:
        return TW_CACHE["data"]
    data = await fetch_all_tw_metrics(top_n=60)
    TW_CACHE.update({"time": now, "data": data})
    return data


def get_user_tw_filter(uid) -> dict:
    return {**DEFAULT_TW_FILTERS, **TW_USER_FILTERS.get(uid, {})}


# ============================================================
# 卡片格式化
# ============================================================
def fmt_tw_card(m: TwStockMetrics, rank=None, show_triggers=True) -> str:
    rank_str = f"#{rank} " if rank else ""
    tag_str  = "  ".join(m.tags) if m.tags else ""

    # 成交金額換算億
    val_str = f"{m.trade_value/1e8:.1f} 億" if m.trade_value >= 1e8 else f"{m.trade_value/1e6:.0f} 百萬"

    lines = [
        f"{rank_str}*{m.stock_id} {m.name}*  {m.direction}",
        f"├ 總分 *{m.total_score:.0f}*  領先 *{m.early_score:.0f}*  信心 *{m.confidence:.0%}*",
        f"├ 收盤: ${m.close:,.2f}  漲跌: {m.change_pct:+.2f}%",
        f"├ 成交: {val_str}",
        f"├ 外資: {m.foreign_net/1e8:+.1f}億  連續: {m.foreign_streak:+d}天",
        f"├ 融資變化: {m.margin_change_pct:+.1f}%  融券變化: {m.short_change_pct:+.1f}%",
    ]
    if m.support or m.resistance:
        s = f"${m.support:,.2f}"  if m.support    else "—"
        r = f"${m.resistance:,.2f}" if m.resistance else "—"
        lines.append(f"├ 支撐: {s}  阻力: {r}")
    if show_triggers and m.triggers:
        for t in m.triggers[:3]:
            lines.append(f"├ {t}")
    if tag_str:
        lines.append(f"├ {tag_str}")
    # TradingView 圖表連結（台股）
    chart_url = get_tv_chart_url(m.stock_id, timeframe="D", market="tw")
    lines.append(f"└ [📊 TV 圖表]({chart_url})")
    return "\n".join(lines)


def fmt_tw_list(stocks, title, show_triggers=True) -> str:
    if not stocks:
        return f"*{title}*\n\n目前沒有符合條件的標的 🌙"
    mkt = market_status_str()
    out = [f"*{title}*  _{datetime.now():%H:%M}_  {mkt}\n"]
    for i, m in enumerate(stocks[:8], 1):
        out.append(fmt_tw_card(m, i, show_triggers))
        out.append("")
    out.append("_資料源: FinMind API + TWSE_")
    return "\n".join(out)


# ============================================================
# 交易建議生成
# ============================================================
def generate_tw_trade_advice(m: TwStockMetrics) -> str:
    price = m.close

    # 方向
    is_bull = "🚀" in m.direction or "🔋" in m.direction or "🔵" in m.direction
    is_bear = "📉" in m.direction or "⚠️" in m.direction
    dir_str = "做多 🟢" if is_bull else "做空（放空）🔴" if is_bear else "觀望 ⚪"

    # 信心等級
    conf = m.confidence
    if conf >= 0.8:   conf_str = "非常高 ⭐⭐⭐"
    elif conf >= 0.65: conf_str = "高 ⭐⭐"
    elif conf >= 0.5:  conf_str = "中等 ⭐"
    else:              conf_str = "偏低 ⚠️"

    # 倉位建議（台股現股，不用槓桿）
    pos = m.position_pct
    if pos == 0:
        pos_note = "🚫 信心不足，暫時觀望"
        pos_block = f"建議倉位: 不開倉\n{pos_note}"
    else:
        pos_block = (
            f"━━━ 倉位建議 ━━━\n"
            f"🟢 保守: 總資金 `{pos//2}%`\n"
            f"🟡 標準: 總資金 `{pos}%`\n"
            f"🔴 積極: 總資金 `{min(pos*2, 30)}%`\n"
            f"💡 台股現股，無槓桿"
        )

    # 進出場區間
    if is_bull and m.support:
        entry_low  = m.support * 0.998
        entry_high = m.support * 1.015
        stop_loss  = m.support * 0.975
        tp1 = m.resistance if m.resistance else price * 1.08
        tp2 = tp1 * 1.05
        rr  = (tp1 - entry_high) / (entry_high - stop_loss) if entry_high > stop_loss else 0
        in_zone = entry_low <= price <= entry_high * 1.02
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
    elif is_bear and m.resistance:
        tp1 = m.support if m.support else price * 0.92
        rr  = (m.resistance - price) / (price - tp1) if price > tp1 else 0
        trade_block = (
            f"━━━ 放空參考 ━━━\n"
            f"📍 放空區: 靠近 `${m.resistance:,.2f}` 阻力\n"
            f"🛑 停損: `${m.resistance*1.025:,.2f}` (突破停損)\n"
            f"🎯 目標: `${tp1:,.2f}` (-{(price-tp1)/price*100:.1f}%)\n"
            f"📊 風報比: `1 : {rr:.1f}`\n\n"
            f"⚠️ 台股融券需注意券源與強制回補日\n\n"
        )
    else:
        trade_block = "⚠️ 結構不明確，建議觀望等待更清晰訊號\n\n"

    # 法人資訊摘要
    inst_summary = (
        f"━━━ 法人動向 ━━━\n"
        f"外資: {m.foreign_net/1e8:+.1f} 億  連續 {m.foreign_streak:+d} 天\n"
        f"三大合計: {m.institutional_net/1e8:+.1f} 億\n"
        f"融資變化: {m.margin_change_pct:+.1f}%  融券變化: {m.short_change_pct:+.1f}%\n"
    )

    # 警告
    warnings = []
    if abs(m.change_pct) > 7:
        warnings.append(f"⚠️ 今日已漲跌 {m.change_pct:+.1f}%，接近漲跌停")
    if m.confidence < 0.4:
        warnings.append("⚠️ 信心偏低，建議等更多訊號")
    if m.trade_value < 5e8:
        warnings.append("⚠️ 成交金額偏低，流動性注意")
    warning_str = "\n".join(warnings) if warnings else "✅ 無特殊警告"

    return (
        f"💡 *{m.stock_id} {m.name} 交易建議*\n\n"
        f"收盤價: `${price:,.2f}`  {market_status_str()}\n"
        f"方向: {dir_str}\n"
        f"信心: {conf_str}\n\n"
        f"{trade_block}"
        f"{pos_block}\n\n"
        f"{inst_summary}\n"
        f"━━━ 注意事項 ━━━\n"
        f"{warning_str}\n\n"
        f"_⚠️ 純技術分析，非投資建議，請自行控管風險_"
    )


# ============================================================
# 指令處理
# ============================================================
async def cmd_tw_start(update, ctx):
    msg = (
        "🇹🇼 *台股妖股雷達*\n\n"
        "核心功能：抓「即將妖動」的台股，而非「已經妖動」的\n\n"
        "*指令*\n"
        "/tw\\_scan - 🔋 預備暴漲榜\n"
        "/tw\\_squeeze - 🎯 BB 壓縮蓄勢\n"
        "/tw\\_foreign - 🏦 外資連買榜\n"
        "/tw\\_top10 - 🏆 台股 Top 10\n"
        "/tw\\_trade 2330 - 💡 完整交易建議\n"
        "/tw\\_detail 2330 - 詳細指標\n"
        "/tw\\_status - 🤖 狀態\n\n"
        f"市場狀態：{market_status_str()}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_tw_scan(update, ctx):
    await update.message.reply_text("🔍 掃描台股中...")
    stocks = await get_tw_scan()
    f = get_user_tw_filter(update.effective_user.id)
    filtered = find_tw_pre_pump(stocks)
    text = fmt_tw_list(filtered, f"🔋 台股預備暴漲榜 ({len(filtered)} 命中)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_tw_squeeze(update, ctx):
    await update.message.reply_text("🎯 偵測 BB 壓縮中...")
    stocks = await get_tw_scan()
    sq = find_tw_squeeze(stocks)
    text = fmt_tw_list(sq, "🎯 台股 BB 壓縮蓄勢榜")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_tw_foreign(update, ctx):
    stocks = await get_tw_scan()
    foreign = find_tw_institutional_buy(stocks)
    text = fmt_tw_list(foreign, "🏦 外資連續買超榜（≥3天）")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_tw_top10(update, ctx):
    stocks = await get_tw_scan()
    text = fmt_tw_list(stocks[:10], "🏆 台股綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_tw_trade(update, ctx):
    if not ctx.args:
        await update.message.reply_text("用法: `/tw_trade 2330`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0].strip()
    await update.message.reply_text(f"💡 分析 {target} 中...")
    stocks = await get_tw_scan()
    m = next((s for s in stocks if s.stock_id == target), None)
    if not m:
        await update.message.reply_text(
            f"❌ 找不到 {target}，可能成交量太低未列入掃描\n"
            f"請確認股票代號（例如台積電是 `2330`）",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    advice = generate_tw_trade_advice(m)
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
        f"BB 壓縮 `{m.score_bb:.0f}`\n"
        f"量能階梯 `{m.score_vol_ladder:.0f}`\n"
        f"沉睡甦醒 `{m.score_sleep:.0f}`\n"
        f"波動率擴張 `{m.score_atr:.0f}`\n"
        f"CVD 背離 `{m.score_cvd:.0f}`\n"
        f"OB+FVG 結構 `{m.score_ob_fvg:.0f}`\n\n"
        f"*台股特有*\n"
        f"法人動向 `{m.score_institution:.0f}`\n"
        f"融資融券 `{m.score_margin:.0f}`\n\n"
        f"*法人明細*\n"
        f"外資淨買賣: `{m.foreign_net/1e8:+.2f}` 億\n"
        f"外資連續: `{m.foreign_streak:+d}` 天\n"
        f"三大法人合計: `{m.institutional_net/1e8:+.2f}` 億\n"
        f"融資餘額變化: `{m.margin_change_pct:+.1f}%`\n"
        f"融券餘額變化: `{m.short_change_pct:+.1f}%`\n"
    )
    if m.triggers:
        msg += "\n*觸發訊號*\n" + "\n".join(f"• {t}" for t in m.triggers)
    if m.support or m.resistance:
        msg += f"\n\n*結構位*\n"
        if m.support:
            msg += f"支撐 (Bullish OB): `${m.support:,.2f}`\n"
        if m.resistance:
            msg += f"阻力 (Bearish OB): `${m.resistance:,.2f}`\n"
    msg += f"\n💡 `/tw_trade {target}` 查看完整交易建議"
    msg += format_trade_chart_block(target, market="tw")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_tw_status(update, ctx):
    stocks    = TW_CACHE.get("data", [])
    scan_time = TW_CACHE.get("time", 0)
    if scan_time == 0:
        last_str = "尚未掃描"
    else:
        elapsed = int(time.time() - scan_time)
        last_str = f"{elapsed//60} 分鐘前" if elapsed >= 60 else f"{elapsed} 秒前"
    pre  = find_tw_pre_pump(stocks)
    sq   = find_tw_squeeze(stocks)
    fore = find_tw_institutional_buy(stocks)
    msg = (
        f"🇹🇼 *台股 Bot 狀態*\n\n"
        f"{market_status_str()}\n"
        f"上次掃描: `{last_str}`\n"
        f"掃描股票數: `{len(stocks)}`\n\n"
        f"*當前訊號*\n"
        f"🔋 預備暴漲: `{len(pre)}` 支\n"
        f"🎯 BB 壓縮: `{len(sq)}` 支\n"
        f"🏦 外資連買: `{len(fore)}` 支\n\n"
        f"*訂閱人數*: `{len(TW_SUBSCRIBERS)}` 人\n\n"
        f"_資料源: FinMind API + TWSE_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_tw_sub(update, ctx):
    TW_SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text(
        "🔔 已訂閱台股預警！\n"
        "每日 08:50（開盤前）和 13:00（盤中）自動推送"
    )


async def cmd_tw_unsub(update, ctx):
    TW_SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 已取消台股訂閱")


# ============================================================
# 排程推送
# ============================================================
async def push_tw_morning(ctx):
    """每日 08:50 推送開盤前預警"""
    if not TW_SUBSCRIBERS:
        return
    try:
        stocks = await get_tw_scan(force=True)
    except Exception as e:
        log.error(f"台股早盤推送失敗: {e}")
        return
    pre  = find_tw_pre_pump(stocks)[:3]
    fore = find_tw_institutional_buy(stocks)[:3]
    if not pre and not fore:
        return
    parts = [f"🇹🇼 *台股開盤前預警*  _{datetime.now():%m/%d %H:%M}_\n"]
    if pre:
        parts.append("*🔋 預備暴漲（技術面）*")
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
# 啟動（獨立運行模式）
# ============================================================
async def post_init_tw(app):
    await app.bot.set_my_commands([
        BotCommand("tw_scan",    "🔋 台股預備暴漲榜"),
        BotCommand("tw_squeeze", "🎯 BB 壓縮蓄勢"),
        BotCommand("tw_foreign", "🏦 外資連買榜"),
        BotCommand("tw_top10",   "🏆 台股 Top 10"),
        BotCommand("tw_trade",   "💡 交易建議 /tw_trade 2330"),
        BotCommand("tw_detail",  "詳細指標 /tw_detail 2330"),
        BotCommand("tw_status",  "🤖 台股 Bot 狀態"),
        BotCommand("tw_sub",     "訂閱台股預警"),
        BotCommand("tw_unsub",   "取消台股訂閱"),
    ])


def register_tw_handlers(app):
    """
    把台股指令掛進現有的 Application 實例。
    在 tg_bot_v2.py 的 main() 裡呼叫這個函式，就能讓加密版 Bot
    同時支援台股指令，不需要開兩個 Bot。

    用法（在 tg_bot_v2.py 的 main() 加入）：
        from tw_bot import register_tw_handlers
        register_tw_handlers(app)
    """
    cmds = [
        ("tw_scan",    cmd_tw_scan),
        ("tw_squeeze", cmd_tw_squeeze),
        ("tw_foreign", cmd_tw_foreign),
        ("tw_top10",   cmd_tw_top10),
        ("tw_trade",   cmd_tw_trade),
        ("tw_detail",  cmd_tw_detail),
        ("tw_status",  cmd_tw_status),
        ("tw_sub",     cmd_tw_sub),
        ("tw_unsub",   cmd_tw_unsub),
    ]
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))
    # 排程：每日 08:50 推送（UTC+8 = UTC 00:50）
    app.job_queue.run_daily(push_tw_morning, time=dtime(0, 50))
    log.info("🇹🇼 台股指令已掛載")


def main():
    """獨立啟動台股 Bot（不與加密版合併時使用）"""
    if BOT_TOKEN.startswith("請填入"):
        raise SystemExit("請先設定 BOT_TOKEN 環境變數")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init_tw)
        .build()
    )
    register_tw_handlers(app)
    log.info("🇹🇼 台股妖股雷達啟動")
    app.run_polling()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    main()
