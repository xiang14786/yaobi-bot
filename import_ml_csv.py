"""
import_ml_csv.py — 將回填 CSV 匯入 yaobi.db
=============================================
使用方式：
    python import_ml_csv.py ml_crypto.csv
    python import_ml_csv.py ml_tw.csv
    python import_ml_csv.py ml_us.csv

也可以一次匯入三個：
    python import_ml_csv.py ml_crypto.csv ml_tw.csv ml_us.csv

輸出：/data/yaobi.db（Fly.io Volume 上的資料庫）
如果要先在本機測試，腳本會自動找到 yaobi.db 的位置。
"""

import csv
import sqlite3
import sys
import os
from pathlib import Path


def find_db() -> str:
    # 優先找 /data/yaobi.db（Fly.io 環境）
    if os.path.isdir("/data"):
        return "/data/yaobi.db"
    # 本機：找腳本旁邊的 yaobi.db
    local = Path(__file__).parent / "yaobi.db"
    return str(local)


def import_csv(csv_path: str, db_path: str):
    print(f"匯入 {csv_path} → {db_path}")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    if not rows:
        print("  [WARN] CSV 為空，跳過")
        return

    conn = sqlite3.connect(db_path)
    # 確保資料表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_scan_data (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT    NOT NULL,
            market        TEXT    NOT NULL,
            scan_ts       INTEGER NOT NULL,
            total_score   REAL,
            early_score   REAL,
            confidence    REAL,
            price         REAL,
            change_pct    REAL,
            feat1         REAL,
            feat2         REAL,
            feat3         REAL,
            feat4         REAL,
            outcome_pct   REAL,
            outcome_label INTEGER,
            labeled_ts    INTEGER
        )
    """)

    inserted = 0
    skipped  = 0
    for r in rows:
        try:
            conn.execute("""
                INSERT INTO ml_scan_data
                  (symbol, market, scan_ts, total_score, early_score, confidence,
                   price, change_pct, feat1, feat2, feat3, feat4,
                   outcome_pct, outcome_label, labeled_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r["symbol"], r["market"], int(r["scan_ts"]),
                float(r["total_score"]), float(r["early_score"]), float(r["confidence"]),
                float(r["price"]), float(r["change_pct"]),
                float(r["feat1"]), float(r["feat2"]),
                float(r["feat3"]), float(r["feat4"]),
                float(r["outcome_pct"]) if r["outcome_pct"] else None,
                int(r["outcome_label"]) if r["outcome_label"] else None,
                int(r["labeled_ts"]) if r["labeled_ts"] else None,
            ))
            inserted += 1
        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f"  [跳過] {r.get('symbol')} {r.get('scan_ts')}: {e}")

    conn.commit()
    conn.close()
    print(f"  ✅ 匯入 {inserted} 筆，跳過 {skipped} 筆")

    # 統計
    conn = sqlite3.connect(db_path)
    total   = conn.execute("SELECT COUNT(*) FROM ml_scan_data").fetchone()[0]
    labeled = conn.execute("SELECT COUNT(*) FROM ml_scan_data WHERE outcome_pct IS NOT NULL").fetchone()[0]
    conn.close()
    print(f"  DB 現有: {total} 筆（已標籤: {labeled}）")


def main():
    if len(sys.argv) < 2:
        print("用法: python import_ml_csv.py <csv檔案> [csv檔案2] ...")
        sys.exit(1)

    db_path = find_db()
    print(f"資料庫路徑: {db_path}\n")

    for csv_path in sys.argv[1:]:
        if not os.path.exists(csv_path):
            print(f"[ERROR] 找不到 {csv_path}")
            continue
        import_csv(csv_path, db_path)
        print()


if __name__ == "__main__":
    main()
