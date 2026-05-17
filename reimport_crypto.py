import csv, sqlite3

conn = sqlite3.connect('yaobi.db')
conn.execute("DELETE FROM ml_scan_data WHERE market='crypto'")
rows = list(csv.DictReader(open('ml_crypto.csv')))
for r in rows:
    conn.execute(
        'INSERT INTO ml_scan_data (symbol,market,scan_ts,total_score,early_score,confidence,price,change_pct,feat1,feat2,feat3,feat4,outcome_pct,outcome_label,labeled_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (r['symbol'], r['market'], int(r['scan_ts']),
         float(r['total_score']), float(r['early_score']), float(r['confidence']),
         float(r['price']), float(r['change_pct']),
         float(r['feat1']), float(r['feat2']), float(r['feat3']), float(r['feat4']),
         float(r['outcome_pct']), int(r['outcome_label']), int(r['labeled_ts']))
    )
conn.commit()
non_zero_oi = sum(1 for r in rows if float(r['feat4']) != 0)
print(f'完成！{len(rows)} 筆，OI 非零筆數：{non_zero_oi}')
conn.close()
