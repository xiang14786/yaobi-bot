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
| `us_data_fetcher.py` | 美股資料抓取（yfinance bulk download） |
| `us_stock_scorer.py` | 美股評分邏輯 |
| `yaobi_scorer_v2.py` | 加密貨幣評分邏輯 |
| `fly.toml` | Fly.io 部署設定 |

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
```

## 已完成功能
### V2.3 Phase 1 ✅
- 背景預掃描（台股/美股每 30 分，加密每 10 分）
- `/stock` 通用個股查詢（自動判斷台股/加密/美股）
- T86 外資連續天數改為 10 天歷史
- TWSE 回傳股票 < 30 支時自動補 CORE_TW50

### V2.3 Phase 3 ✅
- `/watch` `/unwatch` `/mywatchlist` — 自選股追蹤
- `/alert foreign N` — 外資連買 N 天警報
- `/alert pre N` — 評分達 N% 預警
- 背景自選股監控（每 30 分鐘）
- `/stock BTC` 加入 4H K 線方向信號（Binance 免費 API）

### 待完成
#### Phase 2（最後）
- 倉位追蹤（per-user，admin 看全部）
- `/enter [symbol] [price]` — 進場，bot 建議 TP/SL
- `/close [symbol]` — 平倉
- Trailing stop
- SQLite 持久化

#### 策略升級（三市場）
- 加密：Funding Rate + OI + 頂級交易者多空比（Binance/Bybit 免費 API）
- 加密聰明錢榜單
- 台股：提高投信權重、法人聯手榜（外資+投信同買）
- 美股：RS Rating + 法人累積追蹤

## 已知問題 / 注意事項
- PowerShell 寫 fly.toml 需注意 BOM 編碼問題（用 `New-Object System.Text.UTF8Encoding $false`）
- yfinance `t.info` 容易卡住，已改用 `yf.download()` bulk 方式
- FinMind 融資融券偶爾 timeout（2337、2303、2409、3481），會自動重試
- git commit 前若有 index.lock，需手動刪除 `.git/index.lock`

## 用戶偏好
- 喜歡簡短、直接的回覆
- 不需要過多解釋，直接給指令或程式碼
- 部署在 Fly.io 免費額度內（信用卡掛著但 $0 spending limit）
