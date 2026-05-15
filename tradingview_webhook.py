"""
tradingview_webhook.py
======================
TradingView Webhook 接收模組

功能：
  1. 啟動一個 aiohttp HTTP 伺服器，監聽 TradingView 的 Webhook POST 請求
  2. 驗證 secret token，防止未授權推送
  3. 解析 TradingView 信號（JSON 或純文字格式）
  4. 格式化訊息並推送到所有訂閱的 Telegram chat

整合方式：
  在 tg_bot_v2.py 的 main() 裡呼叫 start_webhook_server()，
  並把 bot 實例傳入 TradingViewWebhookHandler。

TradingView Webhook URL 範例（設在 Railway）：
  https://your-app.railway.app/tv-webhook

需要在 Railway 環境變數設定：
  TV_WEBHOOK_SECRET=你的密鑰（任意字串）
  WEBHOOK_PORT=8080（可選，預設 8080）
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from aiohttp import web
from tv_chart_utils import get_tv_chart_url, get_tv_chart_url_multi

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  信號類型 → 中文說明對照
# ──────────────────────────────────────────────
SIGNAL_LABELS = {
    "BB_SQUEEZE":      "🔵 布林帶壓縮（即將爆發）",
    "VOLUME_LADDER":   "📈 量能階梯放大",
    "ATR_EXPANSION":   "⚡ 波動率擴張",
    "MTF_BULL":        "🟢 多時框看多共振",
    "MTF_BEAR":        "🔴 多時框看空共振",
    "SLEEPING_WAKE":   "😴➡️⚡ 沉睡甦醒",
    "OB_BULL_TOUCH":   "🏦 觸碰看多訂單區 (OB)",
    "OB_BEAR_TOUCH":   "🏦 觸碰看空訂單區 (OB)",
    "FVG_FILL":        "📐 FVG 缺口回補",
    "CVD_DIVERGE":     "📊 CVD 背離",
    "CUSTOM":          "🔔 自訂信號",
}

DIRECTION_EMOJI = {
    "LONG":    "🟢 做多",
    "SHORT":   "🔴 做空",
    "NEUTRAL": "⚪ 觀望",
    "EXIT":    "🚪 出場",
}


# ──────────────────────────────────────────────
#  核心處理類
# ──────────────────────────────────────────────
class TradingViewWebhookHandler:
    """
    管理 Webhook 訂閱者列表，以及接收到信號時的推送邏輯。

    使用方式：
        handler = TradingViewWebhookHandler(bot, secret="my_secret")
        handler.add_subscriber(chat_id)
    """

    def __init__(self, bot, secret: str = ""):
        self.bot = bot
        self.secret = secret or os.getenv("TV_WEBHOOK_SECRET", "changeme")
        self._subscribers: set[int] = set()
        self._signal_log: list[dict] = []   # 最近 100 筆信號紀錄

    # ── 訂閱管理 ──────────────────────────────
    def add_subscriber(self, chat_id: int):
        self._subscribers.add(chat_id)
        logger.info(f"[TV Webhook] 新增訂閱者 {chat_id}，目前共 {len(self._subscribers)} 人")

    def remove_subscriber(self, chat_id: int):
        self._subscribers.discard(chat_id)
        logger.info(f"[TV Webhook] 移除訂閱者 {chat_id}")

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def get_recent_signals(self, n: int = 10) -> list[dict]:
        return self._signal_log[-n:]

    # ── HTTP 路由處理 ──────────────────────────
    async def handle_webhook(self, request: web.Request) -> web.Response:
        """aiohttp 路由處理函式，掛到 POST /tv-webhook"""

        # 1. 驗證 Secret Token（從 Header 或 Query String）
        token = (
            request.headers.get("X-TV-Token", "")
            or request.rel_url.query.get("token", "")
        )
        if self.secret and token != self.secret:
            logger.warning(f"[TV Webhook] 未授權請求，來源 IP: {request.remote}")
            return web.Response(status=403, text="Unauthorized")

        # 2. 解析 Payload
        content_type = request.content_type or ""
        try:
            if "json" in content_type:
                data = await request.json()
            else:
                raw = await request.text()
                data = self._parse_plain_text(raw)
        except Exception as e:
            logger.error(f"[TV Webhook] 解析 Payload 失敗: {e}")
            return web.Response(status=400, text=f"Bad payload: {e}")

        logger.info(f"[TV Webhook] 收到信號: {data}")

        # 3. 記錄 + 推送
        data["_received_at"] = datetime.now().isoformat()
        self._signal_log.append(data)
        if len(self._signal_log) > 100:
            self._signal_log = self._signal_log[-100:]

        # 非同步推送，不阻擋 HTTP 回應
        asyncio.create_task(self._broadcast(data))

        return web.Response(text="OK")

    async def handle_status(self, request: web.Request) -> web.Response:
        """GET /tv-status — 快速檢查伺服器是否正常"""
        recent = self._signal_log[-3:] if self._signal_log else []
        return web.json_response({
            "status": "running",
            "subscribers": len(self._subscribers),
            "total_signals_received": len(self._signal_log),
            "recent_signals": recent,
        })

    # ── 私有方法 ──────────────────────────────
    def _parse_plain_text(self, text: str) -> dict:
        """
        解析 TradingView 純文字 Webhook payload。

        建議在 TradingView Alert Message 設定成 JSON，
        但若用純文字，格式請設成：
            {{ticker}} {{strategy.order.action}} {{close}} BB_SQUEEZE 15m

        例：BTCUSDT LONG 67500 BB_SQUEEZE 15m
        """
        parts = text.strip().split()
        result = {"raw": text, "signal_type": "CUSTOM"}
        if len(parts) >= 1:
            result["symbol"] = parts[0].replace("/", "").upper()
        if len(parts) >= 2:
            result["direction"] = parts[1].upper()
        if len(parts) >= 3:
            result["price"] = parts[2]
        if len(parts) >= 4:
            result["signal_type"] = parts[3].upper()
        if len(parts) >= 5:
            result["timeframe"] = parts[4]
        return result

    async def _broadcast(self, data: dict):
        """格式化訊息並推送給所有訂閱者"""
        if not self._subscribers:
            logger.info("[TV Webhook] 無訂閱者，略過推送")
            return

        msg = self._format_message(data)
        failed = []

        for chat_id in list(self._subscribers):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
                await asyncio.sleep(0.05)   # 避免 Telegram rate limit
            except Exception as e:
                logger.error(f"[TV Webhook] 推送失敗 {chat_id}: {e}")
                failed.append(chat_id)

        if failed:
            logger.warning(f"[TV Webhook] 推送失敗的 chat_id: {failed}")

    def _format_message(self, data: dict) -> str:
        """把信號資料格式化成 Telegram Markdown 訊息"""
        symbol      = data.get("symbol", "UNKNOWN")
        direction   = data.get("direction", "NEUTRAL").upper()
        price       = data.get("price", "–")
        signal_type = data.get("signal_type", "CUSTOM").upper()
        timeframe   = data.get("timeframe", "–")
        extra_note  = data.get("note", "")          # Pine Script 可自訂額外備註
        score       = data.get("score", "")          # 若 Pine Script 有計算評分
        received_at = data.get("_received_at", "")

        signal_label   = SIGNAL_LABELS.get(signal_type, f"🔔 {signal_type}")
        direction_text = DIRECTION_EMOJI.get(direction, f"⚪ {direction}")

        # 時間格式化
        try:
            dt = datetime.fromisoformat(received_at)
            time_str = dt.strftime("%H:%M:%S")
        except Exception:
            time_str = "–"

        # TradingView 圖表連結（多時框）
        chart_links = get_tv_chart_url_multi(symbol, timeframes=["15", "60", "240"])

        lines = [
            f"📡 *TradingView 警報*",
            f"━━━━━━━━━━━━━━━━━━",
            f"標的: `{symbol}`",
            f"方向: {direction_text}",
            f"信號: {signal_label}",
            f"時框: `{timeframe}`",
            f"價格: `${price}`",
        ]

        if score:
            lines.append(f"評分: `{score}/100`")

        if extra_note:
            lines.append(f"備註: {extra_note}")

        lines += [
            f"時間: `{time_str}`",
            f"━━━━━━━━━━━━━━━━━━",
            f"📊 圖表：{chart_links}",
            f"",
            f"⚠️ _純技術訊號，非投資建議，請配合自身分析使用_",
        ]

        return "\n".join(lines)


# ──────────────────────────────────────────────
#  伺服器啟動函式
# ──────────────────────────────────────────────
def create_webhook_app(handler: TradingViewWebhookHandler) -> web.Application:
    """
    建立 aiohttp Application，掛上所有路由。

    路由：
        POST /tv-webhook   ← TradingView 發信號到這裡
        GET  /tv-status    ← 健康檢查
    """
    app = web.Application()
    async def health(request):
        return web.Response(text="ok")

    app.router.add_post("/tv-webhook", handler.handle_webhook)
    app.router.add_get("/tv-status",  handler.handle_status)
    app.router.add_get("/health",     health)
    return app


async def start_webhook_server(
    handler: TradingViewWebhookHandler,
    host: str = "0.0.0.0",
    port: int | None = None,
):
    """
    在背景啟動 Webhook HTTP 伺服器。

    呼叫範例（放在 tg_bot_v2.py 的 main() 裡）：

        tv_handler = TradingViewWebhookHandler(application.bot)
        await start_webhook_server(tv_handler)
    """
    port = port or int(os.getenv("WEBHOOK_PORT", "8080"))
    app = create_webhook_app(handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[TV Webhook] 伺服器已啟動 → http://{host}:{port}/tv-webhook")
    return runner


# ──────────────────────────────────────────────
#  Telegram Bot 指令處理函式（掛入 tg_bot_v2.py）
# ──────────────────────────────────────────────
async def cmd_sub_tv(update, context, handler: TradingViewWebhookHandler):
    """
    /sub_tv — 訂閱 TradingView Webhook 信號推送

    在 tg_bot_v2.py 的 setup_handlers() 裡加入：
        app.add_handler(CommandHandler("sub_tv",
            lambda u, c: cmd_sub_tv(u, c, tv_handler)))
    """
    chat_id = update.effective_chat.id
    handler.add_subscriber(chat_id)
    webhook_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "your-app.railway.app")
    secret      = handler.secret

    msg = (
        "✅ *已訂閱 TradingView 警報推送*\n\n"
        "請在 TradingView Alert 設定：\n"
        f"• Webhook URL: `https://{webhook_url}/tv-webhook`\n"
        f"• Header：`X-TV-Token: {secret}`\n\n"
        "📋 *Alert Message 建議格式（JSON）：*\n"
        "```json\n"
        "{{\n"
        '  "symbol": "{{ticker}}",\n'
        '  "direction": "{{strategy.order.action}}",\n'
        '  "price": "{{close}}",\n'
        '  "signal_type": "BB_SQUEEZE",\n'
        '  "timeframe": "{{interval}}"\n'
        "}}\n"
        "```\n"
        "或純文字格式：\n"
        "`{{ticker}} LONG {{close}} BB_SQUEEZE {{interval}}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_unsub_tv(update, context, handler: TradingViewWebhookHandler):
    """/unsub_tv — 取消訂閱"""
    chat_id = update.effective_chat.id
    handler.remove_subscriber(chat_id)
    await update.message.reply_text("❌ 已取消訂閱 TradingView 警報推送")


async def cmd_tv_status(update, context, handler: TradingViewWebhookHandler):
    """/tv_status — 查看 Webhook 伺服器狀態與最近信號"""
    recent = handler.get_recent_signals(5)
    count  = handler.subscriber_count()

    lines = [
        "📡 *TradingView Webhook 狀態*",
        f"訂閱人數: {count}",
        f"累計收到信號: {len(handler._signal_log)} 筆",
        "",
        "🕐 *最近 5 筆信號：*",
    ]

    if not recent:
        lines.append("（尚未收到任何信號）")
    else:
        for sig in reversed(recent):
            symbol    = sig.get("symbol", "?")
            direction = sig.get("direction", "?")
            sig_type  = sig.get("signal_type", "?")
            price     = sig.get("price", "?")
            t         = sig.get("_received_at", "")[:19].replace("T", " ")
            lines.append(f"• `{t}` {symbol} {direction} {sig_type} @ ${price}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
