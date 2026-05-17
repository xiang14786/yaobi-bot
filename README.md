# 全民 TG 妖幣策略 Bot V2

> **核心升級: 抓「即將妖動」而非「已經妖動」**
> 整合 OB+FVG 結構分析 + 10 個領先指標 + 多時框共振

---

## 🆕 V2 三大改造

### 1. 提早預警(從事後追高 → 事前埋伏)
- 限制 `max_price_change_pct ≤ 15%` (已動太多視為「已晚」)
- 領先指標權重提至 **30%**(原 0%)
- 新增 `/pre_pump`、`/pre_dump`、`/squeeze` 預警類指令

### 2. OB + FVG 結構分析
- 自動偵測未失效的 **Order Block**(機構訂單區)
- 自動偵測未填補的 **Fair Value Gap**(失衡缺口)
- 提供當前價格的最近支撐/阻力位

### 3. 10 個領先指標
| # | 指標 | 邏輯 | 提早多久 |
|---|------|------|----------|
| 1 | OI 暴增 + 價格平靜 | 大資金建倉中 | 數小時~1 天 |
| 2 | 資金費率背離 | 多/空頭擁擠 vs 不動 | 數小時 |
| 3 | BB 壓縮 | 波動率歷史低位 | 1~3 根 K |
| 4 | 量能階梯放大 | 持續性買賣盤 | 30 分~2 小時 |
| 5 | 多空比變化率 | 散戶情緒急轉 | 即時反指 |
| 6 | CVD 背離 | 量價背離 | 數小時 |
| 7 | 沉睡幣甦醒 | 長期低波後突發 | 行情起點 |
| 8 | 波動率擴張 | ATR 加速 | 1~2 根 K |
| 9 | 多時框共振 | 15m/1h/4h 一致 | 提高勝率 |
| 10 | OB+FVG 結構 | 在關鍵位置 | 進場時機 |

---

## 📁 檔案結構

```
yaobi_tg_bot/
├── structure_analyzer.py   # OB + FVG 偵測
├── leading_indicators.py   # 10 個領先指標
├── yaobi_scorer_v2.py      # V2 主評分引擎
├── tg_bot_v2.py            # V2 Telegram Bot
├── yaobi_scorer.py         # V1 引擎 (保留)
├── tg_bot.py               # V1 Bot (保留)
├── requirements.txt
└── README.md
```

---

## 🚀 快速啟動

```bash
pip install -r requirements.txt
export BOT_TOKEN="從_BotFather_拿到的_token"
python tg_bot_v2.py     # 啟動 V2 (推薦)
```

---

## 💬 V2 指令清單

### 🎯 提早預警 (V2 主打)
- `/pre_pump` 預備暴漲榜 (24h 漲跌 <8%, 早分 ≥45)
- `/pre_dump` 預備暴跌榜
- `/squeeze` BB 壓縮 + OI 建倉
- `/confidence` 高信心榜 (3+ 訊號共振)

### 📊 一般查詢
- `/scan` 個人化篩選掃描
- `/top10` 綜合 Top 10
- `/pump` 看多榜 (含預警 + 延續)
- `/dump` 看空榜
- `/detail BTC` 單幣全維度報告
- `/structure BTC` 單幣 OB+FVG 結構分析

### 🔧 個人化設定
- `/set_score 60` 最低總分
- `/set_early 50` 最低早期分
- `/set_max_change 12` 最大已動 % (超過視為已晚)
- `/myfilters` 查看設定
- `/reset` 重設

### 🔔 訂閱
- `/sub_pre` 預警自動推送 (30 分鐘一次)
- `/sub` 一般榜單推送 (1 小時)
- `/unsub_all` 取消全部訂閱

---

## 🧠 評分權重 V2

| 維度 | V1 | V2 | 說明 |
|------|----|----|------|
| **早期預警** | — | **30%** | 🆕 領先指標 |
| **結構 (OB+FVG)** | — | **18%** | 🆕 ICT 結構 |
| 價格動量 | 20% | 8% | 降權 (已動 = 已晚) |
| 成交異動 | 18% | 10% | |
| 資金費率 | 15% | 10% | |
| 多空失衡 | 12% | 6% | |
| 爆倉強度 | 15% | 6% | |
| 情緒極端 | 10% | 6% | |
| 鏈上代理 | 10% | 6% | |

---

## 📡 資料來源 (新增)

V2 額外使用以下幣安端點:
- `/futures/data/openInterestHist` — OI 歷史 (建倉偵測)
- `/fapi/v1/fundingRate` — 費率歷史 (背離偵測)
- `/fapi/v1/klines` (15m/1h/4h) — 多時框 K 線
- `/futures/data/takerlongshortRatio` (歷史) — CVD 計算

全部公開 API,**不需 API Key**。

---

## 🎓 ICT 操作建議

當 `/structure BTC` 顯示結果時:

| 場景 | 操作 |
|------|------|
| 價格回測 Bullish OB 不破 | 多單進場 |
| 價格反彈 Bearish OB 不過 | 空單進場 |
| FVG 50% 位置 | 理想進場區 |
| 突破關鍵結構後 | 等回踩確認再做 |
| 三時框共振 | 倉位可加重 |

---

## ⚠️ 風險提醒

- 領先指標 ≠ 100% 準確,會有假訊號
- 信心度 (`confidence`) 越高 (越多指標同時觸發) 越可靠
- 建議至少要 `confidence ≥ 60%` 再進場
- 永遠做好倉位控管 + 停損
- 不構成投資建議
