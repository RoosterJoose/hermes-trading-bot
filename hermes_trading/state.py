#!/usr/bin/env python3
"""
state.py — Hermes Trading State Manager

Single source of truth for all runtime state.
Written atomically every trading cycle.
Read by: watchdog, dashboard, status_server, verify scripts.

All state is in state/runtime.json, written via .tmp + os.replace()
for crash-safe atomicity. Every written state is instantly readable.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RUNTIME_FILE = "runtime.json"
SCHEMA_VERSION = 2


def _git_commit(base_dir: Path) -> str:
    """Get short git commit hash."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(base_dir),
        )
        return r.stdout.strip()[:12] if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def read_runtime(state_dir: Path) -> dict:
    """Read runtime.json, return empty dict if missing/corrupt.

    Checks schema_version for forward compatibility — if the file was
    written by a NEWER version of the code, returns empty to prevent
    reading incompatible state.
    """
    path = Path(state_dir) / RUNTIME_FILE
    try:
        data = json.loads(path.read_text())
        sv = data.get("schema_version", 0)
        if sv > SCHEMA_VERSION:
            # File written by newer code — can't safely read
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def build_from_loop(loop) -> dict:
    """
    Build runtime state dict from a TradingLoop instance.
    Call once per cycle, then write_runtime().
    """
    now = datetime.now(timezone.utc).isoformat()

    # Positions count
    open_positions = sum(1 for p in loop.positions.values() if p is not None)
    trend_positions = sum(1 for p in loop.trend_positions.values() if p is not None)

    # Daily PnL — compute from persisted session_start_balance
    session_start = getattr(loop, "session_start_balance", loop.initial_balance)
    daily_pnl_pct = round(
        (loop.paper_balance - session_start) / session_start * 100,
        2,
    ) if session_start else None

    # Trading enabled state
    trading_enabled = True
    disabled_reason = None
    if getattr(loop, "portfolio_halt_latched", False):
        trading_enabled = False
        disabled_reason = "portfolio_hard_halt_latched"
    elif getattr(loop, "portfolio_loss_halted", False):
        trading_enabled = False
        disabled_reason = "portfolio_daily_loss_halt"
    cal = getattr(loop, "_event_cal_blackout", None)
    if cal and cal.get("blocked"):
        trading_enabled = False
        disabled_reason = (
            f"event_blackout: ~{cal.get('minutes_until_event', '?')}m"
        )

    # Max drawdown from portfolio tracker
    pt = getattr(loop, "portfolio_tracker", None)
    pt_status = pt.status() if pt and hasattr(pt, "status") else {}
    highest_dd = round(pt_status.get("highest_dd_pct", 0), 2)
    current_dd = round(pt_status.get("drawdown_pct", 0), 2)

    runtime = {
        "timestamp": now,
        "git_commit": _git_commit(loop.base_dir),
        "bot_pid": os.getpid(),
        "mode": loop.mode,
        "cycle_count": getattr(loop, "cycle_count", 0),

        # Balance
        "paper_balance": round(loop.paper_balance, 2),
        "initial_balance": loop.initial_balance,
        "total_pnl_pct": round(
            (loop.paper_balance - loop.initial_balance)
            / loop.initial_balance * 100,
            2,
        ),
        "paper_start_date": getattr(loop, "paper_start_date", None),

        # Daily PnL (persisted for crash survival)
        "session_start_balance": round(session_start, 2),
        "daily_pnl_pct": daily_pnl_pct,

        # Trading kill-switch state
        "trading_enabled": trading_enabled,
        "trading_disabled_reason": disabled_reason,
        "portfolio_halt_latched": getattr(loop, "portfolio_halt_latched", False),

        # Positions
        "open_positions": open_positions,
        "trend_positions": trend_positions,
        "total_open_positions": open_positions + trend_positions,
        "max_open_positions": getattr(loop, "max_concurrent_total", 5),

        # Risk metrics
        "max_drawdown_highest_pct": highest_dd,
        "max_drawdown_current_pct": current_dd,
        "trust_multiplier": round(getattr(loop, "trust_multiplier", 1.0), 3),
        "trust_label": getattr(loop, "trust_label", "unknown"),

        # Market context
        "btc_price": loop.btc_context.get("btc_price"),
        "btc_1h_rsi": loop.btc_context.get("btc_1h_rsi"),
        "fng_value": loop._fng_value() if hasattr(loop, "_fng_value") else None,
        "fng_class": loop._fng_class() if hasattr(loop, "_fng_class") else None,

        # Schema versioning for forward migration safety
        "schema_version": SCHEMA_VERSION,
    }
    return runtime


def write_runtime(state_dir: Path, runtime: dict) -> None:
    """
    Atomically write runtime state to state/runtime.json.

    Writes to .tmp file first, then os.replace() for atomic swap.
    If the process crashes mid-write, the previous runtime.json is intact.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = state_dir / f"{RUNTIME_FILE}.tmp"
    final_path = state_dir / RUNTIME_FILE
    with open(tmp_path, "w") as f:
        json.dump(runtime, f, indent=2, default=str)
    os.replace(tmp_path, final_path)


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a JSON file. Uses .tmp + os.replace() for crash safety.

    Args:
        path: Full path to the target file (e.g., state/session_start.json)
        data: Dict to serialize as JSON

    The .tmp file is written to the same directory as the target,
    then atomically renamed. A crash during writing leaves the
    previous version intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)
