"""
全民 TG 妖幣策略 Bot V2
========================
V2 重點功能:
- 🔋 提早預警: 抓「尚未啟動」的妖幣
- 📐 OB+FVG 結構分析
- ⏳ BB 壓縮 / OI 建倉偵測
- 🎯 多時框共振確認
- 🤖 /status 查詢 Bot 運作狀態
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


# ============================================================
# 共用
# ============================================================
async def get_scan(force=False) -> list[CoinMetricsV2]:
    now = asyncio.get_event_loop().time()
    if not force and now - LAST_SCAN["time"] < CACHE_TTL and LAST_SCAN["data"]:
        return LAST_SCAN["data"]
    data = await fetch_all_metrics_v2(top_n=40)
    LAST_SCAN.update({"time": now, "data": data})
    return data


def get_user_filter(uid):
    return {**DEFAULT_FILTERS_V2, **USER_FILTERS.get(uid, {})}


def fmt_card(c: CoinMetricsV2, rank=None, show_triggers=True) -> str:
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
        lines.append(f"└ {tag_str}")
    else:
        lines[-1] = lines[-1].replace("├", "└", 1)

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
# 指令
# ============================================================
async def cmd_start(update, ctx):
    msg = (
        "👹 *妖幣雷達 V2*  _提早預警版_\n\n"
        "🎯 核心改造: 抓「即將妖動」而非「已經妖動」\n\n"
        "*核心指令*\n"
        "/pre\\_pump - 🔋 預備暴漲 (尚未啟動)\n"
        "/pre\\_dump - ⚠️ 預備暴跌\n"
        "/squeeze - 🎯 壓縮蓄勢榜\n"
        "/confidence - 🏆 高信心榜\n"
        "/scan - 預設條件掃描\n\n"
        "*查詢*\n"
        "/detail BTC - 詳細指標\n"
        "/structure BTC - OB+FVG 結構\n"
        "/status - 🤖 Bot 運作狀態\n\n"
        "/help - 完整說明"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update, ctx):
    msg = (
        "*完整指令*\n\n"
        "*🎯 提早預警 (V2 主打)*\n"
        "`/pre_pump` 即將暴漲 (24h 漲跌 <8%)\n"
        "`/pre_dump` 即將暴跌\n"
        "`/squeeze` BB 壓縮 + OI 建倉\n"
        "`/confidence` 多訊號共振\n\n"
        "*📊 一般查詢*\n"
        "`/scan` 個人篩選掃描\n"
        "`/top10` 綜合 Top 10\n"
        "`/pump` 已暴漲清單\n"
        "`/dump` 已暴跌清單\n"
        "`/detail BTC` 單幣詳細\n"
        "`/structure BTC` OB+FVG 結構\n"
        "`/status` Bot 運作狀態\n\n"
        "*🔧 個人化篩選*\n"
        "`/set_score 60` 最低總分\n"
        "`/set_early 50` 最低早分\n"
        "`/set_max_change 12` 最大已動 %\n"
        "`/myfilters` 查看設定\n"
        "`/reset` 重設\n\n"
        "*🔔 訂閱*\n"
        "`/sub_pre` 預警自動推送 (30 分鐘)\n"
        "`/sub` 一般榜單推送 (1 小時)\n"
        "`/unsub_all` 全部取消\n\n"
        "*🧠 偵測維度*\n"
        "OI 暴增 + 價格平靜 → 大資金建倉\n"
        "費率背離 → 軋多/軋空風險\n"
        "BB 壓縮 → 波動率即將擴張\n"
        "量能階梯 → 持續性買賣盤\n"
        "多空比急轉 → 散戶情緒反指\n"
        "CVD 背離 → 量價背離訊號\n"
        "沉睡甦醒 → 長期低波突發異動\n"
        "OB+FVG → 機構訂單區/缺口\n"
        "多時框共振 → 15m/1h/4h 一致\n\n"
        "⚠️ 不構成投資建議,風險自負"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_pre_pump(update, ctx):
    await update.message.reply_text("🔋 偵測蓄勢拉升中...")
    coins = await get_scan()
    pre = find_pre_pump(coins)
    text = fmt_list(pre, "🔋 預備暴漲榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_pre_dump(update, ctx):
    await update.message.reply_text("⚠️ 偵測蓄勢下殺中...")
    coins = await get_scan()
    pre = find_pre_dump(coins)
    text = fmt_list(pre, "⚠️ 預備暴跌榜 (尚未啟動)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_squeeze(update, ctx):
    await update.message.reply_text("🎯 偵測壓縮蓄勢...")
    coins = await get_scan()
    sq = find_squeeze(coins)
    text = fmt_list(sq, "🎯 壓縮蓄勢榜 (BB+OI)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_confidence(update, ctx):
    coins = await get_scan()
    high = [c for c in coins if c.confidence >= 0.6]
    high.sort(key=lambda c: c.confidence, reverse=True)
    text = fmt_list(high, "🏆 高信心度榜 (多訊號共振)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_scan(update, ctx):
    await update.message.reply_text("🔍 掃描中...")
    coins = await get_scan()
    f = get_user_filter(update.effective_user.id)
    filtered = apply_filters_v2(coins, f)
    text = fmt_list(filtered, f"👹 妖幣雷達 V2 ({len(filtered)} 命中)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_top10(update, ctx):
    coins = await get_scan()
    text = fmt_list(coins, "🏆 綜合 Top 10", show_triggers=False)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_pump(update, ctx):
    coins = await get_scan()
    pumps = [c for c in coins if "🚀" in c.direction or "🔋" in c.direction]
    text = fmt_list(pumps, "🚀 看多榜 (含預警+延續)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_dump(update, ctx):
    coins = await get_scan()
    dumps = [c for c in coins if "📉" in c.direction or "⚠️" in c.direction]
    text = fmt_list(dumps, "📉 看空榜 (含預警+延續)")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
            msg += f"支撐: ${m.nearest_support:,.4f}\n"
        if m.nearest_resistance:
            msg += f"阻力: ${m.nearest_resistance:,.4f}\n"
    if m.tags:
        msg += "\n" + " ".join(m.tags)

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


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

    msg = (
        f"📐 *{m.base}/USDT 結構分析*\n\n"
        f"當前價: `${m.last_price:,.4f}`\n"
        f"結構分: `{m.score_structure:.0f}/100`\n"
        f"結構偏向: `{m.structure_bias}`\n\n"
    )
    if m.nearest_support:
        dist = (m.last_price - m.nearest_support) / m.last_price * 100
        msg += f"🟢 *最近支撐 (Bullish OB)*\n   ${m.nearest_support:,.4f} (距 {dist:.2f}%)\n\n"
    if m.nearest_resistance:
        dist = (m.nearest_resistance - m.last_price) / m.last_price * 100
        msg += f"🔴 *最近阻力 (Bearish OB)*\n   ${m.nearest_resistance:,.4f} (距 {dist:.2f}%)\n\n"

    structure_triggers = [t for t in m.triggers if "OB" in t or "FVG" in t or "結構" in t]
    if structure_triggers:
        msg += "*結構訊號*\n" + "\n".join(f"• {t}" for t in structure_triggers) + "\n\n"

    msg += (
        "*ICT 操作建議*\n"
        "• 價格回測 Bullish OB 不破 → 多單\n"
        "• 價格反彈 Bearish OB 不過 → 空單\n"
        "• FVG 50% 為理想進場區\n"
        "• 突破結構後等回踩確認"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update, ctx):
    coins = LAST_SCAN.get("data", [])
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

    pre_pump = find_pre_pump(coins)
    pre_dump = find_pre_dump(coins)
    squeeze = find_squeeze(coins)

    msg = (
        f"🤖 *Bot 運作狀態*\n\n"
        f"✅ 運作中\n"
        f"🕐 上次掃描: `{last_scan_str}`\n"
        f"📊 掃描標的數: `{len(coins)}`\n\n"
        f"*當前訊號*\n"
        f"🔋 預備暴漲: `{len(pre_pump)}` 個\n"
        f"⚠️ 預備暴跌: `{len(pre_dump)}` 個\n"
        f"🎯 壓縮蓄勢: `{len(squeeze)}` 個\n\n"
        f"*訂閱人數*\n"
        f"預警訂閱: `{len(PRE_PUMP_SUBSCRIBERS)}` 人\n"
        f"榜單訂閱: `{len(SUBSCRIBERS)}` 人\n\n"
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
    await update.message.reply_text(f"✅ 最大已動幅度 = {v}% (超過視為已晚)")


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
    await update.message.reply_text("🔕 已取消全部訂閱")


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
    text = "\n".join(parts)

    for cid in list(PRE_PUMP_SUBSCRIBERS):
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN)
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
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            SUBSCRIBERS.discard(cid)


# ============================================================
# 啟動
# ============================================================
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("pre_pump", "🔋 預備暴漲 (尚未啟動)"),
        BotCommand("pre_dump", "⚠️ 預備暴跌"),
        BotCommand("squeeze", "🎯 壓縮蓄勢"),
        BotCommand("confidence", "🏆 高信心榜"),
        BotCommand("scan", "個人化掃描"),
        BotCommand("top10", "綜合 Top 10"),
        BotCommand("pump", "看多榜"),
        BotCommand("dump", "看空榜"),
        BotCommand("detail", "詳細指標 /detail BTC"),
        BotCommand("structure", "OB+FVG /structure BTC"),
        BotCommand("status", "🤖 Bot 運作狀態"),
        BotCommand("set_score", "設總分門檻"),
        BotCommand("set_early", "設早分門檻"),
        BotCommand("set_max_change", "設最大已動 %"),
        BotCommand("myfilters", "我的篩選"),
        BotCommand("reset", "重設"),
        BotCommand("sub_pre", "🔋 訂閱預警(30分)"),
        BotCommand("sub", "訂閱榜單(1小時)"),
        BotCommand("unsub_all", "取消全部訂閱"),
        BotCommand("help", "說明"),
    ])


def main():
    if BOT_TOKEN.startswith("請填入"):
        raise SystemExit("請先設定 BOT_TOKEN 環境變數")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    cmds = [
        ("start", cmd_start), ("help", cmd_help),
        ("pre_pump", cmd_pre_pump), ("pre_dump", cmd_pre_dump),
        ("squeeze", cmd_squeeze), ("confidence", cmd_confidence),
        ("scan", cmd_scan), ("top10", cmd_top10),
        ("pump", cmd_pump), ("dump", cmd_dump),
        ("detail", cmd_detail), ("structure", cmd_structure),
        ("status", cmd_status),
        ("set_score", cmd_set_score), ("set_early", cmd_set_early),
        ("set_max_change", cmd_set_max_change),
        ("myfilters", cmd_myfilters), ("reset", cmd_reset),
        ("sub_pre", cmd_sub_pre), ("sub", cmd_sub),
        ("unsub_all", cmd_unsub_all),
    ]
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))

    app.job_queue.run_repeating(push_pre_warning, interval=1800, first=120)
    app.job_queue.run_repeating(push_general, interval=3600, first=300)

    log.info("🤖 妖幣 Bot V2 啟動")
    app.run_polling()


if __name__ == "__main__":
    main()
