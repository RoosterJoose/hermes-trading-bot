"""
Signal Position Reconciler — Decoupled Safety Net

Reads the persistent signal_ledger.jsonl on a timer, cross-references
against in-memory state, and force-closes any position whose time stop
has expired without the main loop catching it.

Architecture (per deep research):

  "Rather than checking a flag every single tick or building an aggressive
   re-queue loop in the main thread, use a decoupled reconciliation loop
   that runs on a scheduled interval and cross-checks state against the
   persistent ledger."

Key features:
  - Decoupled from main loop (no inlining into signal checks)
  - Reads persistent ledger, not ephemeral in-memory dict
  - Survives bot restarts / crashes
  - Logs every forced close to the ledger
  - Conservative: only force-closes if time stop is clearly expired
  - No hard market order fallback (paper bot — writes ledger entry only)
"""

import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("signal_reconciler")

# How many seconds between reconciliation cycles
RECONCILE_INTERVAL = 120  # 2 minutes

# Import time stop constant from signal_handler
from . import signal_handler as sh


def find_open_ledger_entries(state_dir: str) -> list[dict]:
    """Read signal_ledger.jsonl and return entries still marked OPEN."""
    ledger_path = os.path.join(state_dir, "signal_ledger.jsonl")
    if not os.path.exists(ledger_path):
        return []

    open_entries = []
    try:
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                exit_reason = entry.get("exit_reason")
                status = entry.get("status", "")
                if exit_reason is None and status != "CLOSED":
                    open_entries.append(entry)
    except OSError:
        logger.warning("Cannot read ledger at %s", ledger_path)

    return open_entries


def is_time_stop_expired(entry: dict) -> bool:
    """Check if a ledger entry has exceeded the time stop window."""
    if entry.get("tp1_hit", False):
        return False
    if entry.get("exit_reason") is not None:
        return False

    entry_time_str = entry.get("entry_time") or entry.get("timestamp") or ""
    if not entry_time_str:
        return False

    try:
        if isinstance(entry_time_str, (int, float)):
            entry_dt = datetime.fromtimestamp(entry_time_str, tz=timezone.utc)
        else:
            entry_dt = datetime.fromisoformat(entry_time_str)
    except (ValueError, TypeError):
        logger.debug("Cannot parse entry_time: %s", entry_time_str)
        return False

    elapsed_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
    return elapsed_min > sh.TIME_STOP_MINUTES


def force_close_entry(
    state_dir: str,
    entry: dict,
    current_price: float,
    reason: str = "time_stop_reconciler",
) -> dict:
    """Write a time-stop exit record to the ledger for an open entry."""
    entry_price = entry.get("entry_price", 0)
    direction = entry.get("direction", "long")

    if entry_price <= 0:
        return {"error": "invalid entry_price", "entry": entry.get("asset", "?")}

    if direction == "long":
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
    else:
        pnl_pct = ((entry_price - current_price) / entry_price) * 100

    exit_record = {
        "asset": entry.get("asset", "?"),
        "symbol": entry.get("symbol", entry.get("asset", "?")),
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": current_price,
        "pnl_pct": round(pnl_pct, 2),
        "exit_reason": reason,
        "entry_time": entry.get("entry_time", ""),
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "status": "CLOSED",
        "source": "signal",
    }

    ledger_path = os.path.join(state_dir, "signal_ledger.jsonl")
    try:
        with open(ledger_path, "a") as f:
            f.write(json.dumps(exit_record) + "\n")
    except OSError as e:
        logger.error("Cannot write forced close to ledger: %s", e)
        return {"error": str(e)}

    asset = entry.get("asset", entry.get("symbol", "?"))
    logger.info(
        "RECONCILER: force-closed %s at %.4f | pnl=%.2f%% | reason=%s",
        asset, current_price, pnl_pct, reason,
    )

    return exit_record


def reconcile(state_dir: str, in_memory_positions: dict | None = None) -> list[dict]:
    """Run one reconciliation pass.

    Args:
        state_dir: Path to the bot's state directory.
        in_memory_positions: Optional ref to bot's ``self.signal_positions``.
            Reconciled positions are nulled in-memory.

    Returns:
        List of exit records for force-closed positions.
    """
    open_entries = find_open_ledger_entries(state_dir)
    if not open_entries:
        return []

    closed_records = []

    for entry in open_entries:
        if not is_time_stop_expired(entry):
            continue

        current_price = entry.get("exit_price") or entry.get("current_price", 0)
        if current_price <= 0:
            current_price = entry.get("entry_price", 0)

        exit_record = force_close_entry(
            state_dir, entry, current_price,
            reason="time_stop_reconciler",
        )
        closed_records.append(exit_record)

        if in_memory_positions is not None:
            asset_key = entry.get("asset", entry.get("symbol", ""))
            if asset_key in in_memory_positions:
                in_memory_positions[asset_key] = None
                logger.debug("Cleared in-memory position for %s", asset_key)

    return closed_records


class SignalReconciler:
    """Decoupled background reconciler for signal positions.

    Runs ``reconcile()`` on a timer independent of the main loop's
    tick cycle.  Start via ``asyncio.create_task(reconciler.run())``.
    """

    def __init__(
        self,
        state_dir: str,
        in_memory_positions: dict | None = None,
        interval: int = RECONCILE_INTERVAL,
    ):
        self.state_dir = state_dir
        self.in_memory_positions = in_memory_positions
        self.interval = interval
        self._enabled = True

    async def run(self):
        """Main reconciler loop — run as asyncio task."""
        import asyncio
        logger.info("SignalReconciler started (interval=%ds)", self.interval)
        while self._enabled:
            try:
                closed = reconcile(self.state_dir, self.in_memory_positions)
                if closed:
                    logger.info(
                        "Reconciler closed %d stale position(s)", len(closed)
                    )
            except Exception:
                logger.exception("Reconciler pass failed")
            await asyncio.sleep(self.interval)

    def stop(self):
        """Signal the reconciler to exit on next iteration."""
        self._enabled = False
