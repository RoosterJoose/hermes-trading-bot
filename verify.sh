#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Hermes Trading — System Verification
# ═══════════════════════════════════════════════════════════════
# Run anytime to prove the system is healthy and running
# the right code. No modifications — just inspection.
#
# Usage: bash verify.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

BASE_DIR="/opt/data/hermes-trading"
VENV_PYTHON="/opt/hermes/.venv/bin/python"

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║  Hermes Trading — System Verification                    ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Quick glance at what's in deploy.json
if [ -f "$BASE_DIR/state/deploy.json" ]; then
    echo "📄 Deployment record:"
    cat "$BASE_DIR/state/deploy.json"
    echo ""
    echo ""
fi

# Run the Python verification suite
"$VENV_PYTHON" "$BASE_DIR/scripts/verify_deploy.py"
EXIT_CODE=$?

echo ""
echo "  Process tree:"
ps aux | grep -E "launch_bot|bot_watchdog|server.py|bar_ingester|cloudflared" | grep -v grep | awk '
{
    pid = $2
    stat = $8
    cmd = ""
    for(i=11; i<=NF; i++) cmd = cmd $i " "
    printf "    %-8s %-6s %s\n", pid, stat, substr(cmd,1,60)
}'

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ SYSTEM HEALTHY — All checks passed."
else
    echo "❌ SYSTEM UNHEALTHY — $EXIT_CODE check(s) failed."
fi
exit $EXIT_CODE
