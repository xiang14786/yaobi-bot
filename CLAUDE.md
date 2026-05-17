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

## 每日優化習慣
- 用戶要求：**每天至少優化一次 bot**，或由 Claude 每天提出優化建議
- 每次對話結束前更新 CLAUDE.md 記錄當天工作內容

---

## 2026-05-16 工作記錄

### 評分系統升級
- **加密**：`yaobi_scorer_v2.py` 更新權重（領先預警 26%、FR 13%、OB+FVG 12% 等）
- **台股**：`tw_stock_scorer.py` 加入 MA20/MA60 趨勢乘數（×0.82~×1.12）
- **美股**：`us_stock_scorer.py` MA50 改 MA60、加入 52 週高點評分、單股查詢改 252 天歷史
- 美股掃描清單從 60 → 108 支（後因記憶體縮回 80 支）
- 台股掃描從 60 → 100 支

### Bug 修復（全面掃描）
- `yaobi_scorer_v2.py`：`find_smart_money` fallback 被截斷，補完 + 加 `return result`
- `tg_bot_v2.py`：`cmd_status` 用 `time.time()` 讀 loop monotonic 時間（已修正為 `loop.time()`）
- `tg_bot_v2.py`：help 訂閱面板用 `from_user.id` 而非 `chat_id`（群組訂閱 bug）
- `tg_bot_v2.py`：美股推播時間顯示 20:30 → 21:00（4 處矛盾）
- `tg_bot_v2.py`：版本號 V2.1 → V2.3
- `us_stock_scorer.py`：A/D Ratio off-by-one（`range(-n,0)` → `range(-n+1,0)`）
- `us_stock_scorer.py`：美股總分上限加 `min(100.0, ...)`
- `tg_bot_v2.py`：`_TW_RUNNING` / `_US_RUNNING` / `_CRYPTO_RUNNING` 宣告但沒實際使用，補上 try/finally 並發保護
- `tg_bot_v2.py`：`_crypto_wait_msg` 中 `CRYPTO_CACHE` → `LAST_SCAN`（NameError hotfix）

### 新功能
- 所有掃描指令加入等待訊息 helper（快取暖：⚡ 1~3 秒 / 冷：⏳ XX 秒請稍候）
- 加入全域 `error_handler`：過濾無害錯誤（Message not modified / TimedOut / Query too old），嚴重錯誤通知 admin

### docs/index.html 同步更新
- 推播時間 20:30 → 21:00
- ATR 分數 5 → 10，BB 分數 15 → 20
- RS 門檻 85 → 80，A/D 說明補充
- 新增 `/us_status`、`/us_unsub`、`/tw_unsub` 指令說明

### 記憶體問題（已解決）
- Swap 256MB 加入（/data/swapfile，Fly.io Volume 上）
- gc.collect() 加入每次掃描後，MemAvailable 從 29MB → 80MB
- 美股縮回 80 支，Committed_AS 從 323MB → 216MB
- OOM 問題基本解決

### 全域 error_handler
- 過濾無害錯誤（Message not modified / TimedOut / Query too old）
- 嚴重錯誤自動通知 admin

### Bug 修復（第二輪掃描）
- `tg_bot_v2.py`：`/stock` 台股顯示 support/resistance 為 None 時 crash → 修正
- `us_stock_scorer.py`：A/D ratio up_vol/dn_vol 範圍不對稱 → 對齊
- `tw_stock_scorer.py`：空方主導股票（外資大賣）會出現在預警榜 → 加過濾條件

### ML 訓練資料收集機制
- `db.py` 新增 `ml_scan_data` 資料表
- 每次背景掃描後自動儲存前 30 高分標的（加密/台股/美股）
- 每日背景任務自動標籤 5 天前的資料（補上實際漲跌幅）
- `/ml_status` 查看收集進度
- 資料存在 /data/yaobi.db，預估 6 個月約 2.4MB
- 目標：累積 1000 筆標籤資料後訓練隨機森林模型

### 待查問題
- 新用戶 `/start` `/help` 無回應：已加 error_handler，待觀察下次錯誤通知內容

---

## 2026-05-17 工作記錄

### ML 歷史回填完成
- 寫三支本機回填腳本：`backfill_crypto.py`、`backfill_tw.py`、`backfill_us.py`
- 資料來源：Binance API（加密）、FinMind（台股）、yfinance（美股）
- 本機跑完輸出 CSV → sftp 上傳 → SSH import 進 Fly.io `/data/yaobi.db`
- 匯入結果：加密 7783 筆 + 台股 4662 筆 + 美股 5750 筆 = **18195 筆已標籤資料**
- `import_ml_csv.py` 工具腳本（本機匯入用）
- `reimport_crypto.py`：清除舊加密資料、重新匯入補齊 OI 欄位的 CSV

### ML 訓練 v3（XGBoost + Optuna）
- `train_ml.py` 升級為二元分類（漲>3% vs 其他），AUC 評估
- Optuna 自動調參（80 trials，TPE sampler）
- 特徵工程：score_x_conf、early_ratio、change_sq、feat1x2、feat2x3
- `n_jobs=1`（避免 Windows 中文路徑 UnicodeEncodeError）
- 最終結果：Crypto AUC 0.594、TW AUC 0.652、US AUC 0.638
- 模型以 dict 格式儲存：`{"model": model, "features": ALL_FEATURES}`

### Bot 功能升級
- **TP/SL 互動流程**：觸及 SL 或 TP 反轉不再自動平倉，改為發送含按鈕的警報
  - 按鈕：✅平倉 | 調整SL | 輸入新SL | 繼續持倉
  - 每 5 分鐘重複提醒，第三次後繼續提醒（不自動平倉）
  - `PENDING_ALERTS` dict 追蹤未回應警報
- **台股/美股篩選開放**：`/set_tw_score`、`/set_tw_early`、`/set_us_score`、`/set_us_early`
  - 原本只有加密貨幣可調整，現三市場均可自訂
  - `/myfilters` 顯示三市場篩選設定
  - `/reset` 清除三市場設定
- **Bot 自動儲存真實特徵**：背景掃描後存入 ml_scan_data，feat1~feat4 改為真實指標值
  - 加密：feat1=funding_rate, feat2=long_short_ratio, feat3=top_trader_ls_ratio, feat4=oi_change_pct
  - 台股：feat1=foreign_net/1e8, feat2=foreign_streak, feat3=score_bb, feat4=margin_change_pct
  - 美股：feat1=rs_rating, feat2=accum_score, feat3=momentum_score, feat4=inst_pct
- **`/ml_status` 升級**：顯示最後收集時間與今日新增筆數

### docs/index.html 同步
- 加入台股「領先分」說明（BB壓縮+量能+沉睡甦醒，max ~43分）
- 加入美股「早分」說明（BB壓縮+量能+ATR，max ~45分）
- 補上美股評分表缺少的「量能 15分」列
- 修正台股docstring 25%→35%（法人動向）
- 新增篩選指令說明（台股/美股/加密三市場）
- 推薦設定表格更新為三市場版本

### ML 自動更新評分決策
- 決定**暫不實作**模型自動回調評分權重
- 原因：AUC ~0.65 尚不可靠，自動改權重風險高，回報低

---

---

## 2026-05-18 工作記錄

### 漏洞修復（第三輪全面掃描）
- `yaobi_scorer_v2.py`：`except: pass` → `log.warning` + 加 `import logging`
- `yaobi_scorer_v2.py`：`_HISTORY_CACHE` 加 MAX_SIZE=200 防 OOM
- `tw_data_fetcher.py`：T86 欄位索引加 `>= 0` 邊界檢查（兩處）
- `tw_data_fetcher.py`：外資連買天數 `streak += sign` → `streak += 1` + 方向分離
- `us_stock_scorer.py`：RS Rating `min(99.0, ...)` 防超界
- `tg_bot_v2.py`：ET_TZ 改動態 DST（`_et_tz()` 函式）
- `tg_bot_v2.py`：`_SENT_ALERTS` 改 dict + 24h TTL + `_prune_sent_alerts()` 定期清理
- `tg_bot_v2.py`：import `apply_tw_filters`（hotfix NameError）
- `fetch_mtf_signal`：修正 async context manager 語法錯誤，改用 `asyncio.gather`
- `fetch_mtf_signal`：改為接受共用 session，避免每幣各開 session（OOM 修復）
- `yaobi_scorer_v2.py`：`oi_change_pct` 不存在 → 改用 `score_onchain > 5`

### 信號品質升級
- **預警過濾**：`change_pct < -8% AND early_score < 12` 不推（避免IRYS類假訊號）
- **BTC 大盤過濾**：BTC < -3% 在推播加警示文字
- **一致性門檻**：`passes_consistency_gate()` 要求 ≥3/5 指標同向才推
- **多時框顯示**：`fetch_mtf_signal()` 顯示日線/4H/1H 方向，推播附帶顯示

### 評分系統升級
- **加密早分門檻**：`min_early_score` 40 → 30（一致性門檻補充質量把關）
- **一致性獎勵**：5指標同向 +12/+7/+3，≤1指標 −5（先算）
- **BTC 市場乘數**：漲>2% ×1.08、跌<−3% ×0.85（後乘）
- 計算順序：加權和 → 一致性獎勵 → BTC乘數 → min/max(0,100)
- `get_scan()` 評分前預先抓取 BTC 24h 漲跌（修復第一次掃描乘數=0的問題）

### ML 訓練資料增強
- `db.py`：`ml_scan_data` 加兩欄：`btc_change_24h`、`consistency_score`
- 舊 DB 自動 `ALTER TABLE` 相容（不需重建）
- `save_ml_scan()` 新增兩參數，三市場掃描後均儲存
- `train_ml.py`：`FEATURES` 加入 `btc_change_24h`、`consistency_score`
- `train_ml.py`：`DB_PATH` 改為 `yaobi_fly.db`（本機訓練用）

### docs/index.html 同步
- 加密評分表下方加入「分數調整機制」說明（一致性獎勵 + BTC乘數）

---

## 2026-05-18 ML 訓練升級（下午）

### LightGBM 取代 XGBoost
- `train_ml.py` 升級為 v4，改用 `LGBMClassifier`
- Optuna 參數空間換成 LightGBM 的：`num_leaves`、`min_child_samples`、`subsample_freq`
- 移除 XGBoost 專用參數：`gamma`、`eval_metric`、`verbosity`
- 加入缺欄位保護：`btc_change_24h` / `consistency_score` 若不在 DB 自動補 0

### 訓練結果（2026-05-18，18195 筆資料）
| 市場 | CV AUC | 測試 AUC | 判斷 |
|------|--------|---------|------|
| Crypto | 0.5951 | 0.600 | 正常，無 overfit |
| TW | 0.6516 | 0.644 | 健康 |
| US | 0.6516 | 0.637 | 輕微 overfit，合理 |

### 特徵重要性發現
- 加密：`long_short`（多空比）最重要
- 台股：`margin_chg`（融資變化）最重要
- 美股：`feat1x2`（RS×A/D 交叉特徵）最重要
- `btc_change_24h` / `consistency_score` 因舊資料補 0，重要性低 → 待新資料累積

---

## 2026-05-18 ML 特徵升級（晚）

### 新增特徵：dow + rank_in_session
- **`db.py`**：`ml_scan_data` 表加兩欄 `dow INTEGER DEFAULT -1`、`rank_in_session INTEGER DEFAULT -1`
- **`db.py`**：`init_db()` ALTER TABLE 相容舊 DB，`save_ml_scan()` 加入兩個新參數
- **`tg_bot_v2.py`**：三市場掃描迴圈改用 `enumerate(..., 1)` 取得排名，傳入 `dow=datetime.now().weekday()` 與 `rank_in_session=_rank`
- **`train_ml.py`**：`FEATURES` 加入 `"dow"`、`"rank_in_session"`；缺欄位保護補 -1
- 重新訓練確認：AUC 維持不變（預期，舊資料全為 -1，效果待新資料累積後顯現）

---

## 待完成
- `/tw_status` 加入法人聯手計數顯示（小功能）
- 確認新用戶 `/start` 無回應的根本原因
- **長期**：5天後重新拉 DB（`fly sftp` 下載 `/data/yaobi.db` 改名 `yaobi_fly.db`）重訓，目標 AUC 0.68+（屆時 dow/rank 有真實值）

## 已知問題 / 注意事項
- PowerShell 寫 fly.toml 需注意 BOM 編碼問題（用 `New-Object System.Text.UTF8Encoding $false`）
- yfinance `t.info` 容易卡住，已改用 `yf.download()` bulk 方式；法人持股改為背景 thread 抓取
- FinMind 融資融券偶爾 timeout（2337、2303、2409、3481），會自動重試
- git commit 前若有 index.lock，需手動刪除 `.git\index.lock`（PowerShell: `Remove-Item .git\index.lock -Force`）
- Fly.io 機器有時進入 stopped 狀態（exit code 0），需手動 `fly machine start`
- f-string 中不能直接寫 `\n`，bash patch 腳本用 heredoc 時容易踩到此坑
- Binance top trader API 只支援主流幣，小幣回傳空陣列（`has_tt_data=False`）
- 美股法人持股（`inst_pct`）需從 `us_data_fetcher.get_inst_pct(ticker)` 讀取背景快取，不是直接從 metrics 物件讀
- 大檔案（>22KB）在 Windows 上用 Edit tool 會被截斷，所有大檔案修改必須用 Python subprocess 方式進行
- 美股掃描 80 支 yfinance bulk ~120s，RAM 現在充裕（MemAvailable ~80MB + 256MB Swap）

## 用戶偏好
- 喜歡簡短、直接的回覆
- 不需要過多解釋，直接給指令或程式碼
- 部署在 Fly.io 免費額度內（信用卡掛著但 $0 spending limit）
- 每天至少優化一次 bot

## CLAUDE.md 維護規則（強制）
每次對話結束前，必須更新 CLAUDE.md，確保以下資訊同步：

1. **工作記錄**：在對應日期的區塊補上本次所有變更（修復/優化/升級）
2. **待完成**：已完成的項目從「待完成」移除；新發現的問題或計畫加進去
3. **已知問題**：新發現的 bug 或注意事項補進「已知問題 / 注意事項」
4. **版本號**：若有功能升級，更新專案概述的版本號
5. **檔案說明**：若新增或刪除檔案，更新「主要檔案」表格

同步原則：
- CLAUDE.md 是唯一的專案記憶來源，所有 AI 都依賴它
- 不允許「程式碼改了但 CLAUDE.md 沒更新」的情況
- 不允許「待完成還列著已完成的項目」
- 每次 git commit 前確認 CLAUDE.md 已更新
