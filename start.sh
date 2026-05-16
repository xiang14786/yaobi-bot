#!/bin/bash
# 每次啟動時自動建立並啟用 Swap（存在 /data Volume 上，跨部署持久）

SWAPFILE=/data/swapfile

if [ ! -f "$SWAPFILE" ]; then
    echo "[Swap] 首次建立 swapfile（256MB）..."
    dd if=/dev/zero of=$SWAPFILE bs=1M count=256 status=progress
    chmod 600 $SWAPFILE
    mkswap $SWAPFILE
    echo "[Swap] swapfile 建立完成"
fi

echo "[Swap] 啟用 swapfile..."
swapon $SWAPFILE 2>/dev/null || echo "[Swap] 已掛載或略過"
echo "[Swap] 目前 swap 狀態："
free -m

echo "[Bot] 啟動 tg_bot_v2.py..."
exec python tg_bot_v2.py
