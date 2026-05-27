"""Signal parsing, management, validation, and ledger for signal-following sleeve.

Architecture
------------
This module is the single source of truth for signal-origin trade logic.

- parse_signal()        - 3 format variants -> normalized dict
- cap_signal_sl()       - Enforce 2% max SL from entry
- validate_signal()     - Market-price validation (never modify)
- make_signal_position() -> heartbeat-compatible position dict
- check_signal_exits()  - TP/SL + time-stop checks, returns action dict
- log_signal_entry()    - Persistent JSONL for strategy analysis
- log_signal_exit()
- read_signal_ledger()
- summarize_ledger()

Scale-out model (NotebookLM + institutional mean-reversion):
  TP1: 50% of position, move remaining SL to breakeven
  TP2: 50% of remaining (25% of original)
  TP3: 50% of remaining (12.5% of original)
  TP4: 50% of remaining (6.25%)
  TP5: close remainder

  Time stop: auto-close at 60 min if TP1 not hit.
  SL capped at 2% from entry (overrides signal provider's ~5% SL).
"""

from __future__ import annotations

import json as _json_module
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SIGNAL_SL_PCT = 2.0  # Cap stop-loss at 2% from entry
TIME_STOP_MINUTES = 60   # Auto-close if TP1 not hit within this window
SIGNAL_LEDGER_FILE = "signal_ledger.jsonl"

# TP slice fractions (half-life model)
# On each TP hit, this fraction of the REMAINING position is closed.
# TP1 = 50% of original, TP2 = 25% of original, etc.
_TP_SLICE_REMAINING = 0.50


def _tp_slice(target_num: int) -> float:
    """Return the fraction of the ORIGINAL position to close at *target_num*.

    TP1 -> 0.50 (50% of original)
    TP2 -> 0.25
    TP3 -> 0.125
    TP4 -> 0.0625
    TP5 -> remainder (capped at 1.0)
    """
    if target_num == 1:
        return _TP_SLICE_REMAINING  # 0.5
    # Geometric: 0.5 * 0.5^(n-1) = 0.5^n
    # TP2 = 0.25, TP3 = 0.125, TP4 = 0.0625
    raw = _TP_SLICE_REMAINING ** target_num
    # TP5: close whatever's left
    if target_num >= 5:
        remaining = 1.0 - sum(_tp_slice(t) for t in range(1, 5))
        return max(remaining, 0.0)
    return raw


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_signal(text: str) -> Optional[Dict]:
    """Parse a structured signal from text.

    Supports three formats:

    1.  Compact::
            ONTUSDT LONG 0.0533 SL 0.0507 TP1 0.0536 TP2 0.0541

    2.  Verbose multi-line (app screenshot style)::
            ONTUSDT
            LONG
            Entry: 0.0533
            SL: 0.0507
            TP1: 0.0536
            TP2: 0.0541

    3.  With !! markers::
            !! ONTUSDT LONG 0.0533 SL 0.0507 TP1 0.0536 ...

    Returns normalized dict or None.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()

    # Remove !! prefix
    if text.startswith("!!"):
        text = text[2:].strip()

    # Try compact first (single-line, space-separated)
    if "\n" not in text:
        return _parse_compact(text)

    # Multi-line
    return _parse_verbose(text)


def _parse_compact(text: str) -> Optional[Dict]:
    """Parse: /signal ASSET DIRECTION ENTRY SL TP1 TP2 TP3 TP4 TP5"""
    # Remove leading /signal prefix if present
    if text.startswith("/signal"):
        text = text[7:].strip()

    parts = text.split()
    if len(parts) < 5:
        return None

    asset = parts[0].upper()
    direction = parts[1].lower()
    if direction not in ("long", "short"):
        return None

    try:
        entry = float(parts[2])
    except (ValueError, IndexError):
        return None

    # Parse SL -- could be "SL 0.0507" or just the number
    sl_idx = None
    for i, p in enumerate(parts):
        if p.upper() in ("SL", "STOP", "STOPLOSS", "STOP_LOSS"):
            sl_idx = i
            break
    if sl_idx is not None and sl_idx + 1 < len(parts):
        try:
            stop_loss = float(parts[sl_idx + 1])
        except ValueError:
            return None
    else:
        try:
            stop_loss = float(parts[3])
        except (ValueError, IndexError):
            return None

    # Parse targets
    targets = []
    for i, p in enumerate(parts):
        pu = p.upper()
        if pu.startswith("TP"):
            try:
                targets.append(float(parts[i + 1]))
            except (ValueError, IndexError):
                continue

    if not targets:
        return None

    # Remove duplicates / sort
    seen = set()
    unique = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    if direction == "long":
        unique.sort()
    else:
        unique.sort(reverse=True)

    return {
        "asset": asset,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "targets": unique,
    }


def _parse_verbose(text: str) -> Optional[Dict]:
    """Parse multi-line format from app screenshots."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    asset = None
    direction = None
    entry = None
    stop_loss = None
    targets = []

    for line in lines:
        lower = line.lower()
        # Direction lines
        if lower in ("long", "buy"):
            direction = "long"
            continue
        if lower in ("short", "sell"):
            direction = "short"
            continue

        # Strip markers
        line_clean = line.replace("!!", "").strip()

        # Asset (first line that's just a ticker)
        if asset is None and not any(c in line_clean for c in " :"):
            upper = line_clean.upper()
            if len(upper) <= 10 and upper.isascii():
                asset = upper
                continue

        # Entry / SL / TP
        if "entry" in lower:
            val = _extract_price(line_clean)
            if val is not None:
                entry = val
        elif "sl" in lower and "tp" not in lower:
            val = _extract_price(line_clean)
            if val is not None:
                stop_loss = val
        elif "tp" in lower:
            val = _extract_price(line_clean)
            if val is not None:
                targets.append(val)

    if not asset or not direction or entry is None or stop_loss is None or not targets:
        return None

    if direction == "long":
        targets.sort()
    else:
        targets.sort(reverse=True)

    return {
        "asset": asset,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "targets": targets,
    }


def _extract_price(text: str) -> Optional[float]:
    """Extract the first price-like value from text."""
    import re
    # Match a price after colon or at end
    for pat in [r":\s*([\d.]+)", r"=\s*([\d.]+)", r"\b(\d+\.\d{4,})\b"]:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# SL Cap
# ---------------------------------------------------------------------------


def cap_signal_sl(signal: Dict) -> Dict:
    """Enforce MAX_SIGNAL_SL_PCT cap on stop-loss.

    Mutates *signal* in-place and returns it for chaining.
    If the signal's SL is wider than MAX_SIGNAL_SL_PCT from entry,
    tightens it and logs a note via ``_sl_capped``.
    """
    entry = signal["entry"]
    stop_loss = signal["stop_loss"]
    direction = signal["direction"]

    if direction == "long":
        actual_pct = (entry - stop_loss) / entry * 100
        if actual_pct > MAX_SIGNAL_SL_PCT:
            new_sl = entry * (1 - MAX_SIGNAL_SL_PCT / 100)
            signal["stop_loss"] = round(new_sl, 8)
            signal["_sl_capped"] = True
            signal["_sl_original"] = stop_loss
    else:  # short
        actual_pct = (stop_loss - entry) / entry * 100
        if actual_pct > MAX_SIGNAL_SL_PCT:
            new_sl = entry * (1 + MAX_SIGNAL_SL_PCT / 100)
            signal["stop_loss"] = round(new_sl, 8)
            signal["_sl_capped"] = True
            signal["_sl_original"] = stop_loss

    return signal


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_signal(signal: Dict, current_price: float) -> Tuple[bool, str]:
    """Check signal against current market price.

    Returns (is_valid, reason).
    """
    entry = signal["entry"]
    stop_loss = signal["stop_loss"]
    targets = signal["targets"]
    direction = signal["direction"]

    if current_price <= 0:
        return False, "no current price data"

    # Entry must be within 5% of current market price
    pct_diff = abs(current_price - entry) / entry * 100
    if pct_diff > 5.0:
        return False, f"entry {entry} is {pct_diff:.1f}% from market ${current_price:.4f} (max 5%)"

    # For long, stop must be below entry
    if direction == "long" and stop_loss >= entry:
        return False, f"stop loss {stop_loss} must be below entry {entry}"

    # For short, stop must be above entry
    if direction == "short" and stop_loss <= entry:
        return False, f"stop loss {stop_loss} must be above entry {entry}"

    # Targets must be in order and beyond entry
    if direction == "long":
        prev = entry
        for i, tp in enumerate(targets):
            if tp <= prev:
                return False, f"target {i+1} ({tp}) not above target {i} ({prev})"
            prev = tp
    else:
        prev = entry
        for i, tp in enumerate(targets):
            if tp >= prev:
                return False, f"target {i+1} ({tp}) not below target {i} ({prev})"
            prev = tp

    # Stop distance should be reasonable (0.5% - 15%)
    stop_pct = abs(entry - stop_loss) / entry * 100
    if stop_pct < 0.5:
        return False, f"stop too tight ({stop_pct:.2f}%)"
    if stop_pct > 15.0:
        return False, f"stop too wide ({stop_pct:.2f}%)"

    return True, "ok"


# ---------------------------------------------------------------------------
# Position Management
# ---------------------------------------------------------------------------


def make_signal_position(signal: Dict, size_r: float,
                         asset_key: str) -> Dict:
    """Create a position dict for a signal trade.

    Compatible with the heartbeat JSON schema.

    New fields (per NotebookLM recommendations):
      breakeven_set       - True once SL has been moved to entry after TP1
      time_stop_minutes   - max minutes before auto-close if TP1 not hit
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "asset": asset_key,
        "entry_price": signal["entry"],
        "stop_loss": signal["stop_loss"],
        "targets": signal["targets"],
        "direction": signal["direction"],
        "entry_time": now,
        "position_size_r": size_r,
        "signal": "signal_entry",
        "entry_source": "signal",
        "current_price": signal["entry"],
        "scaled_out": False,
        "breakeven_set": False,
        "time_stop_minutes": TIME_STOP_MINUTES,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "tp4_hit": False,
        "tp5_hit": False,
        "targets_hit": 0,
    }


def check_signal_exits(
    position: Dict,
    current_price: float,
) -> Dict:
    """Check a signal position for take-profit hits, stop loss, or time stop.

    TP scale-out model (half-life):
      TP1: close 50% of original position, caller should set breakeven
      TP2: close 25% (50% of remaining)
      TP3: close 12.5%
      TP4: close 6.25%
      TP5: close remainder

    Time stop: if TIME_STOP_MINUTES elapsed without TP1 hit -> ``time_stop``.

    Returns dict with keys:
      action: str - "none", "stop_loss", "tp1".."tp5", "time_stop"
      pnl_pct: float
      reason: str
      partial: bool - True if it's a partial close (TP, not final)
      slice_pct: float - what fraction of ORIGINAL position to close
    """
    entry = position["entry_price"]
    direction = position["direction"]
    stop_loss = position["stop_loss"]
    targets = position["targets"]
    targets_hit = position.get("targets_hit", 0)

    if entry <= 0:
        return {"action": "none", "pnl_pct": 0, "reason": "",
                "partial": False, "slice_pct": 0}

    # PnL calculation
    if direction == "long":
        pnl_pct = ((current_price - entry) / entry) * 100
    else:
        pnl_pct = ((entry - current_price) / entry) * 100

    # --- Time stop (if TP1 not yet hit) -----------------------------
    if not position.get("tp1_hit", False):
        entry_time_str = position.get("entry_time", "")
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                elapsed_min = (
                    datetime.now(timezone.utc) - entry_dt
                ).total_seconds() / 60
                if elapsed_min > TIME_STOP_MINUTES:
                    return {
                        "action": "time_stop",
                        "pnl_pct": pnl_pct,
                        "reason": "time_stop_60min",
                        "partial": False,
                        "slice_pct": 1.0,
                    }
            except Exception:
                pass

    # --- Stop loss check (always) -----------------------------------
    if direction == "long" and current_price <= stop_loss:
        return {"action": "stop_loss", "pnl_pct": pnl_pct,
                "reason": "signal_stop_loss", "partial": False, "slice_pct": 1.0}
    if direction == "short" and current_price >= stop_loss:
        return {"action": "stop_loss", "pnl_pct": pnl_pct,
                "reason": "signal_stop_loss", "partial": False, "slice_pct": 1.0}

    # --- Target checks ----------------------------------------------
    # TP1 = 50% of original, TP2+ = half-life of remaining
    remaining_after_prior = 1.0

    for i, tp in enumerate(targets):
        target_num = i + 1
        hit_key = f"tp{target_num}_hit"

        if position.get(hit_key, False):
            # Already hit; track what was consumed
            slice_at_target = _tp_slice(target_num)
            remaining_after_prior -= slice_at_target
            continue

        hit = False
        if direction == "long" and current_price >= tp:
            hit = True
        elif direction == "short" and current_price <= tp:
            hit = True

        if hit:
            slice_here = _tp_slice(target_num)
            is_last = target_num >= len(targets) or target_num >= 5
            return {
                "action": f"tp{target_num}",
                "pnl_pct": pnl_pct,
                "reason": f"signal_tp{target_num}",
                "partial": not is_last,
                "slice_pct": slice_here,
                "target_index": i,
            }

    return {"action": "none", "pnl_pct": pnl_pct, "reason": "",
            "partial": False, "slice_pct": 0}


def format_signal_summary(signals_trades: List[Dict]) -> str:
    """Format a human-readable summary of signal trade history."""
    if not signals_trades:
        return "No signal trades yet."

    lines = ["\U0001f4e1 **Signal Trade History**"]
    total_pnl = 0
    wins = 0
    losses = 0

    for t in signals_trades:
        pnl = t.get("pnl_pct", 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1

        asset = t.get("asset", "?")
        entry = t.get("entry_price", 0)
        reason = t.get("exit_reason", "open")

        # Compute duration from timestamps
        entry_time = t.get("entry_time") or t.get("timestamp") or ""
        exit_time = t.get("exit_time") or ""
        duration_str = "?"
        if entry_time and exit_time:
            try:
                if isinstance(entry_time, (int, float)):
                    ed = datetime.fromtimestamp(entry_time, tz=timezone.utc)
                else:
                    ed = datetime.fromisoformat(entry_time)
                if isinstance(exit_time, (int, float)):
                    xd = datetime.fromtimestamp(exit_time, tz=timezone.utc)
                else:
                    xd = datetime.fromisoformat(exit_time)
                dur_min = (xd - ed).total_seconds() / 60
                duration_str = f"{dur_min:.0f}m" if dur_min < 120 else f"{dur_min/60:.1f}h"
            except (ValueError, TypeError):
                duration_str = "?"

        lines.append(
            f"  {asset}: entry=${entry} \u2192 {pnl:+.2f}% ({reason}) [{duration_str}]"
        )

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    lines.append(f"\n**Summary:** {total} trades | {wr:.0f}% WR | PnL: {total_pnl:+.2f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Performance Ledger
# ---------------------------------------------------------------------------


def log_signal_entry(state_dir, signal: dict, asset_key: str,
                     size_r: float, market_ctx: dict = None):
    """Log a signal entry with market context for performance tracking."""
    record = {
        "event": "entry",
        "asset": asset_key,
        "direction": signal.get("direction"),
        "entry_price": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "targets": signal.get("targets", []),
        "size_r": size_r,
        "sl_capped": signal.get("_sl_capped", False),
        "sl_original": signal.get("_sl_original", None),
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if market_ctx:
        record["market_ctx"] = market_ctx
    path = Path(str(state_dir)) / SIGNAL_LEDGER_FILE
    with open(path, "a") as f:
        f.write(_json_module.dumps(record) + "\n")


def log_signal_exit(state_dir, asset_key: str, exit_price: float,
                    pnl_pct: float, reason: str, slice_pct: float = 1.0,
                    entry_price: float = None, entry_time: str = None,
                    direction: str = "long", targets_hit: int = None,
                    market_ctx: dict = None):
    """Log a signal exit to the performance ledger."""
    record = {
        "event": "exit",
        "asset": asset_key,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "slice_pct": slice_pct,
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if entry_price is not None:
        record["entry_price"] = entry_price
    if entry_time is not None:
        record["entry_time"] = entry_time
    if direction is not None:
        record["direction"] = direction
    if targets_hit is not None:
        record["targets_hit"] = targets_hit
    if market_ctx:
        record["market_ctx"] = market_ctx
    path = Path(str(state_dir)) / SIGNAL_LEDGER_FILE
    with open(path, "a") as f:
        f.write(_json_module.dumps(record) + "\n")


def read_signal_ledger(state_dir) -> list:
    """Read the full signal ledger into a list of dicts."""
    from pathlib import Path
    path = Path(str(state_dir)) / SIGNAL_LEDGER_FILE
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(_json_module.loads(line))
                except Exception:
                    continue
    return records


def summarize_ledger(state_dir) -> str:
    """Generate a human-readable performance summary from the ledger."""
    records = read_signal_ledger(state_dir)
    if not records:
        return "No signal ledger records yet."

    entries = [r for r in records if r.get("event") == "entry"]
    exits = [r for r in records if r.get("event") == "exit"]

    total_pnl = sum(e.get("pnl_pct", 0) for e in exits)
    wins = sum(1 for e in exits if e.get("pnl_pct", 0) > 0)
    losses = sum(1 for e in exits if e.get("pnl_pct", 0) <= 0)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    sl_capped = sum(1 for e in entries if e.get("sl_capped"))
    time_stops = sum(1 for e in exits if e.get("reason") == "time_stop_60min")

    # Count partial TP hits across all ledger records (including slice entries)
    tp1_hits = sum(1 for e in records if e.get("exit_reason") in ("signal_tp1", "tp1"))
    tp2_hits = sum(1 for e in records if e.get("exit_reason") in ("signal_tp2", "tp2"))
    tp3_hits = sum(1 for e in records if e.get("exit_reason") in ("signal_tp3", "tp3"))
    reconciler_closes = sum(1 for e in records if e.get("exit_reason") == "time_stop_reconciler")

    # Count unique signal trades (deduplicate slice entries by asset)
    unique_assets = set()
    for r in records:
        asset = r.get("asset", r.get("symbol", ""))
        if asset:
            unique_assets.add(asset)

    lines = [
        "\U0001f4e1 **Signal Ledger Summary**",
        f"  Unique signal trades: {len(unique_assets)}",
        f"  TP1 hits: {tp1_hits} | TP2: {tp2_hits} | TP3: {tp3_hits}",
        f"  Entries: {len(entries)} | Exits: {len(exits)}",
        f"  Win Rate: {wr:.1f}% ({wins}W/{losses}L)",
        f"  Total PnL: {total_pnl:+.2f}%",
        f"  SL capped: {sl_capped}",
        f"  Time stops: {time_stops}",
        f"  Reconciler closes: {reconciler_closes}",
    ]
    return "\n".join(lines)
