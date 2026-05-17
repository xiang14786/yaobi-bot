"""
db.py — SQLite 持久化模組
==========================
儲存：倉位、自選股、警報條件

持久化策略：
- 優先使用環境變數 DB_PATH
- 若 /data 目錄存在（Fly.io Volume 掛載），使用 /data/yaobi.db（跨部署持久）
- fallback: /app/yaobi.db（重啟不消失，重部署會消失）
"""

import os
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("db")

def _resolve_db_path() -> str:
    if "DB_PATH" in os.environ:
        return os.environ["DB_PATH"]
    if os.path.isdir("/data"):
        return "/data/yaobi.db"
    return os.path.join(os.path.dirname(__file__), "yaobi.db")

DB_PATH = _resolve_db_path()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """建立資料表（若不存在）"""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                symbol        TEXT    NOT NULL,
                market        TEXT    NOT NULL,   -- tw / us / crypto
                direction     TEXT    DEFAULT 'long',  -- long / short
                leverage      INTEGER DEFAULT 1,        -- 1 = 現貨
                entry_price   REAL    NOT NULL,
                entry_time    TEXT    NOT NULL,
                tp_price      REAL,
                sl_price      REAL,
                highest_price REAL,              -- trailing stop 用
                status        TEXT    DEFAULT 'open',  -- open / closed
                close_price   REAL,
                close_time    TEXT,
                pnl_pct       REAL
            );
            CREATE TABLE IF NOT EXISTS watchlists (
                user_id  INTEGER NOT NULL,
                symbol   TEXT    NOT NULL,
                PRIMARY KEY (user_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS alert_conditions (
                user_id        INTEGER PRIMARY KEY,
                foreign_streak INTEGER DEFAULT 3,
                pre_warn_pct   INTEGER DEFAULT 70
            );
            CREATE TABLE IF NOT EXISTS subscribers (
                channel  TEXT    NOT NULL,
                user_id  INTEGER NOT NULL,
                PRIMARY KEY (channel, user_id)
            );
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id  INTEGER NOT NULL,
                market   TEXT    NOT NULL,   -- crypto / tw / us
                filters  TEXT    NOT NULL,   -- JSON
                PRIMARY KEY (user_id, market)
            );
            CREATE TABLE IF NOT EXISTS ml_scan_data (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT    NOT NULL,
                market        TEXT    NOT NULL,   -- crypto / tw / us
                scan_ts       INTEGER NOT NULL,   -- unix timestamp
                total_score   REAL,
                early_score   REAL,
                confidence    REAL,
                price         REAL,
                change_pct    REAL,
                feat1         REAL,   -- crypto:funding_rate / tw:foreign_net億 / us:rs_rating
                feat2         REAL,   -- crypto:long_short   / tw:foreign_streak / us:accum_score
                feat3         REAL,   -- crypto:top_trader   / tw:score_bb       / us:momentum_score
                feat4         REAL,   -- crypto:oi_change    / tw:margin_change  / us:inst_pct
                btc_change_24h  REAL DEFAULT 0,  -- BTC當時24h漲跌幅（市場環境特徵）
                consistency_score INTEGER DEFAULT 0, -- 指標一致性（0~5個同向）
                outcome_pct   REAL,   -- N天後漲跌幅（NULL=未標籤）
                outcome_label INTEGER,-- 1=漲>3% / 0=平 / -1=跌>3%
                labeled_ts    INTEGER
            );
        """)
    # 舊 DB 相容：加入新欄位（已存在則忽略）
    with _conn() as c:
        for col, typedef in [
            ("btc_change_24h",   "REAL DEFAULT 0"),
            ("consistency_score","INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE ml_scan_data ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # 欄位已存在
    log.info(f"[DB] 初始化完成：{DB_PATH}")


# ── 自選股 ──────────────────────────────────────
def load_watchlists() -> dict[int, set]:
    """載入所有用戶的自選股"""
    result: dict[int, set] = {}
    with _conn() as c:
        for row in c.execute("SELECT user_id, symbol FROM watchlists"):
            result.setdefault(row["user_id"], set()).add(row["symbol"])
    return result


def save_watch(user_id: int, symbol: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO watchlists (user_id, symbol) VALUES (?, ?)",
                  (user_id, symbol))


def delete_watch(user_id: int, symbol: str):
    with _conn() as c:
        c.execute("DELETE FROM watchlists WHERE user_id=? AND symbol=?", (user_id, symbol))


# ── 警報條件 ─────────────────────────────────────
def load_alert_conditions() -> dict[int, dict]:
    result: dict[int, dict] = {}
    with _conn() as c:
        for row in c.execute("SELECT * FROM alert_conditions"):
            result[row["user_id"]] = {
                "foreign_streak": row["foreign_streak"],
                "pre_warn_pct":   row["pre_warn_pct"],
            }
    return result


def save_alert_condition(user_id: int, key: str, val: int):
    with _conn() as c:
        c.execute("""
            INSERT INTO alert_conditions (user_id, foreign_streak, pre_warn_pct)
            VALUES (?, 3, 70)
            ON CONFLICT(user_id) DO NOTHING
        """, (user_id,))
        c.execute(f"UPDATE alert_conditions SET {key}=? WHERE user_id=?", (val, user_id))


# ── 倉位 ─────────────────────────────────────────
def open_position(user_id: int, symbol: str, market: str,
                  entry_price: float, tp: float, sl: float,
                  direction: str = "long", leverage: int = 1) -> int:
    """開倉，回傳 position id"""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO positions
              (user_id, symbol, market, direction, leverage,
               entry_price, entry_time, tp_price, sl_price, highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, symbol, market, direction, leverage,
              entry_price, now, tp, sl, entry_price))
        return cur.lastrowid


def close_position(pos_id: int, close_price: float, entry_price: float,
                   direction: str = "long"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if direction == "short":
        pnl = (entry_price - close_price) / entry_price * 100
    else:
        pnl = (close_price - entry_price) / entry_price * 100
    with _conn() as c:
        c.execute("""
            UPDATE positions
            SET status='closed', close_price=?, close_time=?, pnl_pct=?
            WHERE id=?
        """, (close_price, now, pnl, pos_id))


def update_highest(pos_id: int, price: float):
    with _conn() as c:
        c.execute("UPDATE positions SET highest_price=? WHERE id=?", (price, pos_id))


def get_open_positions(user_id: int | None = None) -> list[dict]:
    """None = 所有用戶（admin 用）"""
    with _conn() as c:
        if user_id is None:
            rows = c.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='open' AND user_id=?",
                (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_position_by_symbol(user_id: int, symbol: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM positions WHERE user_id=? AND symbol=? AND status='open'",
            (user_id, symbol)).fetchone()
    return dict(row) if row else None


def get_positions_by_symbol(user_id: int, symbol: str) -> list[dict]:
    """取得同一標的所有開倉（可能多筆）"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM positions WHERE user_id=? AND symbol=? AND status='open'",
            (user_id, symbol)).fetchall()
    return [dict(r) for r in rows]


def get_closed_positions(user_id: int, limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM positions WHERE user_id=? AND status='closed' "
            "ORDER BY close_time DESC LIMIT ?",
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── 訂閱 ─────────────────────────────────────────
def load_subscribers() -> dict[str, set]:
    """載入所有訂閱，回傳 {channel: set(user_id)}"""
    result: dict[str, set] = {
        "general": set(), "pre_pump": set(),
        "tw": set(), "us": set(),
    }
    with _conn() as c:
        for row in c.execute("SELECT channel, user_id FROM subscribers"):
            ch = row["channel"]
            if ch in result:
                result[ch].add(row["user_id"])
    return result


def save_subscriber(channel: str, user_id: int):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO subscribers (channel, user_id) VALUES (?, ?)",
                  (channel, user_id))


def delete_subscriber(channel: str, user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM subscribers WHERE channel=? AND user_id=?",
                  (channel, user_id))


# ── 用戶篩選設定 ──────────────────────────────────
import json as _json

def load_user_filters(market: str) -> dict[int, dict]:
    """載入所有用戶的篩選設定，{user_id: {key: val}}"""
    result: dict[int, dict] = {}
    with _conn() as c:
        for row in c.execute(
            "SELECT user_id, filters FROM user_filters WHERE market=?", (market,)
        ):
            try:
                result[row["user_id"]] = _json.loads(row["filters"])
            except Exception:
                pass
    return result


def save_user_filter(user_id: int, market: str, filters: dict):
    with _conn() as c:
        c.execute("""
            INSERT INTO user_filters (user_id, market, filters)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, market) DO UPDATE SET filters=excluded.filters
        """, (user_id, market, _json.dumps(filters)))


def delete_user_filter(user_id: int, market: str | None = None):
    with _conn() as c:
        if market:
            c.execute("DELETE FROM user_filters WHERE user_id=? AND market=?",
                      (user_id, market))
        else:
            c.execute("DELETE FROM user_filters WHERE user_id=?", (user_id,))


# ── ML 訓練資料收集 ───────────────────────────────
import time as _time_module

def save_ml_scan(symbol: str, market: str, total_score: float, early_score: float,
                 confidence: float, price: float, change_pct: float,
                 feat1: float = 0, feat2: float = 0,
                 feat3: float = 0, feat4: float = 0,
                 btc_change_24h: float = 0, consistency_score: int = 0):
    """儲存掃描結果供 ML 訓練用"""
    ts = int(_time_module.time())
    with _conn() as c:
        c.execute("""
            INSERT INTO ml_scan_data
              (symbol, market, scan_ts, total_score, early_score, confidence,
               price, change_pct, feat1, feat2, feat3, feat4,
               btc_change_24h, consistency_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, market, ts, total_score, early_score, confidence,
              price, change_pct, feat1, feat2, feat3, feat4,
              btc_change_24h, consistency_score))


def get_unlabeled_ml_data(days_ago: int = 5) -> list[dict]:
    """取得 N 天前尚未標籤的掃描資料"""
    cutoff = int(_time_module.time()) - days_ago * 86400
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM ml_scan_data
            WHERE outcome_pct IS NULL AND scan_ts < ?
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def label_ml_data(record_id: int, outcome_pct: float):
    """補上 N 天後漲跌幅與標籤"""
    if outcome_pct > 3:
        label = 1
    elif outcome_pct < -3:
        label = -1
    else:
        label = 0
    ts = int(_time_module.time())
    with _conn() as c:
        c.execute("""
            UPDATE ml_scan_data
            SET outcome_pct=?, outcome_label=?, labeled_ts=?
            WHERE id=?
        """, (outcome_pct, label, ts, record_id))


def get_ml_stats() -> dict:
    """取得 ML 資料收集統計"""
    with _conn() as c:
        total   = c.execute("SELECT COUNT(*) FROM ml_scan_data").fetchone()[0]
        labeled = c.execute("SELECT COUNT(*) FROM ml_scan_data WHERE outcome_pct IS NOT NULL").fetchone()[0]
        by_mkt  = c.execute(
            "SELECT market, COUNT(*) as n FROM ml_scan_data GROUP BY market"
        ).fetchall()
    return {"total": total, "labeled": labeled,
            "by_market": {r["market"]: r["n"] for r in by_mkt}}

