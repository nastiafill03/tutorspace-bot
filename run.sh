#!/bin/bash
cd "$(dirname "$0")"
while true; do
    echo "[$(date '+%H:%M:%S')] Starting bot..."
    python3 bot.py
    echo "[$(date '+%H:%M:%S')] Bot exited (code $?). Restarting in 5s..."
    sleep 5
done
