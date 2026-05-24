#!uv run python
"""Phase watchdog — SILENT while positions open. Triggers alert when closed."""
import json
from pathlib import Path

HEARTBEAT = Path("/opt/data/hermes-trading/state/heartbeat.json")

if not HEARTBEAT.exists():
    raise SystemExit(0)

try:
    hb = json.loads(HEARTBEAT.read_text())
except (json.JSONDecodeError, OSError):
    raise SystemExit(0)

positions = hb.get("positions", {})
open_pos = {k: v for k, v in positions.items() if v is not None}

# Silent while any position still open (watchdog pattern)
if open_pos:
    raise SystemExit(0)

# All closed — deliver report
state_dir = Path("/opt/data/hermes-trading/state")
trades = {}
for asset_dir in state_dir.iterdir():
    tf = asset_dir / "trades.jsonl"
    if tf.exists():
        lines = tf.read_text().strip().split("\n")
        if lines:
            trades[asset_dir.name] = json.loads(lines[-1])

msg = "🔔 **Trading Phase Update: Positions Closed**\n\n"
for asset, t in trades.items():
    msg += f"**{asset}**: Exited at {t['exit_price']} | PnL: {t['pnl_pct']:+.2f}% | Reason: {t['exit_reason']}\n"
msg += "\nSteps 4-6 are coded and ready. Restart the bot when ready."
print(msg)
