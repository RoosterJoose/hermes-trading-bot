#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Hermes Trading — Verified Deploy
# ═══════════════════════════════════════════════════════════════
# ONE command. Kills everything. Nukes bytecode caches. 
# Starts clean. Verifies new code is running. Prints proof.
#
# Usage: bash deploy.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

BASE_DIR="/opt/data/hermes-trading"
VENV_PYTHON="/opt/hermes/.venv/bin/python"
DEPLOY_LOG="$BASE_DIR/state/deploy.json"

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║  Hermes Trading — Verified Deploy                       ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Record pre-deploy state ────────────────────────────
COMMIT_HASH=$(cd "$BASE_DIR" && git rev-parse HEAD)
BRANCH=$(cd "$BASE_DIR" && git branch --show-current)
echo "  [1/8] Git: $BRANCH @ $COMMIT_HASH"

# ── Step 2: Kill ALL old processes ─────────────────────────────
echo "  [2/8] Killing ALL old processes..."
for pattern in "launch_bot" "server.py" "cloudflared" "bar_ingester" "bot_watchdog" "loop.py"; do
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        for pid in $pids; do
            kill "$pid" 2>/dev/null || true
        done
    fi
done
sleep 3
# SIGKILL survivors
for pattern in "launch_bot" "server.py" "cloudflared" "bar_ingester" "bot_watchdog" "loop.py"; do
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
    fi
done
sleep 1
echo "         ✓ All processes killed"

# ── Step 3: Nuke Python bytecode caches ────────────────────────
echo "  [3/8] Nuking Python bytecode caches..."
find "$BASE_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$BASE_DIR" -name "*.pyc" -delete 2>/dev/null || true
echo "         ✓ All .pyc caches cleared"

# ── Step 4: Clear lock files, stale state ──────────────────────
echo "  [4/8] Clearing stale locks and state..."
rm -f "$BASE_DIR/state/bot.lock"
rm -f /tmp/bot.lock
rm -f "$BASE_DIR/state/heartbeat.json"       # Stale from previous run
rm -f "$BASE_DIR/state/runtime.json"         # Ensures fresh start
rm -f "$BASE_DIR/state/runtime.json.tmp"     # Partial write artifact
rm -f "$BASE_DIR/state/session_start_balance.txt"   # Legacy format — superseded by .json
rm -f "$BASE_DIR/state/learnings.jsonl.tmp"         # Partial write artifacts
rm -f "$BASE_DIR/state/portfolio_audit.json.tmp"
rm -rf "$BASE_DIR/pids"
mkdir -p "$BASE_DIR/pids"
mkdir -p "$BASE_DIR/state"
echo "         ✓ Stale state cleared"

# ── Step 5: Rotate logs ────────────────────────────────────────
echo "  [5/8] Rotating logs..."
mkdir -p "$BASE_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
for log in bot api tunnel ingester watchdog; do
    if [ -s "$BASE_DIR/$log.log" ]; then
        mv "$BASE_DIR/$log.log" "$BASE_DIR/logs/${log}_${TIMESTAMP}.log" 2>/dev/null || true
    fi
    : > "$BASE_DIR/$log.log"
done
echo "         ✓ Logs rotated"

# ── Step 6: Write deployment marker ────────────────────────────
echo "  [6/8] Writing deployment marker..."
DEPLOY_TIME=$(date -Iseconds -u)
echo "{\"commit\":\"$COMMIT_HASH\",\"branch\":\"$BRANCH\",\"deployed_at\":\"$DEPLOY_TIME\",\"verified\":false}" > "$DEPLOY_LOG"
echo "         ✓ deploy.json written"

# ── Step 7: Start fresh ────────────────────────────────────────
echo "  [7/8] Starting fresh system..."
# Export PYTHONDONTWRITEBYTECODE to prevent future .pyc issues
export PYTHONDONTWRITEBYTECODE=1

# Launch watchdog in background
nohup "$VENV_PYTHON" -B "$BASE_DIR/bot_watchdog.py" >> "$BASE_DIR/watchdog.log" 2>&1 &
WATCHDOG_PID=$!
echo "$WATCHDOG_PID" > "$BASE_DIR/pids/watchdog.pid"
echo "         ✓ Watchdog started (PID $WATCHDOG_PID)"

# ── Step 8: Verify ─────────────────────────────────────────────
echo "  [8/8] Verifying deployment..."
echo "         (waiting up to 90s for first bot heartbeat...)"

# Poll until bot is running or timeout
TIMEOUT=90
ELAPSED=0
VERIFIED=false
while [ $ELAPSED -lt $TIMEOUT ]; do
    if [ -f "$BASE_DIR/pids/bot.pid" ]; then
        BOT_PID=$(cat "$BASE_DIR/pids/bot.pid")
        if kill -0 "$BOT_PID" 2>/dev/null; then
            # Check if runtime.json has been written
            if [ -f "$BASE_DIR/state/runtime.json" ]; then
                echo "         ✓ Bot running (PID $BOT_PID) with runtime state"
                VERIFIED=true
                break
            fi
        fi
    fi
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done

# Run verification
"$VENV_PYTHON" "$BASE_DIR/scripts/verify_deploy.py"
VERIFY_EXIT=$?

echo ""
if [ $VERIFY_EXIT -eq 0 ]; then
    # Mark deployment as verified
    "$VENV_PYTHON" -c "
import json
dep = json.load(open('$DEPLOY_LOG'))
dep['verified'] = True
dep['verified_at'] = '$(date -Iseconds -u)'
json.dump(dep, open('$DEPLOY_LOG', 'w'), indent=2)
"
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║  ✅ DEPLOY VERIFIED — System is running new code        ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo "  Commit:    $COMMIT_HASH"
    echo "  Branch:    $BRANCH"
    echo "  Time:      $DEPLOY_TIME"
    echo "  Watchdog:  PID $WATCHDOG_PID"
    echo ""
    echo "  Quick checks:"
    echo "    Verify → bash $BASE_DIR/verify.sh"
    echo "    Logs   → tail -f $BASE_DIR/watchdog.log"
    echo "    Deploy → cat $BASE_DIR/state/deploy.json"
else
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║  ❌ DEPLOY FAILED VERIFICATION                          ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    exit 1
fi
