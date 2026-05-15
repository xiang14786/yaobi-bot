# 妖幣雷達 Bot — 專案記憶

## 專案概述
**名稱**: 妖幣雷達 V2.3  
**部署**: Fly.io (`yaobi-bot-tw`，新加坡 sin 區，shared-cpu-1x 256MB)  
**Bot Token 環境變數**: `BOT_TOKEN`（設在 Fly.io secrets）  
**用途**: 多市場 Telegram 交易機器人，覆蓋加密貨幣、台股、美股

## 主要檔案
| 檔案 | 說明 |
|------|------|
| `tg_bot_v2.py` | 主程式，所有 TG 指令與排程 |
| `tw_data_fetcher.py` | 台股資料抓取（TWSE T86 + FinMind） |
| `tw_stock_scorer.py` | 台股評分邏輯 |
| `us_data_fetcher.py` | 美股資料抓取（yfinance bulk download）+ 法人持股背景快取 |
| `us_stock_scorer.py` | 美股評分邏輯（RS Rating + A/D Ratio）|
| `yaobi_scorer_v2.py` | 加密貨幣評分邏輯（Funding Rate + OI + 頂級多空比）|
| `db.py` | SQLite 持久化（倉位、自選股、警報條件、訂閱、用戶篩選）|
| `fly.toml` | Fly.io 部署設定（Volume 掛載 /data）|
| `docs/index.html` | GitHub Pages 互動式使用說明網頁 |

## 部署指令
```powershell
# 部署
fly deploy -a yaobi-bot-tw

# 看 log
fly logs -a yaobi-bot-tw

# git 存檔
git add -A
git commit -m "說明"
git push

# 機器沒啟動時手動啟動
fly machine start <machine_id> -a yaobi-bot-tw
```

## 已完成功能

### V2.3 Phase 1 ✅
- 背景預掃描（台股/美股市場時段感知，加密每 3 分鐘）
- `/stock` 通用個股查詢（自動判斷台股/加密/美股）
- T86 外資連續天數改為 10 天歷史
- TWSE 回傳股票 < 30 支時自動補 CORE_TW50
- 加密掃描前 100 大幣（依成交量）

### V2.3 Phase 2 — 倉位追蹤 ✅
- `/enter [symbol] [price] [l/s] [槓桿]` — 進場，bot 自動建議 TP/SL（InlineKeyboard 確認）
- `/close [symbol] [價格]` — 手動平倉，多筆時顯示選單
- `/positions` — 查看開倉 & 近期平倉紀錄（admin 可看全部用戶）
- Trailing Stop — 背景每 30 分監控，多單新高時 SL 上移（保留 50% 利潤）
- 觸及 TP/SL 自動推播通知
- 動能轉弱 / RSI 過熱 / 法人轉賣 動態出場建議
- SQLite 持久化，Fly.io Volume 掛載 `/data/yaobi.db`，**重部署不消失**

### V2.3 Phase 3 — 自選股 & 警報 ✅
- `/watch` `/unwatch` `/mywatchlist` — 自選股追蹤（持久化）
- `/alert foreign N` — 外資連買 N 天警報
- `/alert pre N` — 評分達 N% 預警
- 背景自選股監控（每 30 分鐘）
- `/stock BTC` 加入 4H K 線方向信號（Binance 免費 API）

### V2.3 策略升級 ✅
#### 加密貨幣
- Funding Rate、OI、頂級交易者多空比（Binance API）整合進評分
- `has_tt_data` 標記確保只分析有真實頂級資料的幣
- `/smartmoney` — 頂級交易者倉位異常偵測（機構建倉 / 軋空機會 / 多頭優勢 / 極端空頭），無極端信號時顯示前 3 高分幣作參考

#### 台股
- 投信權重提高（25% → 35%），投信加分 +18/+25
- 法人聯手偵測：外資 + 投信同買 → 觸發 `🤝法人聯手` tag，+20 分
- `/tw_joint` — 法人聯手榜
- 台股市場時段感知掃描：盤中每 10 分鐘，盤外每 60 分鐘

#### 美股
- RS Rating（IBD 風格 0-99 百分位，1 年相對強度）
- A/D Ratio（上漲日成交量 / 下跌日成交量，法人累積指標）
- `/us_rs` — RS Rating 領導股榜
- `/us_accum` — 法人累積榜
- 法人持股（`inst_pct`）改為背景 thread 異步抓取，不阻塞主掃描；顯示「法人載入中...」直到快取建立
- 美股市場時段感知掃描：盤中每 10 分鐘，盤外每 60 分鐘

### V2.3 持久化升級 ✅
- Fly.io Volume `yaobi_data` 掛載至 `/data`，SQLite 路徑 `/data/yaobi.db`
- 訂閱（general / pre_pump / tw / us）存入 SQLite，重部署不消失
- 用戶篩選設定（最低總分 / 最低早分 / 最大已動%）存入 SQLite，重部署不消失
- 倉位資料跨部署持久化

### V2.3 UX 升級 ✅
- `/help` 改為按鈕選單（加密/台股/美股/通用/個人化/訂閱 6 大類）
- 訂閱管理改為按鈕面板（✅/☐ 一鍵切換，顯示當前訂閱狀態）
- 無底線指令別名（`/twscan` = `/tw_scan`，共 30+ 個），方便手機用戶
- `/help` 內提示可省略底線
- GitHub Pages 互動式使用說明：https://xiang14786.github.io/yaobi-bot/
  - 5 市場分頁（加密/台股/美股/通用/個人化）
  - 評分系統說明 + 指標解說卡片
  - 無底線指令提示

## 待完成
- `/tw_status` 加入法人聯手計數顯示（小功能）

## 已知問題 / 注意事項
- PowerShell 寫 fly.toml 需注意 BOM 編碼問題（用 `New-Object System.Text.UTF8Encoding $false`）
- yfinance `t.info` 容易卡住，已改用 `yf.download()` bulk 方式；法人持股改為背景 thread 抓取
- FinMind 融資融券偶爾 timeout（2337、2303、2409、3481），會自動重試
- git commit 前若有 index.lock，需手動刪除 `.git\index.lock`（PowerShell: `Remove-Item .git\index.lock -Force`）
- Fly.io 機器有時進入 stopped 狀態（exit code 0），需手動 `fly machine start`
- f-string 中不能直接寫 `\n`，bash patch 腳本用 heredoc 時容易踩到此坑
- Binance top trader API 只支援主流幣，小幣回傳空陣列（`has_tt_data=False`）
- 美股法人持股（`inst_pct`）需從 `us_data_fetcher.get_inst_pct(ticker)` 讀取背景快取，不是直接從 metrics 物件讀
- asyncio 主迴圈可能因 US 背景掃描（yfinance bulk ~90s）卡住約 2 分鐘，屬已知限制

## 用戶偏好
- 喜歡簡短、直接的回覆
- 不需要過多解釋，直接給指令或程式碼
- 部署在 Fly.io 免費額度內（信用卡掛著但 $0 spending limit）
