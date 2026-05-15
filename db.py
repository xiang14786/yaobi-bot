"""
db.py — SQLite 持久化模組
==========================
儲存：倉位、自選股、警報條件

注意：Fly.io 重新部署會清空檔案系統。
若需跨部署持久化，需要掛載 Fly.io Volume 到 /data。
目前先存在 /app/yaobi.db（重啟不消失，重部署會消失）。
"""

import os
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("db")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "yaobi.db"))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """建立資料表（若不存在）"""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                symbol       TEXT    NOT NULL,
                market       TEXT    NOT NULL,   -- tw / us / crypto
                entry_price  REAL    NOT NULL,
                entry_time   TEXT    NOT NULL,
                tp_price     REAL,
                sl_price     REAL,
                highest_price REAL,              -- trailing stop 用
                status       TEXT    DEFAULT 'open',  -- open / closed
                close_price  REAL,
                close_time   TEXT,
                pnl_pct      REAL
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
        """)
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
                  entry_price: float, tp: float, sl: float) -> int:
    """開倉，回傳 position id"""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO positions
              (user_id, symbol, market, entry_price, entry_time, tp_price, sl_price, highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, symbol, market, entry_price, now, tp, sl, entry_price))
        return cur.lastrowid


def close_position(pos_id: int, close_price: float, entry_price: float):
    now    = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    pnl    = (close_price - entry_price) / entry_price * 100
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


def get_closed_positions(user_id: int, limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM positions WHERE user_id=? AND status='closed' "
            "ORDER BY close_time DESC LIMIT ?",
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]
