#!/usr/bin/env bash
# Hermes Trading Bootstrap v2
# Kills EVERYTHING, starts fresh, launches watchdog as child of PID 1

set -euo pipefail

BASE_DIR="/opt/data/hermes-trading"
VENV_PYTHON="/opt/hermes/.venv/bin/python"

echo "=== Hermes Trading Bootstrap v2 ==="

# Kill ALL existing trading infrastructure
echo "  Killing old processes..."
for pattern in "launch_bot" "server.py" "cloudflared" "bar_ingester" "bot_watchdog"; do
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        # SIGTERM first
        for pid in $pids; do
            kill "$pid" 2>/dev/null || true
        done
    fi
done
sleep 3
# SIGKILL survivors
for pattern in "launch_bot" "server.py" "cloudflared" "bar_ingester" "bot_watchdog"; do
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
    fi
done
sleep 1

# Clean state
rm -rf "$BASE_DIR/pids"
mkdir -p "$BASE_DIR/pids"
for log in bot api tunnel ingester watchdog; do
    : > "$BASE_DIR/$log.log"
done

# Launch watchdog (this starts everything else)
nohup "$VENV_PYTHON" "$BASE_DIR/bot_watchdog.py" >> "$BASE_DIR/watchdog.log" 2>&1 &
WATCHDOG_PID=$!
echo "$WATCHDOG_PID" > "$BASE_DIR/pids/watchdog.pid"
echo "  Watchdog PID: $WATCHDOG_PID (PPID: $(awk '{print $4}' /proc/$WATCHDOG_PID/stat 2>/dev/null || echo 'N/A'))"

echo "Done. Monitor: tail -f $BASE_DIR/watchdog.log"